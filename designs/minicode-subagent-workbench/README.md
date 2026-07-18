# MiniCode Subagent Workbench design

## Context

This prototype is based on:

- The current MiniCode React/Electron shell and design tokens.
- `frontend/src.v2/shell/tabs/SubagentsTab.tsx` and its existing runtime state.
- The user-provided Codex screenshots showing compact agent chips, a right-side agent list, and a dedicated agent detail page.
- `docs/research/codex-claude-comparison.md`.

## Design decision

The prototype uses one recommended direction rather than several divergent visual styles because the user supplied a clear interaction reference. It preserves MiniCode's warm neutral surfaces while simplifying the current Subagents panel into three disclosure levels:

1. Inline chips in the parent conversation.
2. Compact Agents list in the right pane.
3. Dedicated Agent detail with milestones, grouped activity, and result.

The prototype intentionally does not include raw tool arguments, internal coordinator messages, token diagnostics, or workflow graph controls in the default view.

