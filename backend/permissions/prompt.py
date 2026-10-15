"""Source permissions_instructions.rs composition over frozen MiniCode facts."""
from __future__ import annotations

import json
from pathlib import Path


_RESOURCES = Path(__file__).with_name("resources") / "permissions"
_TEXT = json.loads((_RESOURCES / "source-text.json").read_bytes().decode("utf-8"))


def _bundled(path: str) -> str:
    return (_RESOURCES / path).read_bytes().decode("utf-8")


def permission_instructions(context) -> str:
    model = context.model_execution
    stack = model.config.config_layer_stack
    settings = stack.effective_config() if stack is not None else {}
    requirements = stack.requirements if stack is not None else None
    messages = model.model_info.model_messages if model.model_info is not None else None
    messages = messages or {}
    resolved = context.sandbox_policy.resolve(cwd=context.workspace_root)
    roots = sorted(str(root.root) for root in resolved.writable_roots) if not resolved.full_disk_write else []
    mode = "danger_full_access" if resolved.full_disk_write else "workspace_write" if roots else "read_only"
    sandbox_override = (messages.get("permissions") or {}).get(mode)
    sandbox = sandbox_override if sandbox_override is not None else _bundled(f"sandbox_mode/{mode}.md").rstrip()
    sandbox = sandbox.replace("{{ network_access }}", "enabled" if resolved.allow_network else "restricted")
    policy = context.permission.approval_policy_value()
    kind = context.permission.approval_policy
    reviewer = settings.get("approvals_reviewer", "user")
    if context.permission.strict_auto_review or (requirements is not None and (
            requirements.auto_review_required_for_model(model.model) or not requirements.can_select_user_reviewer())):
        reviewer = "auto_review"
    approval_key = {"on-request": "on_request_auto_review" if reviewer == "auto_review" else "on_request",
        "never": "never", "untrusted": "unless_trusted", "granular": "granular"}[kind]
    catalog = (messages.get("approvals") or {}).get(approval_key)
    prefixes = context.run_context.exec_policy_owner.inspect_for_model(model)["allowed_prefixes_text"]
    registry = context.tool_registry or context.run_context.active_tool_registry
    request_permissions_enabled = registry is not None and registry.get_tool("request_permissions") is not None
    exec_permissions_enabled = model.config.feature_flags.enabled("exec_permission_approvals", default=False)
    prefix_section = _TEXT["APPROVED_PREFIXES"] + prefixes if prefixes else ""
    request_tool = _TEXT["REQUEST_PERMISSIONS_TOOL"]
    if catalog is not None:
        approval = catalog
    elif kind == "never":
        approval = _bundled("approval_policy/never.md")
    elif kind == "untrusted":
        approval = _bundled("approval_policy/unless_trusted.md")
        if request_permissions_enabled:
            approval += "\n\n" + request_tool
        if reviewer == "auto_review":
            approval += "\n\n" + _TEXT["AUTO_REVIEW_SUFFIX"]
    else:
        if kind == "granular":
            granular = policy["granular"]
            categories = ["sandbox_approval", "rules", "skill_approval"]
            if request_permissions_enabled:
                categories.append("request_permissions")
            categories.append("mcp_elicitations")
            sections = [_TEXT["GRANULAR_INTRO"]]
            for allowed, heading in ((True, "GRANULAR_PROMPTED_CATEGORIES"), (False, "GRANULAR_REJECTED_CATEGORIES")):
                rows = [f"- `{name}`" for name in categories if granular[name] is allowed]
                if rows:
                    sections.append(_TEXT[heading] + "\n" + "\n".join(rows))
            if exec_permissions_enabled and granular["sandbox_approval"]:
                sections.append(_bundled("approval_policy/on_request_rule_request_permission.md"))
            if request_permissions_enabled and granular["request_permissions"]:
                sections.append(request_tool)
        else:
            sections = [_bundled("approval_policy/on_request_rule_request_permission.md" if exec_permissions_enabled else "approval_policy/on_request.md")]
            if request_permissions_enabled:
                sections.append(request_tool)
        if prefix_section:
            sections.append(prefix_section)
        approval = "\n\n".join(sections)
        if reviewer == "auto_review":
            approval += "\n\n" + _TEXT["AUTO_REVIEW_SUFFIX"]

    remaining = 32 * 1024 - len(_TEXT["OMITTED_PERMISSION_PATHS"].encode("utf-8")) - 1
    omitted = False
    def bounded(paths):
        nonlocal remaining, omitted
        kept = []
        for path in paths:
            size = len(path.encode("utf-8")) + 10
            if remaining >= size:
                remaining -= size
                kept.append(path)
            else:
                omitted = True
        return kept
    roots = bounded(roots)
    denied_paths = bounded([str(path) for path in resolved.unreadable_roots])
    denied_globs = bounded(sorted(set(resolved.unreadable_globs)))
    sections = [sandbox, approval]
    if roots:
        quoted = [f"`{root}`" for root in roots]
        sections.append(f" The writable root is {quoted[0]}." if len(quoted) == 1 else f" The writable roots are {', '.join(quoted)}.")
    denied = [f"- path `{path}`" for path in denied_paths] + [f"- glob `{glob}`" for glob in denied_globs]
    if denied:
        sections.append("## Denied filesystem reads\nThe active permission profile denies reading these paths/globs. Do not request escalation or additional permissions to read them; these denials are policy restrictions.\n" + "\n".join(denied))
    if omitted:
        sections.append(_TEXT["OMITTED_PERMISSION_PATHS"])
    text = ""
    for section in sections:
        if not section:
            continue
        if not text.endswith("\n"):
            text += "\n"
        text += section
    if not text.endswith("\n"):
        text += "\n"
    return "<permissions instructions>" + text + "</permissions instructions>"
