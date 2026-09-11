"""Snippet: typed behavioral hints use the existing tool annotations."""

from litestar import Litestar, get

from litestar_mcp import LitestarMCP, mcp_tool


def build() -> "Litestar":
    # start-example
    @get("/inventory", sync_to_thread=False)
    @mcp_tool(
        "inventory",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
    def inventory() -> "list[str]":
        return []

    app = Litestar(route_handlers=[inventory], plugins=[LitestarMCP()])
    # end-example
    return app
