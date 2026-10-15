from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.config import PermissionSettings
from backend.conversations.repository import ConversationRepository
from backend.mcp.client import MCPToolDef
from backend.mcp.registry import MCPToolProxy, MCPToolRegistry
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.services.permission_rules_service import prepare_permission_rule_add
from backend.services.permission_rules_service import build_permission_rules_list_outcome
from backend.tools.base import PermissionLevel
from backend.tools.registry import ToolRegistry
from backend.tools.tool_search import DeferredToolCatalog
from backend.tools.toolsets import ToolAvailabilityFilter, ToolsetPolicy


def proxy(server: str, name: str, *, destructive: bool = False) -> MCPToolProxy:
    return MCPToolProxy(
        server, MCPToolDef(name, "Remote operation", annotations={
            "openWorldHint": True, "destructiveHint": destructive,
        }), SimpleNamespace(connected=True),
    )


@pytest.mark.parametrize("identities", [
    (("s", "a__b"), ("s__a", "b")),
    (("s", "a.b"), ("s", "a_b")),
    (("s", "Case"), ("s", "case")),
    (("s/a", "b?*"), ("s", "a/b?*")),
])
def test_saved_exact_mcp_grant_survives_reload_without_authorizing_colliding_tool(tmp_path, identities):
    first, second = [proxy(*identity) for identity in identities]
    registry = ToolRegistry()
    registry.register(first)
    repo = ConversationRepository(tmp_path / "conversations")
    conversation = repo.create_conversation(title="Exact remote consent")
    mutation = prepare_permission_rule_add(
        conversation, {"pattern": first.name, "rule_kind": "override", "level": "auto"},
        conversation_id=conversation.id, tool_registry=registry,
    )
    repo.update_permission_rules(
        conversation.id, deny_rules=mutation.deny_rules,
        overrides=mutation.serialized_overrides,
    )
    restored = ConversationRepository(tmp_path / "conversations").get_conversation(conversation.id)
    from backend.ws.utils import normalize_permission_overrides

    context = PermissionContext(
        mode="bypass", approval_policy="never",
        session_overrides=normalize_permission_overrides(restored.permission_overrides),
    )
    checker = PermissionChecker(PermissionSettings(builtin_approval_routing=frozenset({"require_confirm", "require_diff_review"})), tmp_path)
    assert first.policy_identity != second.policy_identity
    assert checker.evaluate(first.name, {}, context=context, tool=first).decision == "allow"
    assert checker.evaluate(second.name, {}, context=context, tool=second).decision == "deny"


@pytest.mark.parametrize("mode", ["confirm", "auto", "bypass"])
def test_never_denies_unapproved_remote_call_and_exact_consent_retains_hard_floors(tmp_path, mode):
    tool = proxy("s", "a__b")
    context = PermissionContext(mode=mode, approval_policy="never")
    checker = PermissionChecker(PermissionSettings(builtin_approval_routing=frozenset({"require_confirm", "require_diff_review"})), tmp_path)
    assert checker.check(tool.name, {}, context=context, tool=tool) == PermissionLevel.ALWAYS_DENY
    assert checker.evaluate(tool.name, {}, context=context, tool=tool).decision == "deny"
    allowed = replace(context, session_overrides={tool.policy_identity: PermissionLevel.AUTO})
    assert checker.evaluate(tool.name, {}, context=allowed, tool=tool).decision == "allow"
    denied = PermissionChecker(PermissionSettings(always_deny=[tool.policy_identity]), tmp_path)
    assert denied.evaluate(tool.name, {}, context=allowed, tool=tool).decision == "deny"
    assert checker.evaluate(tool.name, {}, context=replace(allowed, mode="plan"), tool=tool).decision == "deny"
    destructive = proxy("s", "a__b", destructive=True)
    # Source annotations trigger approval routing, but do not revoke an exact
    # approved tool grant. Authored denials and Plan restrictions stay binding.
    assert checker.evaluate(destructive.name, {}, context=allowed, tool=destructive).decision == "allow"
    assert denied.evaluate(destructive.name, {}, context=allowed, tool=destructive).decision == "deny"


def test_ambiguous_legacy_grants_are_inactive_but_legacy_restrictions_still_apply(tmp_path):
    tools = [proxy("s", "a__b"), proxy("s__a", "b")]
    old = "mcp__s__a__b"
    checker = PermissionChecker(PermissionSettings(), tmp_path)
    for tool in tools:
        consent = PermissionContext(mode="confirm", session_overrides={old: PermissionLevel.AUTO})
        assert checker.evaluate(tool.name, {}, context=consent, tool=tool).decision == "ask"
        assert not ToolAvailabilityFilter(tools=frozenset({old})).allows(tool.get_spec())
        enabled = ToolsetPolicy(enabled_toolsets=frozenset(), enabled_tools=frozenset({tool.policy_identity}))
        assert enabled.is_available(tool.get_spec())
        assert not replace(enabled, disabled_tools=frozenset({old})).is_available(tool.get_spec())
        blocked = replace(consent, session_overrides={old: PermissionLevel.ALWAYS_DENY})
        assert checker.evaluate(tool.name, {}, context=blocked, tool=tool).decision == "deny"


