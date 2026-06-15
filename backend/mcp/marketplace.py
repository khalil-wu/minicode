"""MCP Connectors Marketplace.

Provides curated connector templates that can be installed with one click.
Fetches from the MCP registry and merges with local curated entries.
"""

from __future__ import annotations

from typing import Any

CURATED_CONNECTORS: list[dict[str, Any]] = [
    {
        "name": "filesystem",
        "title": "Filesystem",
        "description": "Read, write, and search files in a specified directory.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        "tags": ["files", "core"],
    },
    {
        "name": "github",
        "title": "GitHub",
        "description": "Interact with GitHub repositories, issues, PRs, and actions.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "tags": ["git", "github", "vcs"],
    },
    {
        "name": "memory",
        "title": "Memory",
        "description": "Persistent memory using a knowledge graph for long-term context.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "tags": ["memory", "knowledge"],
    },
    {
        "name": "postgres",
        "title": "PostgreSQL",
        "description": "Query and manage PostgreSQL databases with schema inspection.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-postgres"],
        "tags": ["database", "sql", "postgres"],
    },
    {
        "name": "sqlite",
        "title": "SQLite",
        "description": "Read and query SQLite databases.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sqlite"],
        "tags": ["database", "sql", "sqlite"],
    },
    {
        "name": "brave-search",
        "title": "Brave Search",
        "description": "Web and local search using the Brave Search API.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-brave-search"],
        "tags": ["search", "web"],
    },
    {
        "name": "puppeteer",
        "title": "Puppeteer",
        "description": "Browser automation for web scraping and testing.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-puppeteer"],
        "tags": ["browser", "automation", "testing"],
    },
    {
        "name": "playwright",
        "title": "Playwright",
        "description": "Official Playwright MCP server for browser automation, inspection, and UI testing.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest"],
        "tags": ["browser", "automation", "testing", "playwright"],
        "auth": "none",
        "requiresUserAction": False,
        "setupHint": "Installs and starts the official Playwright MCP package with npx.",
        "docsUrl": "https://github.com/microsoft/playwright-mcp",
    },
    {
        "name": "figma-desktop",
        "title": "Figma Dev Mode",
        "description": "Connect to the local Figma Desktop Dev Mode MCP server for design context.",
        "transport": "http",
        "url": "http://127.0.0.1:3845/mcp",
        "autoStart": False,
        "maxRetries": 1,
        "tags": ["design", "figma", "mcp", "local-app"],
        "auth": "local_app",
        "requiresUserAction": True,
        "setupHint": "Open Figma Desktop, enable the Dev Mode MCP server, then start or restart this connector.",
        "docsUrl": "https://help.figma.com/hc/en-us/articles/32132100833559-Guide-to-the-Dev-Mode-MCP-Server",
    },
    {
        "name": "slack",
        "title": "Slack",
        "description": "Read and send messages in Slack workspaces.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-slack"],
        "tags": ["chat", "slack", "messaging"],
    },
    {
        "name": "fetch",
        "title": "Fetch",
        "description": "Fetch and convert web pages to markdown for reading.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-fetch"],
        "tags": ["web", "http", "fetch"],
    },
    {
        "name": "sequential-thinking",
        "title": "Sequential Thinking",
        "description": "Dynamic problem-solving through structured thought sequences.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "tags": ["reasoning", "thinking"],
    },
]

_cache: dict[str, Any] = {"data": None, "ts": 0}
_CACHE_TTL = 900


def get_marketplace_connectors(installed_names: list[str] | None = None) -> list[dict[str, Any]]:
    """Return curated connectors with installed status."""
    installed = set(installed_names or [])
    result = []
    for c in CURATED_CONNECTORS:
        entry = {**c, "installed": c["name"] in installed}
        result.append(entry)
    return result
