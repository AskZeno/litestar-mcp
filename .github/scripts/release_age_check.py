#!/usr/bin/env python3
# Copy of AskZeno/infrastructure scripts/release_age_check.py (main
# 87f69eca69a5223283f033352ca2d199316d0ab9, infra#2975 + infra#2982). This repository is public, so
# it cannot call the private repository's reusable workflow; keep this copy in step with infra's
# check. Only runbook hints differ.
"""Fail a pull request that adopts a dependency release younger than the minimum age (7 days).

The org rule is Renovate's `minimumReleaseAge: "7 days"` (AskZeno/renovate-config default.json),
but Renovate only applies it to its own PRs. This check applies it to every PR, human or agent:

* Only references that the PR adds or changes are checked. The script collects the references in
  every changed file at the merge base and at the head, and checks only those whose identity
  (ecosystem, name, version, digest, source) is new at the head. Old pins never fail a PR.
* Covered: container images and digests (YAML `image:`, Helm values `repository`/`tag`,
  kustomize `images:`, explicit registry references in scripts, Dockerfile `FROM` and
  `COPY --from`), Helm charts (kustomize `helmCharts`, HelmChartInflationGenerator, Chart.yaml
  and Chart.lock dependencies, Argo CD chart sources, OCI and HTTP repositories), GitHub Actions
  and reusable workflows (`uses:`), Argo CD git sources pinned to a tag or SHA, OpenTofu and
  Terraform providers (`required_providers`, `.terraform.lock.hcl`), Go modules (`go.mod`),
  PyPI (requirements files, pyproject.toml, uv.lock, poetry.lock, inline `pip`/`uv` installs)
  and npm (package.json, package-lock.json, pnpm-lock.yaml, yarn.lock, inline `npm`/`npx`
  installs, MCP configs).
* Publish dates: OCI image config `created` and `org.opencontainers.image.created`
  annotations (with registry tag times as a fallback), Helm `index.yaml` `created`, GitHub
  commit, tag and release dates, the OpenTofu and Terraform registry APIs, the Go module proxy
  `.info` Time, PyPI `upload_time` and npm `time[version]`.
* Fail closed: a reference whose date cannot be determined fails, except our own images, charts,
  modules and actions (see OWN_* below).
* PyPI and npm only: a version on Aikido's confirmed safe-patch list passes, as Safe Chain
  lets it through (`https://malware-list.aikido.dev/safe_patches.json`). If the list cannot be
  fetched, there are no safe-patch exemptions and the fetch error is shown in the summary.
* Exception: the label `release-age-exception` plus a PR-body line
  `Release-age exception: <advisory or CVE link> <reason>`, with the label added by a repo
  admin or maintainer who is not the PR author. See the release-age-check runbook in AskZeno/infrastructure.

Stdlib plus PyYAML. Runs in .github/workflows/release-age-check.yml through
.github/actions/release-age; unit tests in tests/test_release_age_check.py.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import fnmatch
import gzip
import json
import os
import posixpath
import re
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from typing import Any

import yaml

MIN_AGE = dt.timedelta(days=7)
EXCEPTION_LABEL = "release-age-exception"
EXCEPTION_LINE = re.compile(r"^\s*Release-age exception:\s*(?P<text>.+?)\s*$", re.IGNORECASE | re.MULTILINE)
SAFE_PATCHES_DEFAULT_BASE = "https://malware-list.aikido.dev"
USER_AGENT = "askzeno-release-age-check/1 (+https://github.com/AskZeno/infrastructure)"
MAX_FILE_BYTES = 30 * 1024 * 1024

# Our own artifacts. They are built and bumped by our CI (argocd-image-updater writes the
# `.argocd-source-*.yaml` files with `build: automatic update of ...` commits; publish-*.yml
# workflows push to rg.nl-ams.scw.cloud/zeno), so they must never be blocked.
OWN_IMAGE_PREFIXES = ("ghcr.io/askzeno/", "rg.nl-ams.scw.cloud/zeno/", "rg.fr-par.scw.cloud/zeno/", "rg.pl-waw.scw.cloud/zeno/")
OWN_CHART_PREFIXES = ("oci://ghcr.io/askzeno/", "oci://rg.nl-ams.scw.cloud/zeno", "https://github.com/askzeno/", "https://raw.githubusercontent.com/askzeno/")
OWN_GITHUB_OWNERS = ("askzeno",)
OWN_GO_PREFIXES = ("github.com/askzeno/",)
OWN_NPM_SCOPES = ("@askzeno/",)
OWN_PATH_GLOBS = ("**/.argocd-source-*.yaml", ".argocd-source-*.yaml")

PUBLIC_PYPI = ("", "https://pypi.org/simple", "https://pypi.org/simple/", "https://pypi.org/pypi")
PUBLIC_NPM = ("", "https://registry.npmjs.org", "https://registry.npmjs.org/", "https://registry.yarnpkg.com")

TEMPLATE_MARKERS = ("{{", "${", "$(", "<%")

# --------------------------------------------------------------------------------------------
# Data model


@dataclasses.dataclass(frozen=True)
class Ref:
    """A dependency reference. Equality (the diff identity) ignores `hint`."""

    ecosystem: str  # oci | helm | github | terraform | go | pypi | npm
    name: str
    version: str = ""
    digest: str = ""
    source: str = ""  # Helm repo URL, provider host, PyPI/npm registry
    floor: bool = False  # True when `version` is the lower bound of a range
    hint: str = dataclasses.field(default="", compare=False)  # e.g. `# v7.0.1` after a SHA

    def display(self) -> str:
        if self.ecosystem == "oci":
            text = self.name
            if self.version:
                text += f":{self.version}"
            if self.digest:
                text += f"@{self.digest[:19]}…"
            return text
        if self.ecosystem == "helm":
            return f"{self.source.rstrip('/')}/{self.name}" if self.source else self.name
        if self.ecosystem == "github":
            return self.name
        if self.ecosystem == "terraform":
            return f"{self.source}/{self.name}" if self.source else self.name
        return self.name

    def shown_version(self) -> str:
        if self.ecosystem == "oci":
            return self.version or (self.digest[:19] + "…" if self.digest else "latest")
        text = self.version
        if self.ecosystem == "github" and len(text) == 40 and self.hint:
            text = f"{text[:12]} ({self.hint})"
        elif self.ecosystem == "github" and len(text) == 40:
            text = text[:12]
        return (">= " if self.floor else "") + text


@dataclasses.dataclass(frozen=True)
class Occurrence:
    ref: Ref
    path: str
    line: int


@dataclasses.dataclass
class Lookup:
    published: dt.datetime | None = None
    error: str = ""
    own: bool = False
    note: str = ""
    not_found: bool = False


@dataclasses.dataclass
class Row:
    ref: Ref
    occurrences: list[Occurrence]
    lookup: Lookup
    verdict: str  # pass | own | too-new | unknown | skip | safe-patch | exception
    age: dt.timedelta | None = None

    @property
    def failing(self) -> bool:
        return self.verdict in ("too-new", "unknown")


VERDICT_TEXT = {
    "pass": "✅ pass",
    "own": "✅ own build",
    "skip": "➖ range floor not a release",
    "safe-patch": "✅ Aikido safe patch",
    "too-new": "❌ too new",
    "unknown": "❌ date unknown",
    "exception": "⚠️ exception",
}

# --------------------------------------------------------------------------------------------
# Small helpers


def parse_time(value: Any) -> dt.datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.UTC)
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    text = str(value).strip()
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)  # Go/RFC3339Nano fractions
    text = re.sub(r"Z$", "+00:00", text)
    text = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", text)
    text = text.replace(" ", "T", 1) if re.match(r"\d{4}-\d\d-\d\d \d", text) else text
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def plausible(when: dt.datetime | None, now: dt.datetime) -> bool:
    """Reproducible builds stamp 1970 or 0001; those dates say nothing about the release."""
    return when is not None and when.year >= 2005 and when <= now + dt.timedelta(days=1)


def templated(text: str) -> bool:
    return any(marker in text for marker in TEMPLATE_MARKERS) or bool(re.search(r"\$[A-Za-z_]", text))


def pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pep440_equal(a: str, b: str) -> bool:
    def norm(v: str) -> str:
        v = v.strip().lower().lstrip("v")
        m = re.match(r"^(\d+(?:\.\d+)*)(.*)$", v)
        if not m:
            return v
        release = m.group(1).split(".")
        while len(release) > 1 and release[-1] == "0":
            release.pop()
        return ".".join(str(int(x)) for x in release) + m.group(2).replace("-", "").replace("_", "")

    return norm(a) == norm(b)


def line_of(text: str, needle: str, start: int = 0) -> int:
    idx = text.find(needle, start)
    return text.count("\n", 0, idx) + 1 if idx >= 0 else 1


def human_age(age: dt.timedelta | None) -> str:
    if age is None:
        return "—"
    hours = age.total_seconds() / 3600
    if hours < 0:
        return "in the future"
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{age.days} d {int((age.seconds) // 3600)} h"


# --------------------------------------------------------------------------------------------
# Image references

_IMAGE_RE = re.compile(
    r"^(?P<name>[a-z0-9]+(?:[._-][a-z0-9]+)*(?::\d+(?=/))?(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*)"
    r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?"
    r"(?:@(?P<digest>sha256:[a-f0-9]{64}))?$"
)


def _has_registry_host(reference: str) -> bool:
    first, sep, _ = reference.strip().strip("'\"").removeprefix("docker://").partition("/")
    return bool(sep) and ("." in first or ":" in first or first == "localhost")


def normalize_image_name(name: str) -> str:
    first, _, rest = name.partition("/")
    if not rest or ("." not in first and ":" not in first and first != "localhost"):
        # Docker Hub
        repo = name if rest else f"library/{name}"
        return f"docker.io/{repo}"
    if first in ("index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"):
        return f"docker.io/{rest}"
    return name


def parse_image(text: str, hint: str = "") -> Ref | None:
    value = text.strip().strip("'\"")
    if not value or templated(value) or " " in value:
        return None
    reference = value.removeprefix("docker://")
    m = _IMAGE_RE.match(reference)
    if not m:
        return None
    name = m.group("name")
    if "/" not in name and "." in name:
        return None  # "foo.bar" alone is not an image
    return Ref("oci", normalize_image_name(name), m.group("tag") or "", m.group("digest") or "", hint=hint)


def image_from_parts(registry: str, repository: str, tag: str, digest: str) -> Ref | None:
    repository = (repository or "").strip()
    tag = str(tag or "").strip().strip("'\"")
    digest = str(digest or "").strip()
    if not repository or templated(repository) or templated(tag) or templated(digest):
        return None
    if "@" in tag:
        tag, _, tag_digest = tag.partition("@")
        digest = digest or tag_digest
    if tag.startswith("sha256:"):
        digest, tag = tag, ""
    if not tag and not digest:
        return None  # chart default (appVersion); the pin is not in this file
    name = f"{registry.strip().rstrip('/')}/{repository}" if registry and registry.strip() else repository
    text = name + (f":{tag}" if tag else "") + (f"@{digest}" if digest else "")
    return parse_image(text)


# --------------------------------------------------------------------------------------------
# Scanners. Each takes (path, text) and returns Occurrences.


def _mapping(node: yaml.Node) -> dict[str, yaml.Node]:
    return {k.value: v for k, v in node.value if isinstance(k, yaml.ScalarNode)}


def _scalar(node: yaml.Node | None) -> str | None:
    if isinstance(node, yaml.ScalarNode) and node.tag != "tag:yaml.org,2002:null":
        return str(node.value)
    return None


def _line(node: yaml.Node) -> int:
    return node.start_mark.line + 1


_USES_RE = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?P<path>/[^@\s]*)?@(?P<ref>[A-Za-z0-9_./-]+)$")
_VERSION_COMMENT = re.compile(r"#\s*(?:tag[:=]?\s*)?(v?\d+(?:\.\d+)*[\w.+-]*)")


def parse_uses(value: str, hint: str = "") -> Ref | None:
    value = value.strip()
    if value.startswith("./") or templated(value):
        return None
    if value.startswith("docker://"):
        return parse_image(value)
    m = _USES_RE.match(value)
    if not m:
        return None
    return Ref("github", m.group("repo"), m.group("ref"), hint=hint)


_GITHUB_REPO_URL = re.compile(r"^(?:https?://|git@)github\.com[/:](?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?/?$")
_RELEASE_REV = re.compile(r"^(v?\d+(\.\d+)*([-+.][\w.]+)?|[0-9a-f]{40})$")


def helm_ref(chart: str, repo: str, version: str) -> Ref | None:
    chart, repo, version = (chart or "").strip(), (repo or "").strip(), str(version or "").strip()
    if not chart or not version or templated(chart + repo + version):
        return None
    if repo and "://" not in repo:
        repo = "oci://" + repo  # Argo CD writes OCI Helm repos without a scheme
    if repo.startswith("file://") or repo.startswith("@") or repo.startswith("alias:"):
        return None
    floor_version = range_floor(version)
    if floor_version is None:
        return None
    return Ref("helm", chart, floor_version, source=repo.rstrip("/"), floor=not _is_exact(version))


def _is_exact(version: str) -> bool:
    return bool(re.match(r"^=?\s*v?\d+(\.\d+)*([-+][\w.+-]+)?$", version.strip()))


def range_floor(spec: str) -> str | None:
    """Exact version, or the lower bound of a range (`^1.2.3`, `~> 2.60.0`, `>= 1.2`)."""
    spec = spec.strip().strip("'\"")
    if not spec or spec in ("*", "latest", "x"):
        return None
    if _is_exact(spec):
        return spec.lstrip("=").strip()
    first = re.split(r"[,|]|\s+(?=[<>=~^!])", spec)[0].strip()
    m = re.match(r"^(\^|~>|~=|~|>=|=>|==)\s*v?(\d+(?:\.\d+)*(?:[-+][\w.+-]+)?)$", first)
    if not m:
        return None
    return m.group(2)


def scan_yaml(path: str, text: str) -> list[Occurrence]:
    out: list[Occurrence] = []
    base = posixpath.basename(path)
    in_github = path.startswith(".github/") or "/.github/" in path or base in ("action.yml", "action.yaml")
    try:
        docs = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
    except yaml.YAMLError:
        docs = None
    lines = text.splitlines()

    def hint_for(node: yaml.Node) -> str:
        line = lines[node.start_mark.line] if node.start_mark.line < len(lines) else ""
        m = _VERSION_COMMENT.search(line[node.end_mark.column :] if node.end_mark.line == node.start_mark.line else "")
        return m.group(1) if m else ""

    def add(ref: Ref | None, node: yaml.Node) -> None:
        if ref is not None:
            out.append(Occurrence(ref, path, _line(node)))

    def handle(m: dict[str, yaml.Node], node: yaml.MappingNode, parent_key: str | None, kind: str | None) -> None:
        uses = _scalar(m.get("uses"))
        if uses:
            add(parse_uses(uses, hint_for(m["uses"])), m["uses"])
        image = m.get("image")
        if _scalar(image):
            raw = _scalar(image) or ""
            ref = parse_image(raw)
            # In workflows `image:` is often a matrix variable (`image: dbos-ops`); there only a
            # value with a tag, digest or registry host is an image.
            if ref and in_github and not (ref.version or ref.digest or _has_registry_host(raw)):
                ref = None
            add(ref, image)
        images = m.get("images")
        if isinstance(images, yaml.SequenceNode):
            for item in images.value:
                if _scalar(item):
                    add(parse_image(_scalar(item) or ""), item)
        if "repository" in m and ("tag" in m or "digest" in m) and "version" not in m:
            ref = image_from_parts(_scalar(m.get("registry")) or "", _scalar(m.get("repository")) or "", _scalar(m.get("tag")) or "", _scalar(m.get("digest")) or "")
            add(ref, m.get("tag") or m.get("digest") or node)
        if parent_key == "image" and "name" in m and "tag" in m and "repository" not in m:
            ref = image_from_parts(_scalar(m.get("registry")) or "", _scalar(m.get("name")) or "", _scalar(m.get("tag")) or "", _scalar(m.get("digest")) or "")
            add(ref, m["tag"])
        if parent_key == "images" and "name" in m and any(k in m for k in ("newName", "newTag", "digest")):
            name = _scalar(m.get("newName")) or _scalar(m.get("name")) or ""
            ref = image_from_parts("", name, _scalar(m.get("newTag")) or "", _scalar(m.get("digest")) or "")
            add(ref, m.get("newTag") or m.get("digest") or m.get("newName") or node)
        if parent_key == "helmCharts" and "name" in m and "version" in m:
            add(helm_ref(_scalar(m.get("name")) or "", _scalar(m.get("repo")) or "", _scalar(m.get("version")) or ""), m["version"])
        if kind == "HelmChartInflationGenerator" and parent_key is None:
            version_node = m.get("chartVersion") or m.get("version")
            if version_node is not None:
                chart = _scalar(m.get("chartName")) or _scalar(m.get("name")) or ""
                repo = _scalar(m.get("chartRepoUrl")) or _scalar(m.get("repo")) or ""
                add(helm_ref(chart, repo, _scalar(version_node) or ""), version_node)
        if parent_key == "dependencies" and base in ("Chart.yaml", "Chart.lock") and "name" in m and "version" in m:
            add(helm_ref(_scalar(m.get("name")) or "", _scalar(m.get("repository")) or "", _scalar(m.get("version")) or ""), m["version"])
        if "repoURL" in m and "targetRevision" in m:
            repo_url = _scalar(m.get("repoURL")) or ""
            rev = _scalar(m.get("targetRevision")) or ""
            chart = _scalar(m.get("chart"))
            if chart:
                add(helm_ref(chart, repo_url, rev), m["targetRevision"])
            else:
                gh = _GITHUB_REPO_URL.match(repo_url.strip())
                if gh and _RELEASE_REV.match(rev.strip()) and not templated(rev):
                    add(Ref("github", gh.group("repo"), rev.strip()), m["targetRevision"])

    def walk(node: yaml.Node, parent_key: str | None, kind: str | None, top: bool) -> None:
        if isinstance(node, yaml.MappingNode):
            m = _mapping(node)
            handle(m, node, None if top else parent_key, kind)
            for k, v in node.value:
                walk(v, k.value if isinstance(k, yaml.ScalarNode) else None, kind, False)
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                walk(item, parent_key, kind, False)

    if docs is not None:
        for doc in docs:
            if doc is None:
                continue
            kind = _scalar(_mapping(doc).get("kind")) if isinstance(doc, yaml.MappingNode) else None
            walk(doc, None, kind, True)
    else:
        # Helm templates and other YAML that does not parse: line patterns only.
        for number, line in enumerate(lines, 1):
            m = re.match(r"^\s*-?\s*image:\s*['\"]?([^\s'\"#]+)", line)
            if m:
                ref = parse_image(m.group(1))
                if ref:
                    out.append(Occurrence(ref, path, number))
            m = re.match(r"^\s*-?\s*uses:\s*['\"]?([^\s'\"#]+)['\"]?\s*(#.*)?$", line)
            if m:
                hint = _VERSION_COMMENT.search(m.group(2) or "")
                ref = parse_uses(m.group(1), hint.group(1) if hint else "")
                if ref:
                    out.append(Occurrence(ref, path, number))
    if base == "pnpm-lock.yaml":
        out += scan_pnpm_lock(path, text)
    out += scan_command_lines(path, text, dockerfile=False)
    return out


# ---- shell-like command lines (workflow `run:`, Dockerfile RUN, shell scripts, Makefiles)


def logical_lines(text: str) -> list[list[tuple[int, str]]]:
    groups: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.rstrip()
        current.append((number, stripped[:-1] if stripped.endswith("\\") else stripped))
        if not stripped.endswith("\\"):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


_PIP_CONTEXT = re.compile(r"(?<![\w-])(pip3?|pipx|uv|uvx|poetry|pdm|python3?\s+-m\s+pip)(?![\w-])")
_PIP_PIN = re.compile(r"(?<![\w.\-=/@])([A-Za-z0-9][A-Za-z0-9._-]*)(\[[A-Za-z0-9_,.\- ]*\])?\s*(===?|>=|~=)\s*([0-9][A-Za-z0-9.+!_-]*)")
_UVX_START = re.compile(r"(?<![\w-])(?:uvx|uv\s+tool\s+(?:install|run))(?![\w-])")
_UVX_SPEC = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?@([0-9][A-Za-z0-9.+!_-]*)$")


def uvx_specs(code: str) -> list[tuple[str, str]]:
    """`uvx [options] pkg@1.2.3` and `uv tool install|run [options] pkg@1.2.3` (the first such token)."""
    found = []
    for start in _UVX_START.finditer(code):
        for token in code[start.end() :].split():
            m = _UVX_SPEC.match(_quote_strip(token))
            if m:
                found.append((m.group(1), m.group(2)))
                break
    return found
_NPM_CONTEXT = re.compile(r"(?<![\w-])(npm|npx|pnpm|yarn|bunx|bun|corepack)(?![\w-])")
_NPM_PIN = re.compile(r"(?<![\w/.@-])((?:@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*)@([~^]?\d+\.\d+\.\d+[\w.+-]*)")
_IMAGE_CONTEXT = re.compile(r"(?i)(?<![\w-])(image|images|docker|podman|nerdctl|crane|skopeo|oras|cosign|trivy|grype|syft|kind|from|container|registry|newname|ctr|helm)(?![\w-])")
_EXPLICIT_IMAGE = re.compile(
    r"(?<![\w./:@-])((?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?/[a-z0-9][a-z0-9._/-]*)"
    r"(?::([A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?(?:@(sha256:[a-f0-9]{64}))?(?![\w./@:-])"
)
_NOT_REGISTRIES = ("github.com", "githubusercontent.com", "gitlab.com", "pypi.org", "npmjs.org", "golang.org", "kubernetes.io", "argoproj.io", "zeno.law", "svc", "local", "internal", "example.com")
_DOCKER_RUN = re.compile(r"(?<![\w-])(?:docker|podman|nerdctl)\s+(?:container\s+|image\s+)?(run|create|pull)\s+(.*)$")
_DOCKER_BOOL_FLAGS = {"-d", "-i", "-t", "-it", "-ti", "--rm", "--detach", "--interactive", "--tty", "--init", "--privileged", "-q", "--quiet", "--read-only", "-P", "--publish-all", "--all-tags", "-a"}


def _quote_strip(token: str) -> str:
    return token.strip().strip("'\"").rstrip(";,)").strip("'\"")


def scan_command_lines(path: str, text: str, dockerfile: bool, args: dict[str, str] | None = None) -> list[Occurrence]:
    out: list[Occurrence] = []
    for group in logical_lines(text):
        segments = [(n, _substitute(s, args) if args else s) for n, s in group]
        joined = " ".join(s for _, s in segments)
        if joined.lstrip().startswith("#"):
            continue
        pip = bool(_PIP_CONTEXT.search(joined))
        npm = bool(_NPM_CONTEXT.search(joined))
        image_ctx = bool(_IMAGE_CONTEXT.search(joined))
        for number, segment in segments:
            code = re.sub(r"(^|\s)#.*$", "", segment) if not dockerfile or not segment.lstrip().startswith("#") else ""
            if pip:
                for m in _PIP_PIN.finditer(code):
                    name, op, version = m.group(1), m.group(3), m.group(4).rstrip(".'\"")
                    if templated(m.group(0)) or name.lower() in ("python", "version"):
                        continue
                    out.append(Occurrence(Ref("pypi", pep503(name), version, floor=op in (">=", "~=")), path, number))
                for name, version in uvx_specs(code):
                    out.append(Occurrence(Ref("pypi", pep503(name), version), path, number))
            if npm:
                for m in _NPM_PIN.finditer(code):
                    version = m.group(2)
                    out.append(Occurrence(Ref("npm", m.group(1), version.lstrip("^~"), floor=version[0] in "^~"), path, number))
            for m in _EXPLICIT_IMAGE.finditer(code):
                host = m.group(1).split("/", 1)[0].split(":", 1)[0]
                if not (m.group(2) or m.group(3)) or any(host == d or host.endswith("." + d) for d in _NOT_REGISTRIES):
                    continue
                if not (image_ctx or m.group(3)):
                    continue
                ref = parse_image(m.group(0))
                if ref:
                    out.append(Occurrence(ref, path, number))
        docker = _DOCKER_RUN.search(joined)
        if docker:
            tokens = docker.group(2).split()
            i = 0
            while i < len(tokens):
                token = _quote_strip(tokens[i])
                if token.startswith("-"):
                    if "=" in token or token in _DOCKER_BOOL_FLAGS:
                        i += 1
                    else:
                        i += 2
                    continue
                ref = parse_image(token)
                if ref and (ref.version or ref.digest) and not templated(token):
                    number = next((n for n, s in segments if token in s), segments[0][0])
                    out.append(Occurrence(ref, path, number))
                break
    return out


def _substitute(text: str, args: dict[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(3)
        if name in args:
            return args[name]
        return m.group(2) if m.group(2) is not None else m.group(0)

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)", repl, text)


def scan_dockerfile(path: str, text: str) -> list[Occurrence]:
    out: list[Occurrence] = []
    args: dict[str, str] = {}
    for m in re.finditer(r"(?im)^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"]*\"|'[^']*'|\S+)", text):
        args[m.group(1)] = m.group(2).strip("'\"")
    stages: set[str] = set()
    for group in logical_lines(text):
        number = group[0][0]
        joined = " ".join(s for _, s in group).strip()
        m = re.match(r"(?i)^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", joined)
        if m:
            image = _substitute(m.group(1), args)
            if m.group(2):
                stages.add(m.group(2).lower())
            if image.lower() in stages or image.lower() == "scratch":
                continue
            ref = parse_image(image)
            if ref:
                out.append(Occurrence(ref, path, number))
            continue
        for cm in re.finditer(r"--from=(\S+)", joined):
            if re.match(r"(?i)^(COPY|ADD|RUN)\b", joined):
                value = _substitute(cm.group(1), args)
                if value.lower() in stages or value.isdigit():
                    continue
                ref = parse_image(value)
                if ref and (ref.version or ref.digest or "/" in value):
                    out.append(Occurrence(ref, path, number))
    out += scan_command_lines(path, text, dockerfile=True, args=args)
    return out


# ---- Terraform / OpenTofu


def scan_terraform_lock(path: str, text: str) -> list[Occurrence]:
    out = []
    for m in re.finditer(r'provider\s+"([^"]+)"\s*\{(.*?)\n\}', text, re.DOTALL):
        vm = re.search(r'\bversion\s*=\s*"([^"]+)"', m.group(2))
        if not vm:
            continue
        address = m.group(1)
        host, _, rest = address.partition("/")
        line = text.count("\n", 0, m.start(2) + vm.start()) + 1
        out.append(Occurrence(Ref("terraform", rest, vm.group(1), source=host), path, line))
    return out


def scan_terraform(path: str, text: str) -> list[Occurrence]:
    out: list[Occurrence] = []
    for block in re.finditer(r"required_providers\s*\{", text):
        start = block.end()
        depth, i = 1, start
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        body = text[start : i - 1]
        for obj in re.finditer(r"[\w-]+\s*=\s*\{([^{}]*)\}", body):
            src = re.search(r'\bsource\s*=\s*"([^"]+)"', obj.group(1))
            ver = re.search(r'\bversion\s*=\s*"([^"]+)"', obj.group(1))
            if not src or not ver:
                continue
            floor = range_floor(ver.group(1))
            if floor is None:
                continue
            if re.fullmatch(r"\d+(\.\d+)?", floor):
                floor += ".0" * (2 - floor.count("."))  # `~> 2.70` allows 2.70.0 and later
            parts = src.group(1).split("/")
            host, name = (parts[0], "/".join(parts[1:])) if len(parts) == 3 else ("", src.group(1))
            line = text.count("\n", 0, start + obj.start(1) + ver.start()) + 1
            out.append(Occurrence(Ref("terraform", name.lower(), floor, source=host.lower(), floor=not _is_exact(ver.group(1))), path, line))
    out += scan_command_lines(path, text, dockerfile=False)
    return out


# ---- Go


def scan_go_mod(path: str, text: str) -> list[Occurrence]:
    out = []
    in_block = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if re.match(r"^(require|replace)\s*\($", line):
            in_block = line.split()[0]
            continue
        if line == ")":
            in_block = None
            continue
        spec = None
        if in_block == "require":
            spec = line
        elif in_block == "replace" or line.startswith("replace "):
            target = line.split("=>", 1)[1].strip() if "=>" in line else ""
            spec = target if len(target.split()) == 2 else None
        elif line.startswith("require "):
            spec = line[len("require ") :].strip()
        if spec:
            parts = spec.split()
            if len(parts) >= 2 and parts[1].startswith("v"):
                out.append(Occurrence(Ref("go", parts[0], parts[1]), path, number))
    return out


# ---- Python


def _pep508(spec: str) -> tuple[str, str, bool] | None:
    m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*\(?\s*([^;()]*)\)?\s*(;.*)?$", spec)
    if not m or "@" in (m.group(3) or ""):
        return None
    constraint = (m.group(3) or "").strip()
    if not constraint:
        return None
    first = constraint.split(",")[0].strip()
    em = re.match(r"^(===?|>=|~=)\s*([0-9][A-Za-z0-9.+!_-]*)$", first)
    if not em or "*" in em.group(2):
        # `>=1,<2` -> first part; anything else (`<2`, `!=`) has no floor
        return None
    return pep503(m.group(1)), em.group(2), em.group(1) in (">=", "~=")


def scan_requirements(path: str, text: str) -> list[Occurrence]:
    out = []
    for group in logical_lines(text):
        number, first = group[0]
        line = " ".join(s for _, s in group).split(" #", 1)[0].strip()
        if not line or line.startswith(("#", "-", "git+", "http")):
            continue
        line = re.split(r"\s+--hash", line)[0]
        parsed = _pep508(line)
        if parsed:
            out.append(Occurrence(Ref("pypi", parsed[0], parsed[1], floor=parsed[2]), path, number))
    return out


def scan_pyproject(path: str, text: str) -> list[Occurrence]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    specs: list[str] = []
    project = data.get("project", {})
    specs += project.get("dependencies", []) or []
    for group in (project.get("optional-dependencies") or {}).values():
        specs += group
    for group in (data.get("dependency-groups") or {}).values():
        specs += [s for s in group if isinstance(s, str)]
    specs += (data.get("build-system") or {}).get("requires", []) or []
    uv = (data.get("tool") or {}).get("uv") or {}
    for key in ("dev-dependencies", "constraint-dependencies", "override-dependencies"):
        specs += uv.get(key, []) or []
    out = []
    for spec in specs:
        if not isinstance(spec, str):
            continue
        parsed = _pep508(spec)
        if parsed:
            out.append(Occurrence(Ref("pypi", parsed[0], parsed[1], floor=parsed[2]), path, line_of(text, spec)))
    return out


def scan_python_lock(path: str, text: str) -> list[Occurrence]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    out = []
    package_lines: dict[tuple[str, str], int] = {}
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        m = re.match(r'^name = "([^"]+)"$', line)
        v = re.match(r'^version = "([^"]+)"$', lines[number]) if m and number < len(lines) else None
        if m and v:
            package_lines.setdefault((m.group(1), v.group(1)), number)
    for pkg in data.get("package", []) or []:
        name, version = pkg.get("name"), pkg.get("version")
        if not name or not version:
            continue
        source = pkg.get("source") or {}
        if isinstance(source, dict) and any(k in source for k in ("editable", "virtual", "path", "directory", "git")):
            continue
        registry = source.get("registry", "") if isinstance(source, dict) else ""
        if source.get("type") in ("git", "directory", "file", "url") if isinstance(source, dict) else False:
            continue
        registry = source.get("url", registry) if isinstance(source, dict) and source.get("type") == "legacy" else registry
        line = package_lines.get((name, version), 1)
        out.append(Occurrence(Ref("pypi", pep503(name), version, source=registry.rstrip("/")), path, line))
    return out


# ---- npm


def _npm_spec(name: str, spec: str) -> Ref | None:
    if not isinstance(spec, str):
        return None
    if spec.startswith("npm:"):
        alias = spec[4:]
        at = alias.rfind("@")
        if at <= 0:
            return None
        name, spec = alias[:at], alias[at + 1 :]
    if re.match(r"^(workspace|file|link|git|git\+|github|http|https|portal|patch):", spec) or "/" in spec:
        return None
    floor = range_floor(spec)
    if floor is None:
        return None
    return Ref("npm", name, floor, floor=not _is_exact(spec))


def scan_package_json(path: str, text: str) -> list[Occurrence]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out = []
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (data.get(section) or {}).items():
            ref = _npm_spec(name, spec)
            if ref:
                out.append(Occurrence(ref, path, line_of(text, f'"{name}"')))
    pm = data.get("packageManager")
    if isinstance(pm, str) and "@" in pm:
        name, _, version = pm.partition("@")
        out.append(Occurrence(Ref("npm", name, version.split("+")[0]), path, line_of(text, '"packageManager"')))
    return out


def _json_key_lines(text: str) -> dict[str, int]:
    """First line of every `"key": {` in a JSON document, in one pass."""
    lines: dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), 1):
        m = re.match(r'^\s*"([^"]+)":\s*\{', line)
        if m:
            lines.setdefault(m.group(1), number)
    return lines


def scan_package_lock(path: str, text: str) -> list[Occurrence]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out = []
    key_lines = _json_key_lines(text)
    for key, entry in (data.get("packages") or {}).items():
        if not key or entry.get("link") or "version" not in entry:
            continue
        name = entry.get("name") or key.rsplit("node_modules/", 1)[-1]
        host = urllib.parse.urlsplit(entry.get("resolved", "")).hostname or ""
        source = "" if host in ("", "registry.npmjs.org") else host
        out.append(Occurrence(Ref("npm", name, entry["version"], source=source), path, key_lines.get(key, 1)))

    def legacy(deps: dict[str, Any]) -> None:
        for name, entry in deps.items():
            if isinstance(entry, dict) and "version" in entry and not str(entry["version"]).startswith(("file:", "git")):
                out.append(Occurrence(Ref("npm", name, entry["version"]), path, key_lines.get(name, 1)))
                legacy(entry.get("dependencies") or {})

    if not data.get("packages"):
        legacy(data.get("dependencies") or {})
    return out


def scan_pnpm_lock(path: str, text: str) -> list[Occurrence]:
    out = []
    in_packages = False
    for number, line in enumerate(text.splitlines(), 1):
        if re.match(r"^\S", line):
            in_packages = line.rstrip() == "packages:"
            continue
        if not in_packages:
            continue
        m = re.match(r"^  ['\"]?/?((?:@[^/@\s]+/)?[^@\s/'\"]+)[@/](\d[^()'\":\s]*)", line)
        if m:
            out.append(Occurrence(Ref("npm", m.group(1), m.group(2)), path, number))
    return out


def scan_yarn_lock(path: str, text: str) -> list[Occurrence]:
    out = []
    header: tuple[str, int] | None = None
    for number, line in enumerate(text.splitlines(), 1):
        if line and not line.startswith((" ", "#")) and line.rstrip().endswith(":"):
            spec = line.rstrip(":").split(",")[0].strip().strip('"')
            at = spec.rfind("@")
            header = (spec[:at], number) if at > 0 else None
            if header and header[0] == "__metadata":
                header = None
            continue
        m = re.match(r'^\s+version:?\s+"?([^"\s]+)"?', line)
        if m and header:
            out.append(Occurrence(Ref("npm", header[0], m.group(1)), path, number))
            header = None
    return out


def scan_mcp_config(path: str, text: str) -> list[Occurrence]:
    servers: dict[str, Any] = {}
    try:
        data = tomllib.loads(text) if path.endswith(".toml") else json.loads(text)
    except (tomllib.TOMLDecodeError, json.JSONDecodeError):
        return []
    for key in ("mcpServers", "servers", "mcp_servers"):
        if isinstance(data.get(key), dict):
            servers.update(data[key])
    out = []
    for server in servers.values():
        if not isinstance(server, dict):
            continue
        command = posixpath.basename(str(server.get("command", "")))
        args = [a for a in server.get("args", []) or [] if isinstance(a, str)]
        line = " ".join([command, *args])
        for occ in scan_command_lines(path, line, dockerfile=False):
            needle = occ.ref.name if occ.ref.ecosystem != "oci" else occ.ref.name.split("/")[-1]
            out.append(dataclasses.replace(occ, line=line_of(text, f"{needle}@") if occ.ref.ecosystem != "oci" else line_of(text, needle)))
    return out


# ---- dispatch


def scanners_for(path: str) -> list[Callable[[str, str], list[Occurrence]]]:
    base = posixpath.basename(path)
    lower = base.lower()
    if lower.endswith((".md", ".py")):
        return []
    if base == ".terraform.lock.hcl":
        return [scan_terraform_lock]
    if lower.endswith((".tf", ".tofu")):
        return [scan_terraform]
    if base == "go.mod":
        return [scan_go_mod]
    if base in ("uv.lock", "poetry.lock", "pdm.lock", "pylock.toml"):
        return [scan_python_lock]
    if base == "pyproject.toml":
        return [scan_pyproject]
    if re.match(r"^.*(requirements|constraints).*\.(txt|in)$", lower):
        return [scan_requirements]
    if base in ("package-lock.json", "npm-shrinkwrap.json"):
        return [scan_package_lock]
    if base == "package.json":
        return [scan_package_json]
    if base == "yarn.lock":
        return [scan_yarn_lock]
    if "mcp" in lower and lower.endswith(".json") or path.endswith(".codex/config.toml"):
        return [scan_mcp_config]
    if lower in ("dockerfile", "containerfile") or lower.startswith("dockerfile.") or lower.endswith((".dockerfile", ".containerfile")):
        return [scan_dockerfile]
    if lower.endswith((".yaml", ".yml")):
        return [scan_yaml]
    if lower.endswith((".sh", ".bash")) or base in ("Makefile", "Justfile", "justfile") or lower.endswith(".mk"):
        return [lambda p, t: scan_command_lines(p, t, dockerfile=False)]
    return []


def scan_file(path: str, text: str) -> list[Occurrence]:
    seen: set[tuple[Ref, int]] = set()
    out: list[Occurrence] = []
    for scanner in scanners_for(path):
        for occ in scanner(path, text):
            if (occ.ref, occ.line) not in seen:
                seen.add((occ.ref, occ.line))
                out.append(occ)
    return out


# --------------------------------------------------------------------------------------------
# Git diff


def git(*args: str, cwd: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def git_show(rev: str, path: str, cwd: str | None = None) -> str | None:
    proc = subprocess.run(["git", "cat-file", "-s", f"{rev}:{path}"], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0 or int(proc.stdout.strip() or 0) > MAX_FILE_BYTES:
        return None
    blob = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=cwd, capture_output=True)
    if blob.returncode != 0 or b"\0" in blob.stdout[:8000]:
        return None
    return blob.stdout.decode("utf-8", errors="replace")


def changed_files(base: str, head: str, cwd: str | None = None) -> tuple[list[str], list[str]]:
    """(paths to read at base, paths to read at head)."""
    base_paths, head_paths = [], []
    for line in git("diff", "--name-status", "-M", base, head, cwd=cwd).splitlines():
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R") or status.startswith("C"):
            base_paths.append(parts[1])
            head_paths.append(parts[2])
        elif status == "D":
            base_paths.append(parts[1])
        elif status == "A":
            head_paths.append(parts[1])
        else:
            base_paths.append(parts[1])
            head_paths.append(parts[1])
    return base_paths, head_paths


def new_references(base: str, head: str, cwd: str | None = None) -> list[Occurrence]:
    base_paths, head_paths = changed_files(base, head, cwd)
    old: set[Ref] = set()
    for path in base_paths:
        text = git_show(base, path, cwd) if scanners_for(path) else None
        if text is not None:
            old |= {o.ref for o in scan_file(path, text)}
    new: list[Occurrence] = []
    for path in head_paths:
        text = git_show(head, path, cwd) if scanners_for(path) else None
        if text is not None:
            new += [o for o in scan_file(path, text) if o.ref not in old]
    return new


# --------------------------------------------------------------------------------------------
# HTTP


class _StripAuthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


class HttpError(Exception):
    def __init__(self, status: int, url: str, headers: Any = None, body: bytes = b""):
        super().__init__(f"HTTP {status} from {url}")
        self.status, self.url, self.headers, self.body = status, url, headers, body


class Http:
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_StripAuthRedirect)
        self.cache: dict[tuple[str, str], Any] = {}
        self.lock = threading.Lock()

    def get(self, url: str, headers: dict[str, str] | None = None, *, method: str = "GET") -> tuple[bytes, Any]:
        headers = {"User-Agent": USER_AGENT, **(headers or {})}
        last: Exception | None = None
        for attempt in range(4):
            req = urllib.request.Request(url, headers=headers, method=method)
            try:
                with self.opener.open(req, timeout=self.timeout) as resp:
                    body = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip" or body[:2] == b"\x1f\x8b":
                        body = gzip.decompress(body)
                    return body, resp.headers
            except urllib.error.HTTPError as err:
                body = err.read() if hasattr(err, "read") else b""
                if err.code in (429, 500, 502, 503, 504) and attempt < 3:
                    last = HttpError(err.code, url, err.headers, body)
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise HttpError(err.code, url, err.headers, body) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
                last = err
                time.sleep(1.5 * (attempt + 1))
        raise HttpError(0, url, None, str(last).encode())

    def json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        key = (url, json.dumps(headers or {}, sort_keys=True))
        with self.lock:
            if key in self.cache:
                return self.cache[key]
        body, _ = self.get(url, headers)
        value = json.loads(body)
        with self.lock:
            self.cache[key] = value
        return value


# --------------------------------------------------------------------------------------------
# Publish-date resolvers


OCI_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
CREATED_KEYS = ("org.opencontainers.image.created", "org.label-schema.build-date")


class Resolver:
    def __init__(self, http: Http, now: dt.datetime, github_token: str = "", env: dict[str, str] | None = None):
        self.http, self.now, self.github_token = http, now, github_token
        self.env = env if env is not None else dict(os.environ)
        self.tokens: dict[str, str] = {}
        self.lock = threading.Lock()
        self._helm_index: dict[str, Any] = {}

    # ---- dispatch

    def resolve(self, ref: Ref, path: str = "") -> Lookup:
        if is_own(ref, path):
            return Lookup(own=True)
        try:
            fn = getattr(self, f"_{ref.ecosystem}")
            return fn(ref)
        except HttpError as err:
            if err.status == 404:
                return Lookup(error=f"not found ({err.url})", not_found=True)
            return Lookup(error=str(err))
        except Exception as err:  # noqa: BLE001 - fail closed with the reason
            return Lookup(error=f"{type(err).__name__}: {err}")

    # ---- OCI

    def _registry_host(self, host: str) -> str:
        return "registry-1.docker.io" if host == "docker.io" else host

    def _oci_request(self, host: str, repo: str, path: str, accept: str = OCI_ACCEPT) -> tuple[bytes, Any]:
        api = self._registry_host(host)
        url = f"https://{api}/v2/{repo}/{path}"
        key = f"{api}/{repo}"
        headers = {"Accept": accept}
        if key in self.tokens:
            headers["Authorization"] = f"Bearer {self.tokens[key]}"
        try:
            return self.http.get(url, headers)
        except HttpError as err:
            if err.status != 401 or not err.headers:
                raise
            challenge = err.headers.get("WWW-Authenticate", "")
            if not challenge.lower().startswith("bearer"):
                raise
            params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
            realm = params.pop("realm", "")
            params.setdefault("scope", f"repository:{repo}:pull")
            auth_headers = {}
            if api == "registry-1.docker.io" and self.env.get("DOCKERHUB_USERNAME") and self.env.get("DOCKERHUB_TOKEN"):
                import base64

                creds = base64.b64encode(f"{self.env['DOCKERHUB_USERNAME']}:{self.env['DOCKERHUB_TOKEN']}".encode()).decode()
                auth_headers["Authorization"] = f"Basic {creds}"
            body, _ = self.http.get(f"{realm}?{urllib.parse.urlencode(params)}", auth_headers)
            token_data = json.loads(body)
            token = token_data.get("token") or token_data.get("access_token")
            with self.lock:
                self.tokens[key] = token
            headers["Authorization"] = f"Bearer {token}"
            return self.http.get(url, headers)

    def _oci_dates(self, host: str, repo: str, reference: str) -> list[dt.datetime]:
        body, _ = self._oci_request(host, repo, f"manifests/{reference}")
        manifest = json.loads(body)
        dates = [parse_time((manifest.get("annotations") or {}).get(k)) for k in CREATED_KEYS]
        if "manifests" in manifest:  # index: pick linux/amd64, else the first image
            children = [m for m in manifest["manifests"] if (m.get("platform") or {}).get("os") != "unknown"]
            chosen = next((m for m in children if (m.get("platform") or {}).get("architecture") == "amd64" and (m.get("platform") or {}).get("os") == "linux"), children[0] if children else None)
            if chosen is None:
                return [d for d in dates if d]
            dates += [parse_time((chosen.get("annotations") or {}).get(k)) for k in CREATED_KEYS]
            body, _ = self._oci_request(host, repo, f"manifests/{chosen['digest']}")
            manifest = json.loads(body)
            dates += [parse_time((manifest.get("annotations") or {}).get(k)) for k in CREATED_KEYS]
        config = manifest.get("config") or {}
        media = config.get("mediaType", "")
        if config.get("digest") and ("image.config" in media or "container.image" in media):
            cbody, _ = self._oci_request(host, repo, f"blobs/{config['digest']}", accept="*/*")
            cfg = json.loads(cbody)
            dates.append(parse_time(cfg.get("created")))
            labels = (cfg.get("config") or {}).get("Labels") or {}
            dates += [parse_time(labels.get(k)) for k in CREATED_KEYS]
        return [d for d in dates if d]

    def _oci_tag_time(self, host: str, repo: str, tag: str, digest: str) -> dt.datetime | None:
        if host == "docker.io" and tag:
            ns, _, name = repo.partition("/")
            data = self.http.json(f"https://hub.docker.com/v2/namespaces/{ns}/repositories/{name}/tags/{urllib.parse.quote(tag)}")
            return parse_time(data.get("tag_last_pushed") or data.get("last_updated"))
        if host == "quay.io" and tag:
            data = self.http.json(f"https://quay.io/api/v1/repository/{repo}/tag/?specificTag={urllib.parse.quote(tag)}&onlyActiveTags=true")
            tags = data.get("tags") or []
            return parse_time(tags[0].get("start_ts")) if tags else None
        if host == "gcr.io" or host.endswith((".gcr.io", "-docker.pkg.dev")) or host == "registry.k8s.io":
            body, _ = self._oci_request(host, repo, "tags/list", accept="application/json")
            data = json.loads(body)
            for dgst, info in (data.get("manifest") or {}).items():
                if (digest and dgst == digest) or (tag and tag in info.get("tag", [])):
                    return parse_time(int(info.get("timeUploadedMs") or info.get("timeCreatedMs")) / 1000)
        return None

    def _oci(self, ref: Ref) -> Lookup:
        host, _, repo = ref.name.partition("/")
        reference = ref.digest or ref.version or "latest"
        dates = [d for d in self._oci_dates(host, repo, reference) if plausible(d, self.now)]
        note = ""
        if not dates:
            fallback = self._oci_tag_time(host, repo, ref.version, ref.digest)
            if plausible(fallback, self.now):
                dates = [fallback]  # type: ignore[list-item]
                note = "registry tag time"
        if not dates:
            return Lookup(error="no build date in the image or registry (reproducible-build timestamp?)")
        return Lookup(published=max(dates), note=note)

    # ---- Helm

    def _helm(self, ref: Ref) -> Lookup:
        if not ref.source:
            return Lookup(error="no chart repository")
        if ref.source.startswith("oci://"):
            location = ref.source[len("oci://") :].rstrip("/") + "/" + ref.name
            image = Ref("oci", location, ref.version)
            lookup = self._oci(image)
            if lookup.error and ref.floor:
                lookup.not_found = True
            return lookup
        index_url = ref.source.rstrip("/") + "/index.yaml"
        with self.lock:
            index = self._helm_index.get(index_url)
        if index is None:
            body, _ = self.http.get(index_url)
            index = yaml.safe_load(body) or {}
            with self.lock:
                self._helm_index[index_url] = index
        for entry in (index.get("entries") or {}).get(ref.name, []) or []:
            if str(entry.get("version", "")).lstrip("v") == ref.version.lstrip("v"):
                created = parse_time(entry.get("created"))
                if created:
                    return Lookup(published=created)
                return Lookup(error="index.yaml entry has no created time")
        return Lookup(error=f"chart version not in {index_url}", not_found=True)

    # ---- GitHub

    def _gh(self, path: str) -> Any:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        return self.http.json(f"https://api.github.com/{path.lstrip('/')}", headers)

    def _gh_tag_date(self, repo: str, tag: str) -> dt.datetime | None:
        dates: list[dt.datetime | None] = []
        try:
            release = self._gh(f"repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}")
            dates.append(parse_time(release.get("published_at")))
        except HttpError as err:
            if err.status != 404:
                raise
        try:
            obj = self._gh(f"repos/{repo}/git/ref/tags/{urllib.parse.quote(tag, safe='')}").get("object") or {}
            if obj.get("type") == "tag":
                dates.append(parse_time((self._gh(f"repos/{repo}/git/tags/{obj['sha']}").get("tagger") or {}).get("date")))
        except HttpError as err:
            if err.status != 404:
                raise
        valid = [d for d in dates if d]
        return max(valid) if valid else None

    def _github(self, ref: Ref) -> Lookup:
        repo, rev = ref.name, ref.version
        commit = self._gh(f"repos/{repo}/commits/{urllib.parse.quote(rev, safe='')}")
        dates = [parse_time(((commit.get("commit") or {}).get("committer") or {}).get("date"))]
        note = "commit date"
        tag = ref.hint if len(rev) == 40 else rev
        if tag:
            tag_date = self._gh_tag_date(repo, tag)
            if tag_date:
                dates.append(tag_date)
                note = "release/tag date" if tag_date >= max(d for d in dates if d) else note
        valid = [d for d in dates if d]
        if not valid:
            return Lookup(error="no commit date")
        return Lookup(published=max(valid), note=note)

    # ---- OpenTofu / Terraform

    def _terraform(self, ref: Ref) -> Lookup:
        host = ref.source or ""
        ns, _, name = ref.name.partition("/")
        errors = []
        if host in ("", "registry.opentofu.org"):
            try:
                data = self.http.json(f"https://api.opentofu.org/registry/docs/providers/{ns}/{name}/index.json")
                for version in data.get("versions", []):
                    if version.get("id", "").lstrip("v") == ref.version.lstrip("v"):
                        return Lookup(published=parse_time(version.get("published")))
                errors.append("version not in the OpenTofu registry")
            except HttpError as err:
                errors.append(str(err))
        if host in ("", "registry.terraform.io", "registry.opentofu.org"):
            try:
                data = self.http.json(f"https://registry.terraform.io/v1/providers/{ns}/{name}/{ref.version}")
                published = parse_time(data.get("published_at"))
                if published:
                    return Lookup(published=published, note="Terraform registry" if host == "registry.opentofu.org" else "")
            except HttpError as err:
                errors.append(str(err))
        if host == "github.com" or host not in ("registry.terraform.io", "registry.opentofu.org", ""):
            # Providers installed from a GitHub release (e.g. github.com/<owner>/<name>).
            for tag in (f"v{ref.version}", ref.version):
                try:
                    published = self._gh_tag_date(f"{ns}/terraform-provider-{name}", tag)
                except HttpError as err:
                    errors.append(str(err))
                    break
                if published:
                    return Lookup(published=published, note="GitHub release")
            errors.append(f"no release {ref.version} in {ns}/terraform-provider-{name}")
        return Lookup(error="; ".join(errors) or "unknown provider host", not_found=True)

    # ---- Go

    def _go(self, ref: Ref) -> Lookup:
        escaped = re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), ref.name)
        version = re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), ref.version)
        data = self.http.json(f"https://proxy.golang.org/{escaped}/@v/{version}.info")
        return Lookup(published=parse_time(data.get("Time")))

    # ---- PyPI / npm

    def _pypi(self, ref: Ref) -> Lookup:
        if ref.source not in PUBLIC_PYPI:
            return Lookup(error=f"private index {ref.source}")
        data = self.http.json(f"https://pypi.org/pypi/{ref.name}/{ref.version}/json")
        uploads = [parse_time(u.get("upload_time_iso_8601") or u.get("upload_time")) for u in data.get("urls", [])]
        valid = [u for u in uploads if u]
        if not valid:
            return Lookup(error="release has no files", not_found=True)
        return Lookup(published=min(valid))

    def _npm(self, ref: Ref) -> Lookup:
        if ref.source not in PUBLIC_NPM:
            return Lookup(error=f"private registry {ref.source}")
        name = ref.name.replace("/", "%2f")
        data = self.http.json(f"https://registry.npmjs.org/{name}")
        published = parse_time((data.get("time") or {}).get(ref.version))
        if not published:
            return Lookup(error="version not in the npm registry", not_found=True)
        return Lookup(published=published)


def is_own(ref: Ref, path: str = "") -> bool:
    lowered = ref.name.lower()
    if path and any(fnmatch.fnmatch(path, g) for g in OWN_PATH_GLOBS) and ref.ecosystem == "oci":
        return True
    if ref.ecosystem == "oci":
        return lowered.startswith(tuple(p.lower() for p in OWN_IMAGE_PREFIXES))
    if ref.ecosystem == "helm":
        return ref.source.lower().startswith(tuple(p.lower() for p in OWN_CHART_PREFIXES))
    if ref.ecosystem == "github":
        return lowered.split("/", 1)[0] in OWN_GITHUB_OWNERS
    if ref.ecosystem == "go":
        return lowered.startswith(OWN_GO_PREFIXES)
    if ref.ecosystem == "npm":
        return lowered.startswith(OWN_NPM_SCOPES)
    return False


# --------------------------------------------------------------------------------------------
# Aikido safe patches (PyPI and npm only)


@dataclasses.dataclass
class SafePatches:
    entries: list[dict[str, str]]
    error: str = ""
    url: str = ""

    def covers(self, ref: Ref) -> bool:
        if ref.ecosystem == "pypi" and ref.source in PUBLIC_PYPI:
            return any(
                (e.get("ecosystem") or "pypi").lower() == "pypi" and pep503(e.get("package_name", "")) == pep503(ref.name) and pep440_equal(e.get("version", ""), ref.version)
                for e in self.entries
            )
        if ref.ecosystem == "npm" and ref.source in PUBLIC_NPM:
            return any((e.get("ecosystem") or "npm").lower() == "npm" and e.get("package_name") == ref.name and e.get("version") == ref.version for e in self.entries)
        return False


def load_safe_patches(http: Http, env: dict[str, str]) -> SafePatches:
    # Always Aikido's official list: an overridable base URL would let a job's environment point
    # at a fake list and pass any fresh release (the Safe Chain bypass guard forbids it too).
    del env
    url = f"{SAFE_PATCHES_DEFAULT_BASE.rstrip('/')}/safe_patches.json"
    try:
        body, _ = http.get(url, {"Accept": "application/json"})
        data = json.loads(body)
        if not isinstance(data, list):
            raise ValueError("not a JSON list")
        entries = [e for e in data if isinstance(e, dict) and isinstance(e.get("package_name"), str) and isinstance(e.get("version"), str)]
        return SafePatches(entries, url=url)
    except Exception as err:  # noqa: BLE001 - no exemptions, the reason goes in the summary
        return SafePatches([], error=f"{type(err).__name__}: {err}", url=url)


# --------------------------------------------------------------------------------------------
# Exception (label + PR-body line + actor)


@dataclasses.dataclass
class ExceptionState:
    labelled: bool = False
    granted: bool = False
    reason: str = ""
    problems: list[str] = dataclasses.field(default_factory=list)
    actor: str = ""


def evaluate_exception(
    labels: Iterable[str],
    body: str,
    author: str,
    actor: str | None,
    actor_role: str | None,
    label: str = EXCEPTION_LABEL,
    team_member: bool | None = None,
) -> ExceptionState:
    state = ExceptionState(labelled=label in set(labels), actor=actor or "")
    if not state.labelled:
        return state
    m = EXCEPTION_LINE.search(body or "")
    if not m:
        state.problems.append("the PR body has no `Release-age exception: <advisory/CVE link> <reason>` line")
    else:
        text = m.group("text")
        state.reason = text
        urls = re.findall(r"https?://\S+", text)
        words = re.sub(r"https?://\S+", " ", text).split()
        if not urls:
            state.problems.append("the `Release-age exception:` line has no advisory or CVE link")
        if len([w for w in words if re.search(r"[A-Za-z]{2,}", w)]) < 3:
            state.problems.append("the `Release-age exception:` line needs a reason of a few words next to the link")
    if not actor:
        state.problems.append(f"could not determine who added the `{label}` label")
    else:
        if actor.lower() == (author or "").lower():
            state.problems.append(f"the `{label}` label was added by the PR author ({actor}); another maintainer must add it")
        if actor.endswith("[bot]"):
            state.problems.append(f"the `{label}` label was added by a bot ({actor})")
        if team_member is False:
            state.problems.append(f"{actor} is not in the exception team")
        if team_member is None and actor_role not in ("admin", "maintain"):
            state.problems.append(f"{actor} has repository role `{actor_role or 'unknown'}`; only admin or maintain may grant an exception")
    state.granted = not state.problems
    return state


# A merge queue entry runs on `gh-readonly-queue/<base>/pr-<number>-<sha>`.
_QUEUE_REF = re.compile(r"^(?:refs/heads/)?gh-readonly-queue/.+/pr-(\d+)-[0-9a-f]+$")


def pull_request_of(event: dict[str, Any], resolver: Resolver, repo: str) -> dict[str, Any]:
    """The pull request a run belongs to, for the exception label and body.

    A `merge_group` event carries no `pull_request`. Its number is in the queue branch name, so
    labels and body come from the API. If that fails, no exception applies (fail closed).
    """
    if event.get("pull_request"):
        return event["pull_request"]
    m = _QUEUE_REF.match((event.get("merge_group") or {}).get("head_ref") or "")
    if not m or not repo:
        return {}
    try:
        return resolver._gh(f"repos/{repo}/pulls/{m.group(1)}") or {}
    except HttpError:
        return {}


def label_actor(resolver: Resolver, repo: str, number: int, label: str) -> str | None:
    actor = None
    page = 1
    while page < 20:
        events = resolver._gh(f"repos/{repo}/issues/{number}/events?per_page=100&page={page}")
        if not events:
            break
        for event in events:
            if event.get("event") == "labeled" and (event.get("label") or {}).get("name") == label:
                actor = (event.get("actor") or {}).get("login")
        if len(events) < 100:
            break
        page += 1
    return actor


def repo_role(resolver: Resolver, repo: str, user: str) -> str | None:
    try:
        data = resolver._gh(f"repos/{repo}/collaborators/{urllib.parse.quote(user)}/permission")
    except HttpError:
        return None
    return data.get("role_name") or data.get("permission")


def team_membership(http: Http, team: str, user: str, token: str) -> bool | None:
    org, _, slug = team.partition("/")
    if not (org and slug and token):
        return None
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}
    try:
        data = http.json(f"https://api.github.com/orgs/{org}/teams/{slug}/memberships/{urllib.parse.quote(user)}", headers)
    except HttpError as err:
        return False if err.status == 404 else None
    return data.get("state") == "active"


# --------------------------------------------------------------------------------------------
# Evaluation and report


def evaluate(occurrences: list[Occurrence], resolver: Resolver, safe: SafePatches, now: dt.datetime, min_age: dt.timedelta = MIN_AGE, workers: int = 8) -> list[Row]:
    grouped: dict[Ref, list[Occurrence]] = {}
    for occ in occurrences:
        grouped.setdefault(occ.ref, []).append(occ)

    def one(item: tuple[Ref, list[Occurrence]]) -> Row:
        ref, occs = item
        own_path = all(any(fnmatch.fnmatch(o.path, g) for g in OWN_PATH_GLOBS) for o in occs)
        lookup = resolver.resolve(ref, occs[0].path if own_path else "")
        age = now - lookup.published if lookup.published else None
        if lookup.own:
            verdict = "own"
        elif lookup.published is None:
            verdict = "skip" if ref.floor and lookup.not_found else "unknown"
        elif age is not None and age < min_age:
            verdict = "too-new"
        else:
            verdict = "pass"
        if verdict in ("too-new", "unknown") and safe.covers(ref):
            verdict = "safe-patch"
        return Row(ref, occs, lookup, verdict, age)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(one, grouped.items()))
    order = {"too-new": 0, "unknown": 1, "exception": 2, "safe-patch": 3, "skip": 4, "pass": 5, "own": 6}
    return sorted(rows, key=lambda r: (order[r.verdict], r.ref.ecosystem, r.ref.display()))


def apply_exception(rows: list[Row], state: ExceptionState) -> None:
    if state.granted:
        for row in rows:
            if row.failing:
                row.verdict = "exception"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_summary(rows: list[Row], safe: SafePatches, state: ExceptionState, min_age: dt.timedelta, now: dt.datetime) -> str:
    hours = int(min_age.total_seconds() // 3600)
    failing = [r for r in rows if r.failing]
    lines = [f"## Release age (minimum {hours} h, checked {now:%Y-%m-%d %H:%M} UTC)", ""]
    if not rows:
        lines.append("No dependency references were added or changed.")
    else:
        verdict_line = f"**{len(failing)} failing**" if failing else "**All new references pass.**"
        lines += [verdict_line, "", "| Reference | Ecosystem | Version | Published | Age | Verdict | Where |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for row in rows:
            published = row.lookup.published.strftime("%Y-%m-%d %H:%M") if row.lookup.published else "—"
            if row.lookup.note:
                published += f" ({row.lookup.note})"
            verdict = VERDICT_TEXT[row.verdict]
            if row.lookup.error and row.verdict in ("unknown", "exception", "skip", "safe-patch"):
                verdict += f": {row.lookup.error}"
            where = ", ".join(f"`{o.path}:{o.line}`" for o in row.occurrences[:3]) + (f" +{len(row.occurrences) - 3}" if len(row.occurrences) > 3 else "")
            lines.append(f"| `{_cell(row.ref.display())}` | {row.ref.ecosystem} | `{_cell(row.ref.shown_version())}` | {published} | {human_age(row.age)} | {_cell(verdict)} | {where} |")
    lines.append("")
    if safe.error:
        lines.append(f"Aikido safe-patch list unavailable ({_cell(safe.error)} from {safe.url}); PyPI and npm pins get no safe-patch exemption.")
    else:
        lines.append(f"Aikido safe-patch list: {len(safe.entries)} entries from {safe.url} (PyPI and npm only).")
    if state.labelled:
        lines.append("")
        if state.granted:
            lines.append(f"**Exception granted** by {state.actor}: {_cell(state.reason)}")
        else:
            lines.append(f"The `{EXCEPTION_LABEL}` label is set but the exception is not valid:")
            lines += [f"- {p}" for p in state.problems]
    elif failing:
        lines += ["", f"To override for a verified security fix, a repo admin or maintainer other than the PR author adds the `{EXCEPTION_LABEL}` label and the PR body carries `Release-age exception: <advisory/CVE link> <reason>`. See the release-age-check runbook in AskZeno/infrastructure."]
    return "\n".join(lines) + "\n"


def annotations(rows: list[Row]) -> list[str]:
    out = []
    for row in rows:
        if row.verdict not in ("too-new", "unknown", "exception"):
            continue
        if row.verdict == "too-new":
            msg = f"{row.ref.display()} {row.ref.shown_version()} was published {human_age(row.age)} ago, under the 7-day minimum release age."
        elif row.verdict == "unknown":
            msg = f"Could not determine when {row.ref.display()} {row.ref.shown_version()} was published ({row.lookup.error}); the check fails closed."
        else:
            msg = f"{row.ref.display()} {row.ref.shown_version()} is under the minimum release age; allowed by the {EXCEPTION_LABEL} label."
        level = "warning" if row.verdict == "exception" else "error"
        for occ in row.occurrences:
            out.append(f"::{level} file={occ.path},line={occ.line},title=Release age::{msg}")
    return out


# --------------------------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="base commit or branch; the merge base with --head is used")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--repo-dir", default=".")
    parser.add_argument("--now", help="ISO time to evaluate ages at (default: now)")
    parser.add_argument("--min-age-hours", type=float, default=MIN_AGE.total_seconds() / 3600)
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH", ""), help="GitHub event JSON (pull_request or merge_group)")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--exception-team", default=os.environ.get("RELEASE_AGE_EXCEPTION_TEAM", ""), help="org/team-slug; needs RELEASE_AGE_ORG_TOKEN with read:org")
    parser.add_argument("--json", help="write the rows as JSON here")
    parser.add_argument("--no-merge-base", action="store_true", help="diff --base..--head directly")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    now = parse_time(args.now) if args.now else dt.datetime.now(dt.UTC)
    if now is None:
        parser.error(f"--now is not an ISO time: {args.now}")
    min_age = dt.timedelta(hours=args.min_age_hours)
    cwd = args.repo_dir
    base = args.base if args.no_merge_base else git("merge-base", args.base, args.head, cwd=cwd).strip()
    occurrences = new_references(base, args.head, cwd)

    http = Http()
    resolver = Resolver(http, now, env.get("GITHUB_TOKEN", ""), env)
    safe = load_safe_patches(http, env)
    rows = evaluate(occurrences, resolver, safe, now, min_age)

    state = ExceptionState()
    if args.event and os.path.exists(args.event):
        with open(args.event) as fh:
            event = json.load(fh)
        pr = pull_request_of(event, resolver, args.repository)
        labels = [label.get("name", "") for label in pr.get("labels", [])]
        if EXCEPTION_LABEL in labels:
            actor = None
            try:
                actor = label_actor(resolver, args.repository, int(pr["number"]), EXCEPTION_LABEL)
            except (HttpError, KeyError, ValueError):
                pass
            if not actor and event.get("action") == "labeled" and (event.get("label") or {}).get("name") == EXCEPTION_LABEL:
                actor = (event.get("sender") or {}).get("login")
            role = repo_role(resolver, args.repository, actor) if actor else None
            member = team_membership(http, args.exception_team, actor, env.get("RELEASE_AGE_ORG_TOKEN", "")) if actor and args.exception_team else None
            if args.exception_team and member is None:
                member = False if not env.get("RELEASE_AGE_ORG_TOKEN") else None
            state = evaluate_exception(labels, pr.get("body") or "", (pr.get("user") or {}).get("login", ""), actor, role, team_member=member)
            if args.exception_team and not env.get("RELEASE_AGE_ORG_TOKEN"):
                state.problems.append("RELEASE_AGE_EXCEPTION_TEAM is set but RELEASE_AGE_ORG_TOKEN is missing, so team membership cannot be checked")
                state.granted = False
    apply_exception(rows, state)

    summary = render_summary(rows, safe, state, min_age, now)
    print(summary)
    for line in annotations(rows):
        print(line)
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(summary)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(
                [
                    {
                        "reference": r.ref.display(),
                        "ecosystem": r.ref.ecosystem,
                        "version": r.ref.shown_version(),
                        "published": r.lookup.published.isoformat() if r.lookup.published else None,
                        "age_hours": round(r.age.total_seconds() / 3600, 1) if r.age else None,
                        "verdict": r.verdict,
                        "error": r.lookup.error,
                        "where": [f"{o.path}:{o.line}" for o in r.occurrences],
                    }
                    for r in rows
                ],
                fh,
                indent=2,
            )
    return 1 if any(r.failing for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