def test_exact_remote_consent_clears_default_routing_and_keeps_authored_restrictions(tmp_path):
    tool = proxy("s", "a__b")
    consent = PermissionContext(mode="confirm", approval_policy="never", session_overrides={tool.policy_identity: PermissionLevel.AUTO})
    static_grant = PermissionChecker(PermissionSettings(auto_allow=["mcp::*", tool.policy_identity],
        builtin_approval_routing=frozenset({"require_confirm", "require_diff_review"})), tmp_path)
    assert static_grant.evaluate(tool.name, {}, context=replace(consent, session_overrides={}), tool=tool).decision == "allow"
    assert static_grant.evaluate(tool.name, {}, context=replace(consent, session_overrides={tool.policy_identity: PermissionLevel.CONFIRM}), tool=tool).decision == "deny"
    assert static_grant.evaluate(tool.name, {}, context=replace(consent, session_overrides={"mcp::*": PermissionLevel.AUTO, tool.policy_identity: PermissionLevel.AUTO}), tool=tool).decision == "allow"
    for restrictive in (
        PermissionSettings(require_confirm=["mcp__*", tool.policy_identity]),
        PermissionSettings(require_confirm=["mcp__*", tool.name]),
        PermissionSettings(require_diff_review=["mcp__*"]),
        PermissionSettings(content_ask_rules=[tool.policy_identity]),
    ):
        decision = PermissionChecker(restrictive, tmp_path).evaluate(tool.name, {}, context=consent, tool=tool)
        assert decision.decision == "deny"


def test_wire_renaming_preserves_only_historical_restrictions(tmp_path):
    manager = SimpleNamespace(
        registry_version=0,
        get_server_contract=lambda _name: None,
        get_server_config=lambda _name: None,
    )
    registry = ToolRegistry()
    bridge = MCPToolRegistry(registry, mcp_manager=manager)
    first = proxy("fixture.one", "read.name")
    second = proxy("fixture/one", "read.name")
    bridge._publish_catalog([first, second])
    before = next(tool for tool in registry.get_tools() if tool.server_name == "fixture.one")
    bridge._publish_catalog([proxy("fixture.one", "read.name")])
    current = next(iter(registry.get_tools()))
    assert before.name != current.name
    assert current.policy_identity == before.policy_identity
    assert before.name in current.policy_deny_aliases
    assert before.name not in current.get_spec().policy_names
    checker = PermissionChecker(PermissionSettings(always_deny=[before.name]), tmp_path)
    assert checker.evaluate(current.name, {}, context=PermissionContext(mode="bypass"), tool=current).decision == "deny"


def test_case_folded_search_preserves_distinct_advertised_identities():
    registry = ToolRegistry()
    for name in ("CaseSensitive", "casesensitive"):
        registry.register(proxy("s", name))
    catalog = DeferredToolCatalog(registry)
    expected = ["mcp__s__CaseSensitive", "mcp__s__casesensitive"]
    for query in ("CaseSensitive", "casesensitive", "CASESENSITIVE"):
        assert [entry.name for entry in catalog.search(query, 5)] == expected
    assert {registry.get_tool(name).policy_identity for name in expected} == {"mcp::s/CaseSensitive", "mcp::s/casesensitive"}


def test_legacy_rule_list_and_authored_unknown_alias_explain_reauthorization():
    old = "mcp__s__a__b"
    rules = {
        "mode": "confirm", "session_deny": [], "system_deny": [],
        "session_overrides": [{"pattern": old, "level": "auto"}],
    }
    outcome = build_permission_rules_list_outcome("owned-conversation", rules)
    assert "does not establish an exact raw tool permission" in outcome.message
    target = SimpleNamespace(permission_deny_rules=[], permission_overrides={old: "auto"})
    mutation = prepare_permission_rule_add(
        target, {"pattern": old, "rule_kind": "override", "level": "auto"},
        conversation_id="owned-conversation",
    )
    assert mutation.outcome.level == "warning"
    assert mutation.serialized_overrides[old] == "auto"


def test_global_legacy_allow_warning_uses_existing_config_warning_export():
    from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack

    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user"), {
        "permissions": {"auto_allow": ["mcp__s__a__b", "mcp::s/a__b"]},
    }),))
    warnings = stack.to_payload()["startup_warnings"]
    assert len(warnings) == 1 and "mcp__s__a__b" in warnings[0]
    assert "reauthorize" in warnings[0]
