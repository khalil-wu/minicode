from __future__ import annotations

from backend.artifact.store import ArtifactStore
from backend.services.tool_registry_factory import build_tool_registry
from backend.tools.tool_search import DeferredToolCatalog


class _HostedSearchProvider:
    def supports_hosted_web_search(self) -> bool:
        return True

    def hosted_web_search_supports_blocked_domains(self) -> bool:
        return True


def test_cc_alignment_tools_are_registered_with_expected_visibility(tmp_path) -> None:
    registry = build_tool_registry(
        ArtifactStore(storage_dir=tmp_path / "artifacts"),
        llm_provider=_HostedSearchProvider(),
    )

    for name in {"browser_control"}:
        assert registry.get_tool(name) is not None
    assert registry.get_tool("repl") is None
    assert registry.get_tool("skill_search") is None

    direct_names = {schema["function"]["name"] for schema in registry.get_schemas()}
    assert {
        "task",
        "task_status",
        "task_stop",
        "send_message",
        "list_files",
        "ask_user",
        "web_search",
        "web_fetch",
    } <= direct_names
    deferred_names = {entry.name for entry in DeferredToolCatalog(registry).entries()}
    assert {"web_search", "web_fetch"}.isdisjoint(deferred_names)
    assert "skill_search" not in direct_names
    assert "repl" not in direct_names
    assert "team_memory_sync" not in direct_names
    assert "browser_control" not in direct_names


def test_web_search_is_available_without_a_hosted_provider(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    assert registry.get_tool("web_search") is not None


def test_mcp_bridge_tools_are_uniformly_deferred(tmp_path) -> None:
    """No MCP bridge tool may sit in the always-on tool array.

    list_mcp_resources/read_mcp_resource declared should_defer while the
    template, subscription, notification, and prompt bridges did not, so the
    rarely-needed six were direct on every turn while the two entry points were
    hidden behind tool_search. Keep the whole family lazy and reachable only
    through the deferred directory.
    """

    registry = build_tool_registry(
        ArtifactStore(storage_dir=tmp_path / "artifacts"),
        llm_provider=_HostedSearchProvider(),
    )
    mcp_names = {name for name in registry.list_tools() if "mcp" in name}
    assert mcp_names, "expected MCP bridge tools to be registered"

    direct_names = {schema["function"]["name"] for schema in registry.get_schemas()}
    assert not (mcp_names & direct_names)

    views = {view.name: view for view in registry.build_schema_views()}
    for name in mcp_names:
        assert views[name].exposure == "deferred", name
        assert views[name].schema_available is True, name

    # The async-agent availability filter admits the whole "mcp" toolset, so
    # bridge tools must remain name-activated members of the default toolset.
    assert {
        name for name in mcp_names if registry.get_tool_spec(name).toolset == "mcp"
    } == set()

# Documentation placeholders that are deliberately not tool names: example
# artifact ids, example symbol names used in search/LSP docs, tool_search
# argument placeholders, and a result field returned by read_file.
_NON_TOOL_DOC_TOKENS = frozenset(
    {
        "art_a1b2c3d4",
        "content_hash",
        "run_agent",
        "run_agent_loop",
        "string_helper",
        "tool_a",
        "tool_b",
        "tool_name",
    }
)


def _schema_vocabulary(tool) -> set[str]:
    """Property names and enum values a tool may legitimately mention."""

    words: set[str] = set()

    def walk(node) -> None:
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            words.update(properties)
            for value in properties.values():
                walk(value)
        for value in node.get("enum", []) or []:
            if isinstance(value, str):
                words.add(value)
        for key in ("items", "additionalProperties"):
            walk(node.get(key))
        for key in ("anyOf", "oneOf", "allOf"):
            for value in node.get(key, []) or []:
                walk(value)

    for getter in ("model_schema", "get_schema"):
        try:
            schema = getattr(tool, getter)()
        except Exception:
            continue
        if schema is not None:
            walk(schema.parameters if isinstance(schema.parameters, dict) else {})
    return words


def _tool_facing_text(tool) -> str:
    texts = [str(getattr(tool, "description", "") or "")]
    for getter in ("model_description", "runtime_description"):
        try:
            texts.append(str(getattr(tool, getter)() or ""))
        except Exception:
            continue
    for getter in ("model_schema", "get_schema"):
        try:
            schema = getattr(tool, getter)()
        except Exception:
            continue
        if schema is None:
            continue
        texts.append(str(schema.description or ""))
        properties = (
            schema.parameters.get("properties")
            if isinstance(schema.parameters, dict)
            else None
        )
        if isinstance(properties, dict):
            for value in properties.values():
                if isinstance(value, dict):
                    texts.append(str(value.get("description") or ""))
    return "\n".join(texts)


def test_tool_descriptions_never_reference_a_nonexistent_tool(tmp_path) -> None:
    """A description that names a tool the registry lacks causes hallucinated calls.

    ``list_worktree_snapshots`` once instructed the model to "必须先显式调用
    save_worktree_snapshot" while the real tool is ``worktree_snapshot``; the
    model would have received "Tool does not exist" from the registry. Nothing
    else in the suite catches this, so pin it registry-wide.
    """

    import re

    registry = build_tool_registry(
        ArtifactStore(storage_dir=tmp_path / "artifacts"),
        llm_provider=_HostedSearchProvider(),
    )
    real_tools = set(registry.list_tools())
    vocabulary: set[str] = set()
    for name in real_tools:
        vocabulary |= _schema_vocabulary(registry.get_tool(name))

    offenders: dict[str, set[str]] = {}
    for name in sorted(real_tools):
        text = _tool_facing_text(registry.get_tool(name))
        for token in set(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", text)):
            if token in real_tools or token in vocabulary:
                continue
            if token in _NON_TOOL_DOC_TOKENS:
                continue
            offenders.setdefault(token, set()).add(name)

    assert not offenders, (
        "tool-facing text mentions snake_case names that are neither a registered "
        "tool nor any tool's schema field/enum value. Either fix the name, or add "
        f"it to _NON_TOOL_DOC_TOKENS if it is a documentation example: {offenders}"
    )
