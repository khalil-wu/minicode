from __future__ import annotations

from typing import Any


def _tool_names(tool_schemas: list[Any]) -> set[str]:
    names: set[str] = set()
    for schema in tool_schemas:
        if not isinstance(schema, dict):
            continue
        function = schema.get("function")
        if isinstance(function, dict) and function.get("name"):
            names.add(str(function["name"]))
    return names


WORKSPACE_TOOLS = {
    "list_files",
    "read_file",
    "write_file",
    "edit_file",
    "run_command",
    "search_files",
}


def build_harness_guidance(
    tool_schemas: list[Any],
    mcp_instructions: dict[str, str] | None = None,
) -> str:
    """Build compact per-turn runtime guidance from available tools.

    Stable behavior belongs in the system prompt. This guidance only tells the
    model which runtime contracts are active for the tools exposed this turn.
    """
    names = _tool_names(tool_schemas)
    sections: list[str] = [
        (
            "Harness contract:\n"
            "- Treat tool results as runtime evidence. If you need to act, call tools before claiming completion.\n"
            "- Final answers must separate confirmed results from plans, guesses, and candidate evidence."
        )
    ]

    if names & WORKSPACE_TOOLS:
        sections.append(
            "Workspace contract:\n"
            "- Use list_files for project or directory overviews; use read_file for exact file contents.\n"
            "- Use write_file/edit_file for file changes. Use run_command for builds, tests, installs, git, and processes."
        )

    if "todo_write" in names:
        plan_lines = [
            "Planning contract:",
            "- **MANDATORY**: For ANY multi-step work (≥2 steps, several files, user-supplied list, "
            "or takes >5 minutes), you MUST call todo_write FIRST before doing anything else.",
            "- Break the work into clear, actionable tasks with imperative form: \"Fix auth bug\", \"Add tests\", \"Update docs\".",
            "- Create tasks at the START of your response, before any execution. This shows the user your plan immediately.",
            "- Only ONE task should be in_progress at a time. Update status to completed when done, then mark next as in_progress.",
            "- Example workflow:",
            "  User: \"refactor authentication and add tests\"",
            "  Step 1: Call todo_write([\"Analyze current auth code\", \"Design new structure\", \"Implement refactor\", \"Write unit tests\", \"Update docs\"])",
            "  Step 2: Mark first task as in_progress",
            "  Step 3: Execute the task",
            "  Step 4: Mark as completed, move to next",
            "- Skip ONLY for trivial single-step requests (one-file edit, simple question).",
        ]
        if "task" in names:
            plan_lines.append(
                "- When subtasks are independent and read-heavy, delegate them in parallel via task "
                "(up to 5 at once) instead of doing them serially yourself."
            )
        if "update_plan" in names:
            plan_lines.append(
                "- For a user-visible execution plan on larger tasks, call update_plan with the full "
                "step list, then call it again to advance each step (exactly one in_progress at a time). "
                "todo_write is your private checklist; update_plan is the plan the user watches."
            )
        sections.append("\n".join(plan_lines))

    if "web_search" in names or "web_fetch" in names:
        sections.append(
            "Web contract:\n"
            "- search snippets are candidate evidence only; fetch sources before confident factual claims.\n"
            "- For today/latest/current questions, include an absolute date in queries and answers."
        )

    mcp_tools = sorted(name for name in names if name.startswith("mcp__"))
    if mcp_tools:
        servers = sorted({parts[1] for tool in mcp_tools if len(parts := tool.split("__")) >= 2})
        server_text = ", ".join(servers) if servers else "available MCP servers"
        sections.append(
            "MCP contract:\n"
            f"- {len(mcp_tools)} MCP tools are available from {server_text}.\n"
            "- Prefer direct MCP tools when already exposed; use deferred discovery only for optional tools."
        )

    # Server-declared usage instructions (mirrors CC's getMcpInstructions). Only
    # include servers that actually expose tools this turn, so the guidance stays
    # consistent with what the model can call.
    if mcp_tools and mcp_instructions:
        exposed_servers = {
            parts[1] for tool in mcp_tools if len(parts := tool.split("__")) >= 2
        }
        blocks = [
            f"## {server}\n{text.strip()}"
            for server, text in sorted(mcp_instructions.items())
            if server in exposed_servers and text.strip()
        ]
        if blocks:
            sections.append("MCP server instructions:\n" + "\n\n".join(blocks))

    if "tool_search" in names:
        sections.append(
            "Deferred tools:\n"
            "- Use tool_search to find optional tools, tool_describe to inspect schemas, and tool_call to invoke them."
        )

    if names & {"read_memory", "save_memory", "recall_memory", "remember_memory"}:
        sections.append(
            "Memory contract:\n"
            "- Use memory only for durable user/project facts. Do not store secrets or transient scratch notes."
        )

    return "\n\n".join(sections)
