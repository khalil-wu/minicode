import asyncio
import json
from types import SimpleNamespace

from backend.config_requirements import ConfigRequirements
from backend.feature_flags import load_feature_flags
from backend.mcp.manager import MCPServerManager
from backend.plugins.manager import PluginManager
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.services import feature_flag_settings_service as flags_service
from backend.services import plugin_settings_service as plugins


def test_global_plugins_flag_reaches_later_skill_hook_extension_instruction_and_mcp_snapshots_without_erasing_installs(tmp_path, monkeypatch):
    from backend import config, config_helpers
    from backend.commands import plugins as commands
    from backend.extensions import capability_source

    settings = tmp_path / "settings.json"
    original = {"plugins": {"docs@local": {"enabled": True}}, "feature_flags": {"plugins": True}}
    settings.write_text(json.dumps(original), encoding="utf8")
    root = tmp_path / "installed" / "docs"
    manifest = root / ".minicode-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    skill = root / "skills/lookup/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: lookup\ndescription: Read local documentation\n---\nOriginal skill instructions.\n", encoding="utf8")
    extension = root / "extensions/entry.py"
    extension.parent.mkdir(parents=True)
    extension.write_text("# Fixture extension; this test never executes it.\n", encoding="utf8")
    manifest.write_text(json.dumps({"name": "docs", "version": "1.0.0", "skills": "./skills",
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo fixture"}]}]},
        "extensions": "./extensions/entry.py",
        "mcp_servers": {"lookup": {"transport": "stdio", "command": "fixture-server", "args": []}}}), encoding="utf8")
    requirements = ConfigRequirements()
    read = lambda: json.loads(settings.read_text(encoding="utf8"))
    stack = SimpleNamespace(requirements=requirements, fingerprint="fixture-policy", managed_policy_errors=(),
        effective_config=read, get_layers=lambda: [])
    stack_for = lambda **_kwargs: stack
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings)
    monkeypatch.setattr(config, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(config, "load_config_layer_stack", stack_for)
    monkeypatch.setattr(config, "get_config_requirements", lambda: requirements)
    monkeypatch.setattr(config, "_load_effective_settings_json", read)
    monkeypatch.setattr(plugins, "_load_settings_json", read)
    monkeypatch.setattr(commands, "default_plugin_roots", lambda: [root.parent])
    monkeypatch.setenv("MINICODE_PLUGINS_DIR", str(root.parent))
    monkeypatch.setattr(capability_source, "load_config_layer_stack", stack_for)
    monkeypatch.setattr(flags_service, "get_config_requirements", lambda: requirements)
    monkeypatch.setattr(flags_service, "_load_settings_json", read)
    monkeypatch.setattr(flags_service, "load_config", lambda: SimpleNamespace(feature_flags=load_feature_flags(read(), managed_requirements={})))
    monkeypatch.setattr(SkillLoader, "_search_dirs", lambda self: self._plugin_search_dirs())
    loader = SkillLoader()
    loader._config_stack = stack
    skills = SkillManager(loader)
    mcp = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=None)
    source = capability_source.ExtensionCapabilitySource(session_owner="fixture", owner_id="fixture-chat", workspace_root=None,
        project_trusted=False, on_model_change=lambda *_: None)
    captured = PluginManager(config_stack=stack).snapshot()
    enabled_fingerprint = source.fingerprint()

    def consumers():
        return {
            "skills": skills.snapshot(config_stack=stack).discover(),
            "hooks": plugins.load_enabled_plugin_hook_sources(config_stack=stack),
            "extensions": plugins.load_enabled_plugin_extension_sources(config_stack=stack),
            "instructions": plugins.resolve_enabled_plugin_mentions([{"path": "plugin://docs@local"}], config_stack=stack,
                connected_mcp_servers=["plugin:docs@local:lookup"]),
            "mcp": mcp._load_plugin_configs(config_stack=stack),
        }
    enabled = consumers()
    assert all(enabled.values())
    assert {skill.name for skill in enabled["skills"]} == {"docs@local:lookup"}
    assert {server.name for server in enabled["mcp"]} == {"plugin:docs@local:lookup"}
    assert enabled["instructions"][0]["mcp_server_names"] == ["plugin:docs@local:lookup"]
    hooks = []
    async def allowed(**kwargs):
        hooks.append(kwargs)
    changed = asyncio.run(flags_service.update_feature_flag_settings({"plugins": False}, settings_file=settings, config_change_hook=allowed))
    assert changed["effective"]["plugins"] == {"enabled": False}
    assert all(not value for value in consumers().values())
    assert source.fingerprint() != enabled_fingerprint
    # A captured in-flight inventory stays owned by its original turn.
    assert captured.enabled_plugins
    current = PluginManager(config_stack=stack)
    assert not current.enabled()
    assert current.resolve("docs@local") is None
    assert current.resolve("docs@local", require_enabled=False)["enabled"] is True
    assert read()["plugins"] == original["plugins"]
    assert manifest.exists() and skill.read_text(encoding="utf8").endswith("Original skill instructions.\n")
    asyncio.run(flags_service.update_feature_flag_settings({"plugins": True}, settings_file=settings, config_change_hook=allowed))
    assert all(consumers().values())
    assert len(hooks) == 2
