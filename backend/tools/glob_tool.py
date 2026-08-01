"""Compatibility import for the legacy ``GlobTool`` name."""

from backend.tools.search_tools import GlobFilesTool


class GlobTool(GlobFilesTool):
    """Backward-compatible alias of the current workspace glob tool."""
