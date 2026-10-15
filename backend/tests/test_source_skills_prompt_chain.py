from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.agent.prompting import PromptBuilderV2
from backend.config_layers import ConfigLayerStack, ConfigLayer, ConfigLayerSource
from backend.plugins.manager import PluginManager
from backend.plugins.package import validate_plugin_directory, package_plugin_directory
from backend.plugins.source_agent_manifest import AGENT_PLUGIN_SCHEMA
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.skills.executor import SkillExecutor, utf8_prefix
from backend.llm.provider_contracts import ModelDefinition
from backend.tests.test_codex_guardian import guardian_context


def skill(root: Path, name: str, body="Instructions", products=None):
    target = root / name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\nname: {name}\ndescription: Use {name}.\n---\n{body}", encoding="utf-8")
    if products is not None:
        (target.parent / "agents").mkdir()
        (target.parent / "agents/openai.yaml").write_text("policy:\n  products: " + json.dumps(products), encoding="utf-8")
    return target


def manager_for(root, monkeypatch, stack):
    loader = SkillLoader(root)
    loader._config_stack = stack
    monkeypatch.setattr(loader, "_search_dirs", lambda: [("workspace", root)])
    return SkillManager(loader)


def test_ordered_global_rules_product_gate_and_prompt_setting_own_the_real_discovery(tmp_path, monkeypatch):
    target = skill(tmp_path, "same")
    skill(tmp_path, "other-product", products=["chatgpt"])
    skill(tmp_path, "codex-product", products=["codex"])
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user"), {"skills": {"config": [{"path": str(target), "enabled": False}]}}),
        ConfigLayer(ConfigLayerSource("session_flags"), {"skills": {"include_instructions": False, "config": [{"name": "same", "enabled": True}]}})))
    manager = manager_for(tmp_path, monkeypatch, stack)
    assert {meta.name for meta in manager.discover()} == {"same", "codex-product"}
    assert SkillExecutor(manager).build_layer1_summary() == ""
    assert manager.load_skill_payload("same")["content"].endswith("Instructions")


def test_catalog_roots_aliases_utf8_budget_and_complete_main_agent_policy(tmp_path, monkeypatch):
    root = tmp_path / ("long-root-" * 12)
    for index in range(25):
        skill(root, f"skill-{index}", "required refs remain complete")
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user"), {"skills": {"max_context_tokens": 600}}),))
    manager = manager_for(root, monkeypatch, stack)
    text = SkillExecutor(manager).build_layer1_summary(context_window_tokens=100_000)
    assert "### Skill roots" in text and "r0/skill-" in text
    assert "main agent must read each required instruction or reference file itself" in text
    assert "Do not delegate reading, summarizing, or interpreting skill instructions to a subagent" in text
    assert "continue until EOF" in text
    assert utf8_prefix("é🙂汉" * 4000, 8000).encode("utf-8") == ("é🙂汉" * 4000).encode("utf-8")[:7998]


def test_agent_plugin_schema_to_snapshot_direct_children_and_actual_skill_payload_limit(tmp_path, monkeypatch):
    root = tmp_path / "plugins" / "agent"
    root.mkdir(parents=True)
    (root / "plugin.json").write_text(json.dumps({"$schema": AGENT_PLUGIN_SCHEMA, "name": "agent", "version": "1.0.0"}), encoding="utf-8")
    direct = skill(root / "skills", "direct", "é🙂" * 3000)
    skill(root / "skills" / "nested", "hidden")
    monkeypatch.setenv("MINICODE_PLUGINS_DIR", str(root.parent))
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user"), {"plugins": {"agent@local": {"enabled": True}}}),))
    assert validate_plugin_directory(root)["ok"]
    assert Path(package_plugin_directory(root, tmp_path / "packages")["package"]["path"]).is_file()
    snapshot = PluginManager(plugin_roots=[root.parent], config_stack=stack).snapshot()
    entry = next(item for item in snapshot.plugins if item["name"] == "agent")
    assert entry["manifest_format"] == "agent_plugin" and entry["skill_discovery_mode"] == "direct_children"
    loader = SkillLoader(tmp_path)
    loader._config_stack = stack
    manager = SkillManager(loader)
    metas = manager.discover()
    meta = next(item for item in metas if item.source_path == direct)
    assert meta.agent_plugin
    assert not any(item.source_path.parent.name == "hidden" for item in metas)
    payload = manager.load_skill_payload(meta.name, direct)
    assert len(payload["content"].encode("utf-8")) <= 8000
    assert payload["warnings"] == [f"Skill `{meta.name}` exceeded the main prompt context limit and was truncated."]


