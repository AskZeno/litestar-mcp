#!/usr/bin/env python3
# Copy of AskZeno/infrastructure scripts/dependency_release_age.py (infra#2981 head
# 48984d333ec74a0ac2c8fd658117dce455508dc1). This repository is public, so it cannot call the
# private repository's action; keep this copy in step with infra's gate. Only GUARD_SELF and
# path hints differ.
"""Block npm and PyPI releases younger than the org minimum release age (LEA-197).

The org Renovate preset sets `minimumReleaseAge: "7 days"`, but that only holds back Renovate's own
pull requests. This gate applies the same rule to every pull request through Aikido Safe Chain
(.github/actions/safe-chain). Aikido-confirmed CVE patch releases are exempt from the age check.

Subcommands (stdlib only, so the gate needs no package install of its own):

  guard   fail if any tracked file lowers or bypasses the Safe Chain age check
  check   resolve pinned versions through the Safe Chain shims (run .github/actions/safe-chain
          first); with --base REV only the pins REV does not already have, so a pull request is
          blocked for what it adopts, not for a young pin that is already on main
  canary  prove the shims are active: a PyPI release younger than 7 days must fail to resolve

Pins are collected from uv.lock, poetry.lock, package-lock.json and pnpm-lock.yaml (every registry
package), requirements and constraints files, and inline pins in .github/ workflow and action
YAML, Dockerfiles and shell scripts: `name==1.2.3` and `uvx name@1.2.3` (PyPI), `npx name@1.2.3`
and `npm install name@1.2.3` (npm). `${VAR}` versions are resolved from ARG/ENV defaults, shell assignments and
YAML keys in the same file. Each PyPI pin is checked with `uv pip install --dry-run --no-deps`,
each npm pin with `npm view`; Safe Chain withholds versions younger than
SAFE_CHAIN_MINIMUM_PACKAGE_AGE_HOURS (168) from the registry metadata, so they fail to resolve.
Other lockfiles (yarn, Pipfile, pdm, bun, npm-shrinkwrap) fail closed until the gate learns them.

Not covered (the dependency-release-age runbook in AskZeno/infrastructure): unpinned installs (they take the newest
7-day-old version only where a job runs the action), packages from other indexes, container
images, Helm charts, Actions refs, OpenTofu providers and Go modules.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

MIN_AGE_HOURS = 168
ENV_HOURS = "SAFE_CHAIN_MINIMUM_PACKAGE_AGE_HOURS"
SHIM_DIR = Path.home() / ".safe-chain" / "shims"

# This file and its test spell out the bypass strings they look for.
GUARD_SELF = {".github/scripts/dependency_release_age.py"}

_SKIP_FLAG = "safe-chain-skip-minimum-package-age"
_NUMERIC_SETTINGS = [
    # env var assignment (shell, YAML, JSON, Dockerfile): NAME=V, NAME: V, "NAME": V
    re.compile(r"SAFE_CHAIN_MINIMUM_PACKAGE_AGE_HOURS[\"']?\s*[:=]\s*[\"']?([\w${}.-]*)"),
    # Dockerfile `ENV NAME VALUE`
    re.compile(r"\bENV\s+SAFE_CHAIN_MINIMUM_PACKAGE_AGE_HOURS\s+[\"']?([\w${}.-]*)"),
    # CLI flag: --safe-chain-minimum-package-age-hours=V or ... V
    re.compile(r"--safe-chain-minimum-package-age-hours(?:=|\s+)[\"']?([\w${}.-]*)"),
    # config file keys (~/.safe-chain/config.json, .aikido)
    re.compile(r"[\"']?minimumPackageAgeHours[\"']?\s*:\s*[\"']?([\w${}.-]*)"),
]
_FORBIDDEN = [
    (re.compile(re.escape(_SKIP_FLAG), re.I), "skips the Safe Chain minimum package age check"),
    (re.compile(r"MINIMUM_PACKAGE_AGE_EXCLUSIONS|minimumPackageAgeExclusions"),
     "exempts packages from the Safe Chain minimum package age check"),
    (re.compile(r"SAFE_CHAIN_MALWARE_LIST_BASE_URL|malwareListBaseUrl|safe-chain-malware-list-base-url"),
     "replaces the Safe Chain malware and new-release lists"),
]


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True).stdout
    return [p for p in out.decode().split("\0") if p]


def _read_text(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 5_000_000:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def guard_text(rel: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    if Path(rel).name == ".aikido" and re.search(r"(?m)^\s*safe-chain\s*:", text):
        findings.append(Finding(rel, 1, "project .aikido file configures Safe Chain; settings belong in "
                                        ".github/actions/safe-chain only"))
    for lineno, line in enumerate(text.splitlines(), 1):
        for pattern, why in _FORBIDDEN:
            if pattern.search(line):
                findings.append(Finding(rel, lineno, why))
        for pattern in _NUMERIC_SETTINGS:
            for match in pattern.finditer(line):
                value = match.group(1)
                if not value.isdigit() or int(value) < MIN_AGE_HOURS:
                    findings.append(Finding(
                        rel, lineno,
                        f"sets the Safe Chain minimum package age to {value or '<empty>'!r}; "
                        f"it must be a literal of at least {MIN_AGE_HOURS} hours"))
    return findings


def cmd_guard(root: Path) -> int:
    findings: list[Finding] = []
    for rel in tracked_files(root):
        if rel in GUARD_SELF:
            continue
        text = _read_text(root / rel)
        if text is not None:
            findings.extend(guard_text(rel, text))
    for finding in findings:
        print(f"::error file={finding.path},line={finding.line}::{finding.message}")
        print(finding)
    if findings:
        print(f"{len(findings)} Safe Chain bypass(es) found; see the dependency-release-age runbook in AskZeno/infrastructure")
        return 1
    print("No Safe Chain minimum-age bypass in tracked files.")
    return 0


# ---------------------------------------------------------------------------------------------
# Pin extraction

@dataclass(frozen=True, order=True)
class Pin:
    ecosystem: str  # "pypi" or "npm"
    name: str
    version: str
    source: str = field(default="", compare=False)  # path:line

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.ecosystem, self.name, self.version)


_PY_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_PY_PIN = re.compile(
    rf"(?<![\w.\-/@$=])({_PY_NAME})(?:\[[^\]\s]*\])?===?(\$\{{?\w+\}}?|\d[\w.+!-]*)(?![\w.*$])")
_PY_TOOL = re.compile(r"\b(?:pip3?|uv|uvx|pipx|poetry|pdm)\b")
_UVX_PIN = re.compile(rf"\buvx\s+(?:--?[\w-]+(?:[ =][^\s-]\S*)?\s+)*({_PY_NAME})@(\$\{{?\w+\}}?|\d[\w.+!-]*)")
_NPM_NAME = r"(?:@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*"
_NPM_LINE = re.compile(r"\b(?:npx|npm\s+(?:i|install|add|exec|x)|pnpm\s+(?:add|dlx)|yarn\s+(?:add|dlx)|bunx)\b")
_NPM_PIN = re.compile(rf"(?<![\w./@-])({_NPM_NAME})@(\$\{{?\w+\}}?|\d[\w.+-]*)(?![\w.*])")
_DOCKER_VAR = re.compile(r"^\s*(?:ARG|ENV)\s+([A-Za-z_]\w*)=[\"']?([^\s\"']+)", re.M)
_YAML_VAR = re.compile(r"^\s*([A-Za-z_]\w*):\s*[\"']?([\w.+!-]+)[\"']?\s*(?:#.*)?$", re.M)
_SHELL_VAR = re.compile(r"^\s*(?:export\s+)?([A-Za-z_]\w*)=[\"']?([\w.+!-]+)[\"']?\s*$", re.M)
_VAR_REF = re.compile(r"^\$\{?(\w+)\}?$")


def _kind(rel: str) -> str | None:
    p = Path(rel)
    if _REQUIREMENTS.match(p.name):
        return "requirements"
    if "dockerfile" in p.name.lower():
        return "dockerfile"
    if p.suffix == ".sh":
        return "shell"
    if rel.startswith(".github/") and p.suffix in {".yml", ".yaml"}:
        return "yaml"
    return None


def _logical_lines(text: str, kind: str) -> list[tuple[int, str]]:
    """Join backslash continuations in shell scripts and Dockerfiles; drop comments."""
    out: list[tuple[int, str]] = []
    start, buf = 0, ""
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        code = re.split(r"\s#", line, maxsplit=1)[0]
        if not buf:
            start = lineno
        if kind in {"shell", "dockerfile"} and code.rstrip().endswith("\\"):
            buf += code.rstrip()[:-1] + " "
            continue
        out.append((start, buf + code))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


def pins_in_text(rel: str, text: str) -> tuple[set[Pin], list[str]]:
    """Exact npm/PyPI pins in a workflow, action, Dockerfile, shell script or requirements file.

    Returns the pins and the pins whose version is a variable that could not be resolved.
    """
    kind = _kind(rel) or "yaml"
    pattern = {"dockerfile": _DOCKER_VAR, "shell": _SHELL_VAR}.get(kind, _YAML_VAR)
    variables = {name: value for name, value in pattern.findall(text)}
    pins: set[Pin] = set()
    unresolved: list[str] = []

    def add(ecosystem: str, raw_name: str, raw_version: str, lineno: int) -> None:
        ref = _VAR_REF.match(raw_version)
        version = variables.get(ref.group(1), "") if ref else raw_version
        if not re.match(r"^\d[\w.+!-]*$", version):
            unresolved.append(f"{rel}:{lineno}: {raw_name} {raw_version}")
            return
        name = re.sub(r"[-_.]+", "-", raw_name).lower() if ecosystem == "pypi" else raw_name
        pins.add(Pin(ecosystem, name, version, f"{rel}:{lineno}"))

    for lineno, code in _logical_lines(text, kind):
        if kind == "requirements":
            code = code.strip()
            if code.startswith("-"):
                continue
        elif kind != "yaml" and not (_PY_TOOL.search(code) or _NPM_LINE.search(code)):
            # Shell and Dockerfile `==` is mostly comparisons (awk NR==1); only installer lines count.
            continue
        for name, version in _UVX_PIN.findall(code):
            add("pypi", name, version, lineno)
        if _NPM_LINE.search(code) and "uvx" not in code:
            for name, version in _NPM_PIN.findall(code):
                add("npm", name, version, lineno)
        for name, version in _PY_PIN.findall(code):
            add("pypi", name, version, lineno)
    return pins, unresolved


def uv_lock_pins(rel: str, text: str) -> tuple[set[Pin], list[str]]:
    pins: set[Pin] = set()
    other: list[str] = []
    for package in tomllib.loads(text).get("package", []):
        registry = package.get("source", {}).get("registry")
        if not registry or "version" not in package:
            continue  # editable, path, git or virtual
        if urllib.parse.urlsplit(registry).hostname not in {"pypi.org", "files.pythonhosted.org"}:
            other.append(f"{rel}: {package['name']} {package['version']} from {registry}")
            continue
        name = re.sub(r"[-_.]+", "-", package["name"]).lower()
        pins.add(Pin("pypi", name, package["version"], rel))
    return pins, other


def npm_lock_pins(rel: str, text: str) -> tuple[set[Pin], list[str]]:
    pins: set[Pin] = set()
    other: list[str] = []
    for path, meta in json.loads(text).get("packages", {}).items():
        if not path or meta.get("link") or "version" not in meta:
            continue
        name = meta.get("name") or path.rsplit("node_modules/", 1)[-1]
        resolved = meta.get("resolved", "")
        if not resolved.startswith("https://registry.npmjs.org/"):
            other.append(f"{rel}: {name} {meta['version']} from {resolved or 'unknown'}")
            continue
        pins.add(Pin("npm", name, meta["version"], rel))
    return pins, other


_PNPM_KEY = re.compile(r"^  (['\"]?)/?((?:@[^@/\s'\"]+/)?[^@/\s'\"]+)@([^@()\s'\"]+)(?:\(.*\))?\1:\s*(?:\{\})?\s*$")


def pnpm_lock_pins(rel: str, text: str) -> tuple[set[Pin], list[str]]:
    """Registry packages of a pnpm-lock.yaml (lockfile v6 and v9), without a YAML parser.

    Package keys sit two spaces deep under the top-level `packages:` mapping (`name@1.2.3:`,
    `'@scope/name@1.2.3':`, v6 with a leading `/` and peer suffixes `(react@18.0.0)`). An entry
    whose `resolution` names a tarball outside registry.npmjs.org, a git repository or a directory
    is listed as not checked. Lockfiles older than v6 are rejected.
    """
    match = re.search(r"(?m)^lockfileVersion:\s*['\"]?(\d+)", text)
    if not match or int(match.group(1)) < 6:
        raise ValueError(f"{rel}: pnpm lockfile version older than 6 is not supported")
    pins: set[Pin] = set()
    other: list[str] = []
    section = None
    current: tuple[str, str] | None = None
    for line in text.splitlines():
        if line and not line[0].isspace():
            section = line.split(":", 1)[0].strip()
            current = None
            continue
        if section != "packages" or not line.strip():
            continue
        if not line.startswith("   "):
            key = _PNPM_KEY.match(line)
            if key is None:
                other.append(f"{rel}: unparsed package key {line.strip()}")
                current = None
                continue
            current = (key.group(2), key.group(3))
            if re.match(r"^\d", current[1]):
                pins.add(Pin("npm", current[0], current[1], rel))
            else:
                other.append(f"{rel}: {current[0]} {current[1]}")
                current = None
            continue
        stripped = line.strip()
        if current and stripped.startswith("resolution:"):
            tarball = re.search(r"tarball:\s*['\"]?([^,}'\"\s]+)", stripped)
            foreign = re.search(r"\b(?:type:\s*git|repo:|directory:)", stripped)
            if foreign or (tarball and not tarball.group(1).startswith("https://registry.npmjs.org/")):
                pins.discard(Pin("npm", current[0], current[1], rel))
                other.append(f"{rel}: {current[0]} {current[1]} from {stripped.split(':', 1)[1].strip()}")
    return pins, other


def poetry_lock_pins(rel: str, text: str) -> tuple[set[Pin], list[str]]:
    pins: set[Pin] = set()
    other: list[str] = []
    for package in tomllib.loads(text).get("package", []):
        source = package.get("source") or {}
        kind = source.get("type")
        if kind in {"git", "directory", "file", "url"} or "version" not in package:
            continue  # not from an index, like git and path entries in uv.lock
        if kind and urllib.parse.urlsplit(source.get("url", "")).hostname not in {"pypi.org", "files.pythonhosted.org"}:
            other.append(f"{rel}: {package['name']} {package['version']} from {source.get('url') or kind}")
            continue
        name = re.sub(r"[-_.]+", "-", package["name"]).lower()
        pins.add(Pin("pypi", name, package["version"], rel))
    return pins, other


_UNSUPPORTED_LOCKS = {"yarn.lock", "Pipfile.lock", "pdm.lock", "bun.lock", "bun.lockb", "npm-shrinkwrap.json"}
_REQUIREMENTS = re.compile(r"^(?:.*[-_.])?requirements(?:[-_.].*)?\.txt$|^constraints(?:[-_.].*)?\.txt$")


@dataclass
class Inventory:
    pins: set[Pin] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)  # pins not checked (variables, other indexes)
    unsupported: list[str] = field(default_factory=list)


def inventory(files: list[str], read) -> Inventory:
    """Collect every npm/PyPI pin from `files`; `read(rel)` returns the text or None."""
    inv = Inventory()
    for rel in files:
        if rel in GUARD_SELF:
            continue
        name = Path(rel).name
        if name in _UNSUPPORTED_LOCKS:
            inv.unsupported.append(rel)
            continue
        if name == "uv.lock":
            parse = uv_lock_pins
        elif name == "package-lock.json":
            parse = npm_lock_pins
        elif name == "pnpm-lock.yaml":
            parse = pnpm_lock_pins
        elif name == "poetry.lock":
            parse = poetry_lock_pins
        elif _kind(rel):
            parse = pins_in_text
        else:
            continue
        text = read(rel)
        if text is None:
            continue
        try:
            pins, notes = parse(rel, text)
        except ValueError:  # includes TOML and JSON decode errors: fail closed
            inv.unsupported.append(rel)
            continue
        inv.pins |= pins
        inv.notes += notes
    return inv


def _git_reader(root: Path, ref: str | None):
    if ref is None:
        return tracked_files(root), lambda rel: _read_text(root / rel)
    out = subprocess.run(["git", "ls-tree", "-r", "-z", "--name-only", ref], cwd=root, check=True,
                         capture_output=True).stdout
    files = [p for p in out.decode().split("\0") if p]

    def read(rel: str) -> str | None:
        result = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=root, capture_output=True)
        if result.returncode != 0 or b"\0" in result.stdout[:8192]:
            return None
        return result.stdout.decode("utf-8", errors="replace")

    return files, read


def new_pins(root: Path, base: str | None) -> tuple[list[Pin], Inventory]:
    """Pins in the working tree that the base revision does not already have (all pins without a base)."""
    head = inventory(*_git_reader(root, None))
    known: set[tuple[str, str, str]] = set()
    if base is not None:
        known = {p.key for p in inventory(*_git_reader(root, base)).pins}
    grouped: dict[tuple[str, str, str], list[str]] = {}
    for pin in sorted(head.pins):
        if pin.key not in known:
            grouped.setdefault(pin.key, []).append(pin.source)
    return [Pin(*key, ", ".join(sorted(set(src)))) for key, src in grouped.items()], head


# ---------------------------------------------------------------------------------------------
# Resolution through the Safe Chain shims

def _shimmed(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise SystemExit(f"::error::{tool} is not on PATH")
    if Path(path).parent.resolve() != SHIM_DIR.resolve():
        raise SystemExit(f"::error::{tool} resolves to {path}, not the Safe Chain shim in {SHIM_DIR}; "
                         "run .github/actions/safe-chain after every setup-* step")
    return path


def _require_env() -> None:
    value = os.environ.get(ENV_HOURS, "")
    if not value.isdigit() or int(value) < MIN_AGE_HOURS:
        raise SystemExit(f"::error::{ENV_HOURS} is {value!r}; run .github/actions/safe-chain first")


def _run(argv: list[str], cwd: Path | None = None) -> tuple[int, str]:
    result = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return result.returncode, result.stdout


def resolve(pin: Pin, uv: str | None, npm: str | None, venv: Path, workdir: Path) -> tuple[bool, str]:
    """Ask the registry for exactly this version through Safe Chain; False if it is withheld."""
    if pin.ecosystem == "pypi":
        argv = [uv, "pip", "install", "--dry-run", "--no-deps", "--no-config", "--python", str(venv),
                f"{pin.name}=={pin.version}"]
    else:
        argv = [npm, "view", f"{pin.name}@{pin.version}", "version"]
    code, output = _run(argv, cwd=workdir)
    if pin.ecosystem == "npm" and code == 0 and not output.strip():
        code = 1  # npm view prints nothing for a version range that matches no version
    return code == 0, "+ " + " ".join(argv) + "\n" + output


def cmd_check(root: Path, python: str, base: str | None) -> int:
    _require_env()
    pins, head = new_pins(root, base)
    for note in head.notes:
        print(f"::notice::not checked: {note}")
    if head.unsupported:
        for rel in head.unsupported:
            print(f"::error file={rel}::lockfile type not supported by the dependency release age gate; "
                  "extend .github/scripts/dependency_release_age.py")
        return 1
    scope = f"new since {base}" if base else "all"
    print(f"{len(head.pins)} npm/PyPI pins in the repository; checking {len(pins)} ({scope}).")
    failed: list[Pin] = []
    with tempfile.TemporaryDirectory(prefix="dependency-age-") as tmp_name:
        tmp = Path(tmp_name)
        uv = npm = None
        venv = tmp / "venv"
        if any(p.ecosystem == "pypi" for p in pins):
            uv = _shimmed("uv")
            # In the temporary directory: the checked-out project's requires-python and uv
            # settings must not apply to the throwaway venv.
            code, output = _run([uv, "venv", "--quiet", "--no-config", "--python", python, str(venv)], cwd=tmp)
            if code != 0:
                print(output)
                return 1
        if any(p.ecosystem == "npm" for p in pins):
            npm = _shimmed("npm")
        for pin in pins:
            ok, output = resolve(pin, uv, npm, venv, tmp)
            sep = "==" if pin.ecosystem == "pypi" else "@"
            print(f"::group::{'ok' if ok else 'BLOCKED'} {pin.ecosystem} {pin.name}{sep}{pin.version} ({pin.source})")
            print(output, end="")
            print("::endgroup::", flush=True)
            if not ok:
                failed.append(pin)
    for pin in failed:
        path, _, line = pin.source.split(", ")[0].partition(":")
        sep = "==" if pin.ecosystem == "pypi" else "@"
        print(f"::error file={path},line={line or 1}::{pin.name}{sep}{pin.version} does not resolve through "
              f"Safe Chain: a release younger than {MIN_AGE_HOURS // 24} days (or not published). Pin a "
              "version that is at least 7 days old; see the dependency-release-age runbook in AskZeno/infrastructure")
    print(f"{len(pins) - len(failed)} ok, {len(failed)} blocked.")
    return 1 if failed else 0


def young_pypi_versions(now: datetime, data: dict) -> list[str]:
    """Versions in a PyPI JSON document whose first file was uploaded well under MIN_AGE_HOURS ago, newest first."""
    young = []
    for version, files in data.get("releases", {}).items():
        times = [f["upload_time_iso_8601"] for f in files if not f.get("yanked")]
        if not times:
            continue
        uploaded = min(datetime.fromisoformat(t.replace("Z", "+00:00")) for t in times)
        if now - uploaded < timedelta(hours=MIN_AGE_HOURS - 6):
            young.append((uploaded, version))
    return [v for _, v in sorted(young, reverse=True)]


def cmd_canary(python: str) -> int:
    _require_env()
    uv = _shimmed("uv")
    # boto3 publishes almost every weekday and its releases are not CVE patches, which Aikido
    # exempts from the age check.
    with urllib.request.urlopen("https://pypi.org/pypi/boto3/json", timeout=30) as response:
        data = json.load(response)
    young = young_pypi_versions(datetime.now(timezone.utc), data)[:3]
    if not young:
        print("::notice::no boto3 release younger than 7 days right now; canary skipped")
        return 0
    with tempfile.TemporaryDirectory(prefix="dependency-age-canary-") as tmp:
        venv = Path(tmp) / "venv"
        code, output = _run([uv, "venv", "--quiet", "--no-config", "--python", python, str(venv)], cwd=Path(tmp))
        if code != 0:
            print(output)
            return 1
        for version in young:
            ok, output = resolve(Pin("pypi", "boto3", version), uv, None, venv, Path(tmp))
            print(output, end="")
            if not ok:
                print(f"Canary OK: boto3=={version} (younger than 7 days) did not resolve.")
                return 0
    print(f"::error::Safe Chain let boto3 {young} resolve although younger than 7 days; the gate is not enforcing")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["guard", "check", "canary"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--base", help="git revision; check only pins it does not already have")
    parser.add_argument("--python", default=os.environ.get("GATE_PYTHON", sys.executable),
                        help="interpreter for throwaway venvs (not the Safe Chain python shim)")
    args = parser.parse_args(argv)
    if args.command == "guard":
        return cmd_guard(args.root)
    if args.command == "check":
        return cmd_check(args.root, args.python, args.base)
    return cmd_canary(args.python)


if __name__ == "__main__":
    sys.exit(main())
