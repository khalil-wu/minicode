"""Provider-owned output limits for task and terminal projections.

The values come from the checked-in Claude Code contracts: TaskOutput uses a
32,000-character default with a 160,000-character upper bound, and Bash uses a
30,000-character default with a 150,000-character upper bound.  The shared Pi
50-KiB tool-result boundary still applies when these snapshots enter a model
turn.
"""

from __future__ import annotations

CLAUDE_TASK_OUTPUT_DEFAULT_CHARS = 32_000
CLAUDE_TASK_OUTPUT_MAX_CHARS = 160_000
CLAUDE_BASH_OUTPUT_DEFAULT_CHARS = 30_000
CLAUDE_BASH_OUTPUT_MAX_CHARS = 150_000
