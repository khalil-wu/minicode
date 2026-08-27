"""Unified plugin runtime primitives.

The application historically exposed plugin settings from one service while
Skills, MCP and Hooks each walked plugin directories independently.  The
modules in this package provide the small, dependency-free pieces shared by
those consumers: canonical ``name@marketplace`` identities, version
constraints and load-time dependency demotion.

Consumers import from the submodules directly (``backend.plugins.identity``,
``backend.plugins.dependencies``, ``backend.plugins.materializer``).
"""
