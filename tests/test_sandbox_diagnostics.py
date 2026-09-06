from __future__ import annotations

import pytest

from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.permissions.context import PermissionContext
from backend.permissions.profiles import sandbox_capability_for_context
from backend.sandbox.policy import sandbox_policy_for_permission_context
from backend.sandbox.runner import SandboxCapability, SandboxRunner


@pytest.mark.parametrize("settings,network_requested,unavailable_action", [
    (
        {"sandbox_workspace_write": {"network_access": True}, "sandbox": {"enabled": False}},
        False, "run_unsandboxed",
    ),
    (
        {"sandbox": {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False}},
        True, "reject_turn",
    ),
])
def test_sandbox_diagnostics_use_the_effective_command_configuration(
    tmp_path, monkeypatch, settings, network_requested, unavailable_action,
):
    stack = ConfigLayerStack(layers=(ConfigLayer(source=ConfigLayerSource(kind="user"), config=settings),))
    context = PermissionContext(mode="confirm", workspace_root=tmp_path)
    command_policy = sandbox_policy_for_permission_context(tmp_path, context, config_stack=stack)
    monkeypatch.setattr("backend.config.load_config_layer_stack", lambda **_: stack)
    monkeypatch.setattr(SandboxRunner, "capability", lambda self, **_: SandboxCapability(
        available=False, backend="audit-unavailable", filesystem_isolated=False, network_isolated=False,
    ))

    diagnostic = sandbox_capability_for_context(tmp_path, context)

    assert diagnostic["requested"]["network"] is network_requested
    assert diagnostic["requested"]["network"] is not command_policy.resolve().allow_network
    assert diagnostic["unavailable_action"] == unavailable_action
    assert diagnostic["fail_closed"] is command_policy.preflight_required