def test_captured_template_is_literal_empty_is_authoritative_and_cache_tracks_same_slug_changes():
    builder = PromptBuilderV2()
    state = AgentState(user_message="test")
    args = {"state": state, "model_slug": "gpt-6.1-sol", "has_captured_model": True}
    for template in ("Captured template one", "", "Captured template two"):
        parts = builder.build_sections(**args, captured_model_messages={"instructions_template": template})
        assert next(part.content for part in parts if part.name == "stable_system") == template


def test_source_model_snapshot_controls_main_prompt_and_empty_collaboration_mode(tmp_path):
    ctx, _ = guardian_context(tmp_path)
    info = ModelDefinition("custom", "parent", "parent", "openai-responses", "https://fixture.invalid", extra={"model_messages": {
        "instructions_template": "Captured model instructions", "collaboration_modes": {"plan": "", "default": "Captured default mode"}}})
    ctx.model_execution = replace(ctx.model_execution, model_info=info)
    builder = ContextBuilder(llm=ctx.llm, workspace_root=tmp_path)
    builder._tool_execution_context = ctx
    state = AgentState(user_message="test", prompt_context={"collaboration_mode": "plan"})
    assert builder._build_prompt_parts(state, tmp_path).stable == "Captured model instructions"
    assert builder._build_collaboration_mode_block(state) == "<collaboration_mode>\n\n</collaboration_mode>"
    state.prompt_context["collaboration_mode"] = "default"
    assert "Captured default mode" in builder._build_collaboration_mode_block(state)


def test_truncation_image_and_model_template_metadata_reach_selected_adapter():
    from backend.llm.model_runtime import ModelRuntime
    from backend.services.llm_adapter_factory import _build_registered_provider_adapter
    runtime = ModelRuntime(provider_configs={"owned": {"api": "openai-responses", "base_url": "https://fixture.invalid/v1", "api_key": "fixture",
        "models": [{"id": "m", "truncation_policy": {"mode": "bytes", "limit": 1234}, "supports_image_detail_original": True,
            "model_messages": {"instructions_template": ""}}]}})
    info = runtime.get_model("owned", "m")
    settings = _build_registered_provider_adapter(runtime.resolve_adapter_spec("owned", "m"))._settings
    assert info.truncation_policy == settings.truncation_policy == {"mode": "bytes", "limit": 1234}
    assert info.supports_image_detail_original and settings.supports_image_detail_original
    assert settings.model_messages == {"instructions_template": ""}


def test_unknown_configured_model_receives_the_source_unknown_descriptor_prompt():
    from backend.llm.model_runtime import ModelRuntime
    from backend.agent.codex_prompts import codex_model_instructions
    from backend.services.llm_adapter_factory import _build_registered_provider_adapter

    runtime = ModelRuntime(provider_configs={"owned": {"api": "openai-responses", "base_url": "https://fixture.invalid/v1", "api_key": "fixture",
        "models": [{"id": "unknown-descriptor"}]}})
    model = runtime.get_model("owned", "unknown-descriptor")
    settings = _build_registered_provider_adapter(runtime.resolve_adapter_spec("owned", "unknown-descriptor"))._settings
    assert model.model_messages == settings.model_messages
    assert settings.model_messages["instructions_template"] == codex_model_instructions("unknown-descriptor")
    assert settings.truncation_policy == {"mode": "bytes", "limit": 10000}
