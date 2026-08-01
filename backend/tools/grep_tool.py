"""Compatibility import for the legacy ``GrepTool`` name."""

from backend.tools.search_tools import GrepFilesTool


class GrepTool(GrepFilesTool):
    """Backward-compatible alias of the current workspace grep tool."""
