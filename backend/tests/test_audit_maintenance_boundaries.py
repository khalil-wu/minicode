"""Maintenance boundaries: container argv, SDK nullability, image save
collisions, event-loop DNS, POSIX discovery, agent effort reload, feature-flag
projection and shipped-asset checks.

Each test drives the production entry the read-only audit reproduced with its
probes.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import socket
import subprocess
import time
from io import BytesIO
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- S01

def _container_runner(workspace: Path):
    from backend.sandbox.policy import (
        FileSystemAccessMode, FileSystemSandboxEntry, FileSystemSandboxPolicy, FileSystemPath,
        PermissionProfile, SandboxPolicy,
    )
    from backend.sandbox.runner import SandboxCapability, SandboxRunner

    policy = SandboxPolicy(workspace_root=workspace, permission_profile=PermissionProfile.managed(
        FileSystemSandboxPolicy.restricted([
            FileSystemSandboxEntry(FileSystemPath.path(workspace), FileSystemAccessMode.READ)])))
    runner = SandboxRunner(policy)
    runner.capability = lambda **_kwargs: SandboxCapability(True, "docker", True, True)
    runner._build_env = lambda: {}
    return runner, policy


@pytest.mark.asyncio
@pytest.mark.parametrize("argv", [
    ["clangd", "--background-index"],
    ["git", "--no-optional-locks", "status", "--porcelain"],
    ["node", "server.js", "--stdio"],
])
async def test_container_launch_preserves_a_programs_own_argv(tmp_path, monkeypatch, argv):
    runner, _policy = _container_runner(tmp_path)
    captured = {}

    async def capture(*args, **_kwargs):
        image_index = args.index("audit-image")
        captured["container_argv"] = list(args[image_index + 1:])
        return SimpleNamespace(stdin=None, stdout=None, stderr=None, returncode=None)

    monkeypatch.setattr("backend.sandbox.runner._container_runtime", lambda: ("docker", "audit-image", ""))
    monkeypatch.setattr("backend.sandbox.runner.spawn_exec", capture)
    await runner.spawn_interactive(argv, cwd=tmp_path)
    assert captured["container_argv"] == argv


def test_container_launch_writes_the_readiness_receipt_without_touching_argv(tmp_path, monkeypatch):
    runner, policy = _container_runner(tmp_path)
    monkeypatch.setattr("backend.sandbox.runner._container_runtime", lambda: ("docker", "audit-image", ""))
    resolved = policy.resolve(cwd=tmp_path)
    runner._prepare_synthetic_mount_targets(resolved)
    ready = tmp_path / "ready-receipt"
    ready.write_text("", encoding="utf-8")
    runner._sandbox_ready_file = ready
    try:
        args = runner._container_command("clangd --background-index", "docker", cwd=tmp_path,
            resolved=resolved, argv=["clangd", "--background-index"], tty=False)
    finally:
        runner._cleanup_sandbox_setup_state()
    tail = args[args.index("audit-image") + 1:]
    assert tail == ["/bin/sh", "-lc", 'printf ready > /minicode-control-ready; exec "$0" "$@"',
                    "clangd", "--background-index"]


# ---------------------------------------------------------------- SDK01

@pytest.mark.asyncio
async def test_sdk_nullable_annotation_accepts_explicit_null():
    from jsonschema import Draft202012Validator

    from backend.agent.tool_execution import _authorize_final_tool_request
    from backend.config import PermissionSettings
    from backend.llm.base import ToolCallEvent
    from backend.llm.openai_adapter import OpenAIAdapter
    from backend.permissions.checker import PermissionChecker
    from backend.permissions.context import ToolExecutionContext
    from backend.sdk import create_tool_registry, tool
    from backend.tools.base import validate_tool_input
    from backend.tools.toolsets import ACTIVE_TOOLSET_POLICY_METADATA_KEY, ToolsetPolicy

    calls = []

    @tool(name="audit_nullable")
    def nullable(value: str | None = None):
        calls.append(value)
        return value

    registry = create_tool_registry(nullable)
    checker = PermissionChecker(PermissionSettings(auto_allow=["audit_nullable"]), workspace_root=ROOT)
    permission = checker.build_context(mode="confirm")
    context = ToolExecutionContext(permission=permission, workspace_root=ROOT, tool_registry=registry,
        metadata={ACTIVE_TOOLSET_POLICY_METADATA_KEY: ToolsetPolicy.default()})
    assert nullable.get_schema().parameters["properties"]["value"]["type"] == ["string", "null"]
    assert nullable.get_execution_schema().parameters["properties"]["value"]["type"] == ["string", "null"]
    assert not validate_tool_input(nullable, {"value": None})
    result = await _authorize_final_tool_request(
        ToolCallEvent(id="audit-nullable", name="audit_nullable", arguments={"value": None}),
        tool_registry=registry, permission_checker=checker, permission_context=permission, tool_ctx=context)
    assert not result.error, result.error
    schema = registry.get_tool_schema("audit_nullable", toolset_policy=ToolsetPolicy.default(),
        permission_checker=checker, permission_context=permission)
    responses = OpenAIAdapter._convert_tools_to_responses_format([schema])
    chat = OpenAIAdapter._normalize_chat_tools([schema])
    assert Draft202012Validator(responses[0]["parameters"]).is_valid({"value": None})
    assert Draft202012Validator(chat[0]["function"]["parameters"]).is_valid({"value": None})
    assert calls == []


# ---------------------------------------------------------------- I01

@pytest.mark.asyncio
async def test_image_save_collision_keeps_the_generated_image_and_the_user_file(tmp_path):
    import base64
    import hashlib

    from PIL import Image

    from backend.config import PermissionSettings
    from backend.permissions.checker import PermissionChecker
    from backend.permissions.context import ToolExecutionContext
    from backend.sandbox.policy import SandboxPolicy
    from backend.tools.image_generation_tool import GenerateImageTool

    buffer = BytesIO()
    Image.new("RGB", (2, 2), (34, 102, 153)).save(buffer, format="PNG")
    raw_png = buffer.getvalue()
    encoded_png = base64.b64encode(raw_png).decode("ascii")

    async def observe(collision: bool):
        target = tmp_path / ("collision.png" if collision else "control.png")
        user_fixture = b"independent user save after prepare_image_output\n"
        emitted = []

        async def emit(kind, payload):
            emitted.append({"type": kind, "data": payload})

        class InMemoryProvider:
            def __init__(self, _settings):
                self.closed = False

            async def generate_images(self, *_args, **_kwargs):
                if collision:
                    target.write_bytes(user_fixture)
                return [(encoded_png, "image/png")]

            async def aclose(self):
                self.closed = True

        checker = PermissionChecker(PermissionSettings(), workspace_root=tmp_path)
        permission = checker.build_context(mode="confirm")
        context = ToolExecutionContext(permission=permission, workspace_root=tmp_path,
            sandbox_policy=SandboxPolicy.workspace_default(tmp_path),
            artifact_store=SimpleNamespace(save=lambda *a, **k: None),
            emit_event=emit,
            tool_call_id="audit-image-output", conversation_id="audit-image-chat")
        with patch("backend.tools.image_generation_tool.OpenAIAdapter", InMemoryProvider):
            result = await GenerateImageTool().execute({"prompt": "audit fixture",
                "api_base_url": "http://127.0.0.1:1/v1", "model": "audit-image", "output_path": str(target)}, context)
        actual = target.read_bytes() if target.exists() else None
        return result, actual, user_fixture

    control, control_bytes, _ = await observe(False)
    assert control.status == "success" and control_bytes == raw_png
    assert len(control.images) == 1 and control.images[0]["data"] == encoded_png

    collision, collision_bytes, user_fixture = await observe(True)
    assert collision.is_error is False
    assert collision.status == "partial"
    assert len(collision.images) == 1 and collision.images[0]["data"] == encoded_png
    assert collision_bytes == user_fixture
    assert "could not be saved" in collision.content


# ---------------------------------------------------------------- WEB01

async def _observe_dns_blocking():
    loop = asyncio.get_running_loop()
    started = time.perf_counter()
    fired = []
    heartbeat = loop.call_later(0.02, lambda: fired.append(time.perf_counter() - started))

    def slow_failed_dns(*_args, **_kwargs):
        time.sleep(0.15)
        raise socket.gaierror("audit DNS fixture; no request sent")

    with patch("backend.permissions.network.socket.getaddrinfo", slow_failed_dns):
        fetched = await _run_web_fetch()
        ticks_before_completion = len(fired)
        await asyncio.sleep(0.01)
    heartbeat.cancel()
    return fetched, ticks_before_completion


async def _run_web_fetch():
    from backend.tools.web_tools import WebFetchTool

    result = await WebFetchTool(artifact_store=object()).execute({"url": "https://audit.example/", "prompt": "audit"})
    return result


@pytest.mark.asyncio
async def test_web_fetch_dns_lookup_does_not_block_the_event_loop():
    fetched, ticks = await _observe_dns_blocking()
    assert ticks > 0
    assert fetched.is_error


@pytest.mark.asyncio
async def test_preview_navigation_dns_lookup_does_not_block_the_event_loop():
    from backend.ws.handlers import preview as preview_handler

    loop = asyncio.get_running_loop()
    started = time.perf_counter()
    fired = []
    delivered = []
    heartbeat = loop.call_later(0.02, lambda: fired.append(time.perf_counter() - started))

    def slow_failed_dns(*_args, **_kwargs):
        time.sleep(0.15)
        raise socket.gaierror("audit DNS fixture; no request sent")

    async def scope_boundary(*_args, **_kwargs):
        return SimpleNamespace(conversation_id="audit-preview-chat", workspace_root=str(tmp_path_value))

    async def capture_event(event):
        delivered.append(event.type)

    tmp_path_value = ROOT
    session = SimpleNamespace(session_id="audit-preview-session", send_event=capture_event)
    with patch("backend.permissions.network.socket.getaddrinfo", slow_failed_dns), \
            patch.object(preview_handler, "_resolve_scope", scope_boundary):
        completed = await preview_handler.handle_preview_navigate(session, {"url": "https://audit.example/"})
        ticks_before_completion = len(fired)
        await asyncio.sleep(0.01)
    heartbeat.cancel()
    assert completed is True
    assert ticks_before_completion > 0
    assert delivered


# ---------------------------------------------------------------- WS-W01

class _MemoryEntry:
    def __init__(self, name):
        self.name, self.path = name, "/repo/" + name

    def is_symlink(self):
        return False

    def is_dir(self, **_kwargs):
        return False

    def is_file(self, **_kwargs):
        return True


class _MemoryDirectory:
    ENTRIES = ["aux.py", "con.txt", "aux.ts", "normal.py", "normal.ts"]

    def __enter__(self):
        return iter(_MemoryEntry(name) for name in self.ENTRIES)

    def __exit__(self, *_args):
        return None


class _MemoryPath(PurePosixPath):
    def read_text(self, **_kwargs):
        raise FileNotFoundError(str(self))


@pytest.mark.parametrize("platform,expected", [
    ("posix", ["/repo/aux.py", "/repo/con.txt", "/repo/aux.ts", "/repo/normal.py", "/repo/normal.ts"]),
    ("nt", ["/repo/normal.py", "/repo/normal.ts"]),
])
def test_discovery_applies_windows_device_names_only_on_windows(monkeypatch, platform, expected):
    import backend.workspace.fuzzy_search as fuzzy

    monkeypatch.setattr(fuzzy, "os", SimpleNamespace(name=platform, scandir=lambda _path: _MemoryDirectory()))
    monkeypatch.setattr(fuzzy, "Path", _MemoryPath)
    returned = [str(path) for path, _is_dir in fuzzy.iter_search_paths(_MemoryPath("/repo"))]
    assert returned == expected


# ---------------------------------------------------------------- AGCFG01

def test_agent_effort_ultra_survives_reload_and_child_defaults(tmp_path, monkeypatch):
    from backend.agents import loader
    from backend.agents.source_roles import get_execution_agent
    from backend.config_layers import ConfigLayerStack
    from backend.services.agent_editor_service import AgentUpsertPayload, list_agents, upsert_agent
    from backend.tools import subagent_support

    config = SimpleNamespace(config_layer_stack=ConfigLayerStack(()))
    agents = tmp_path / ".minicode" / "agents"
    monkeypatch.setattr(loader, "_agent_search_dirs", lambda *args, **kwargs: [agents])
    monkeypatch.setattr(subagent_support, "get_custom_agent",
        lambda name, root: get_execution_agent(name, root, config=config))
    for effort in ("high", "ultra"):
        name = f"audit-{effort}"
        saved = upsert_agent(AgentUpsertPayload(name=name, description="private fixture",
            prompt="Read only audit fixture.", model="custom/audit-model", effort=effort, source="project"),
            workspace_root=tmp_path)
        file_text = (agents / f"{name}.md").read_text(encoding="utf-8")
        reloaded = next(agent for agent in list_agents(workspace_root=tmp_path)["agents"] if agent["name"] == name)
        defaults = subagent_support._configured_subagent_overrides(agent_type=name, model_override="",
            effort_override="", workspace_root=tmp_path)
        assert saved["agent"]["effort"] == effort
        assert f"effort: {effort}" in file_text
        assert reloaded["effort"] == effort
        assert defaults[1] == effort


# ---------------------------------------------------------------- FLAGS01

def test_feature_flag_effective_projection_uses_the_startup_profile_layers(tmp_path, monkeypatch):
    from backend import config_helpers
    from backend.feature_flags import load_feature_flags
    from backend.managed_settings import ManagedSettingsResult
    from backend.services.feature_flag_settings_service import get_feature_flag_settings

    workspace = tmp_path / "profile"
    workspace.mkdir()
    user = workspace / "settings.json"
    user.write_text(json.dumps({"features": {"global_search": True}}), encoding="utf-8")
    (workspace / "audit.config.toml").write_text("[features]\nglobal_search = false\n", encoding="utf-8")
    monkeypatch.delenv("MINICODE_FEATURE_GLOBAL_SEARCH", raising=False)
    monkeypatch.setenv("MINICODE_PROFILE", "audit")
    monkeypatch.setenv("MINICODE_SYSTEM_CONFIG_FILE", str(workspace / "absent-system.toml"))
    monkeypatch.setenv("MINICODE_REQUIREMENTS_FILE", str(workspace / "absent-requirements.toml"))
    monkeypatch.setattr(config_helpers, "STATE_ROOT", workspace)
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", user)
    monkeypatch.setattr("backend.config.SETTINGS_FILE", user, raising=False)
    monkeypatch.setattr("backend.workspace.state.get_explicit_active_workspace_root", lambda: None)
    monkeypatch.setattr("backend.managed_settings.load_minicode_managed_settings",
        lambda *args, **kwargs: ManagedSettingsResult({}))
    stack = config_helpers.load_config_layer_stack()
    runtime_flags = load_feature_flags(stack.effective_config(),
        managed_requirements=stack.requirements.feature_requirements)
    assert runtime_flags.enabled("global_search") is False
    settings = get_feature_flag_settings()
    assert settings["effective"]["global_search"]["enabled"] is False
    row = next(row for row in settings["flags"] if row["name"] == "global_search")
    assert row["enabled"] is False


# ---------------------------------------------------------------- S-M01

def test_hatch_continuity_metric_counts_rgb_only_changes():
    from PIL import Image

    spec = importlib.util.spec_from_file_location(
        "hatch_measure_continuity", ROOT / "bundled-skills" / "hatch-pet" / "scripts" / "measure_direction_continuity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cell = (module.CELL_WIDTH, module.CELL_HEIGHT)
    first = Image.new("RGBA", cell, (0, 0, 0, 0))
    first.paste(Image.new("RGBA", (64, 64), (255, 0, 0, 255)), (10, 10))
    recolored = first.copy()
    recolored.paste(Image.new("RGBA", (64, 64), (0, 0, 255, 255)), (10, 10))
    assert module.pair_metric(first, recolored)["diffPixels"] == 64 * 64
    assert module.pair_metric(first, first.copy())["diffPixels"] == 0


# ---------------------------------------------------------------- S-JS01

def test_figma_variable_discovery_example_compiles(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to compile the shipped example")
    text = (ROOT / "backend" / "skills" / "curated" / "figma-generate-design" / "SKILL.md").read_text(encoding="utf-8")
    marker = text.index("const frame = figma.currentPage.findOne")
    block = text[marker:text.index("```", marker)]
    target = tmp_path / "snippet.js"
    target.write_text("async function __audit_wrapper() {\n" + block + "\n}\n", encoding="utf-8")
    completed = subprocess.run([node, "--check", str(target)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
