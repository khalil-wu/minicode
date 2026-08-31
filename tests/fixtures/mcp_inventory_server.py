from __future__ import annotations

from mcp.server.fastmcp import FastMCP


server = FastMCP(
    "minicode-inventory-fixture",
    instructions="Fixture server for exercising standard MCP inventory discovery.",
)


@server.resource(
    "fixture://guide",
    name="Guide",
    description="A fixed resource exposed by the fixture server.",
    mime_type="text/markdown",
)
def guide() -> str:
    return "# Fixture guide"


@server.resource(
    "fixture://repo/{path}",
    name="Repository file",
    description="A resource template exposed by the fixture server.",
    mime_type="text/plain",
)
def repository_file(path: str) -> str:
    return f"fixture content for {path}"


@server.prompt(
    name="review",
    description="Review a repository path with an optional tone.",
)
def review(path: str, tone: str = "concise") -> str:
    return f"Review {path} using a {tone} tone."


if __name__ == "__main__":
    server.run(transport="stdio")
