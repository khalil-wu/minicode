import asyncio
from pathlib import Path

from backend.agent.context import ContextBuilder
from backend.agent.skill_activation import activate_turn_skills
from backend.agent.state import AgentState
from backend.config import TokenBudget
from backend.skills.executor import SkillExecutor, _cap_layer1_summary
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager


def _write_skill(
    root: Path,
    name: str,
    description: str,
    body: str,
    *,
    openai_yaml: str = "",
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )
    if openai_yaml:
        agents_dir = skill_dir / "agents"
        agents_dir.mkdir()
        (agents_dir / "openai.yaml").write_text(openai_yaml, encoding="utf-8")
    return skill_file


def _loader(*roots: tuple[str, Path]) -> SkillLoader:
    loader = SkillLoader()
    loader._search_dirs = lambda: list(roots)  # type: ignore[method-assign]
    return loader


def test_layer1_summary_cap_preserves_entry_boundary_and_omission_notice() -> None:
    summary = "\n".join(f"- skill-{index}: {'x' * 80}" for index in range(100))

    capped = _cap_layer1_summary(summary, max_chars=800)

    assert len(capped) <= 800
    assert "omitted by context budget" in capped
    assert capped.startswith("- skill-0:")
    assert _cap_layer1_summary(summary, max_chars=0) == ""


def test_executor_renders_minicode_available_skill_catalog(tmp_path) -> None:
    skills_root = tmp_path / "skills"
    skill_path = _write_skill(skills_root, "verify", "Verify completed work.", "Run the checks.")
    manager = SkillManager(_loader(("workspace", skills_root)))

    result = SkillExecutor(manager).build_layer1_summary()

    assert "## Skills" in result
    assert "### Available skills" in result
    assert f"(file: {str(skill_path).replace(chr(92), '/')})" in result
    assert "Do not carry skills across turns unless re-mentioned." in result


def test_executor_has_one_canonical_catalog_for_all_providers(tmp_path) -> None:
    skills_root = tmp_path / "skills"
    skill_path = _write_skill(
        skills_root,
        "xml-safe",
        "Use A & B <carefully>.",
        "Run the workflow.",
    )
    manager = SkillManager(_loader(("workspace", skills_root)))
    executor = SkillExecutor(manager)

    result = executor.build_layer1_summary()

    normalized_path = str(skill_path).replace(chr(92), "/")
    assert f"(file: {normalized_path})" in result
    assert "Use A & B <carefully>." in result
    assert "<available_skills>" not in result


def test_loader_reads_agent_instructions_only_from_skill_md_and_ui_metadata_from_openai_yaml(tmp_path) -> None:
    skills_root = tmp_path / "skills"
    skill_path = _write_skill(
        skills_root,
        "openai-docs",
        "Use official OpenAI documentation.",
        "Read official sources before answering.",
        openai_yaml="""interface:
  display_name: OpenAI Docs
  short_description: Official documentation workflow
  icon_small: ./icon.png
  brand_color: '#10a37f'
  default_prompt: Check official docs.
policy:
  allow_implicit_invocation: false
dependencies:
  tools:
    - type: mcp
      value: openaiDeveloperDocs
""",
    )
    (skill_path.parent / "icon.png").write_bytes(b"png")
    loader = _loader(("workspace", skills_root))

    [meta] = loader.discover()
    full = loader.load_full("openai-docs")

    assert meta.name == "openai-docs"
    assert meta.description == "Use official OpenAI documentation."
    assert meta.display_name == "OpenAI Docs"
    assert meta.short_description == "Official documentation workflow"
    assert meta.brand_color == "#10a37f"
    assert meta.mcp_dependencies == ["openaiDeveloperDocs"]
    assert meta.allow_implicit_invocation is False
    assert meta.default_prompt == "Check official docs."
    assert meta.icon == str(skill_path.parent / "icon.png")
    assert full is not None
    assert full.content == "Read official sources before answering."
    assert full.raw_content.startswith("---\nname: openai-docs")


def test_duplicate_skill_names_remain_distinct_and_plain_name_is_not_resolved(tmp_path) -> None:
    workspace_root = tmp_path / "workspace-skills"
    user_root = tmp_path / "user-skills"
    workspace_path = _write_skill(workspace_root, "review", "Workspace review.", "Workspace rules.")
    user_path = _write_skill(user_root, "review", "User review.", "User rules.")
    loader = _loader(("workspace", workspace_root), ("user", user_root))
    manager = SkillManager(loader)

    metas = manager.discover()

    assert [meta.source_path for meta in metas] == [workspace_path, user_path]
    assert manager.detect("Use $review for this") == []
    assert manager.load_skill_payload("review") is None


def test_structured_selection_resolves_exact_duplicate_skill_path(tmp_path) -> None:
    workspace_root = tmp_path / "workspace-skills"
    user_root = tmp_path / "user-skills"
    _write_skill(workspace_root, "review", "Workspace review.", "Workspace rules.")
    user_path = _write_skill(user_root, "review", "User review.", "User rules.")
    manager = SkillManager(_loader(("workspace", workspace_root), ("user", user_root)))

    detections = manager.detect(
        "Review this change",
        selected_skills=[{"name": "review", "path": str(user_path)}],
    )

    assert len(detections) == 1
    assert detections[0].name == "review"
    assert detections[0].source_path == str(user_path)
    payload = manager.load_skill_payload("review", detections[0].source_path)
    assert payload is not None
    assert payload["content"].endswith("User rules.\n")


def test_context_builder_consumes_complete_skill_payload_once(tmp_path) -> None:
    skills_root = tmp_path / "skills"
    skill_path = _write_skill(skills_root, "verify", "Verify completed work.", "Run all checks.")
    manager = SkillManager(_loader(("workspace", skills_root)))
    builder = ContextBuilder(skill_manager=manager, token_budget=TokenBudget(active_skills=1000))
    state = AgentState(user_message="Check this")
    state.prompt_context["selected_skills"] = [{"name": "verify", "path": str(skill_path)}]

    async def run():
        activation_events = [event async for event in activate_turn_skills(manager, state.user_message, state)]
        await builder.start_turn(state.user_message, state)
        first = await builder.build(state)
        await builder.start_turn("Check again", state)
        second = await builder.build(state)
        return activation_events, first, second

    activation_events, first, second = asyncio.run(run())
    first_skill_messages = [message.content for message in first if "<skill>" in message.content]
    second_skill_messages = [message.content for message in second if "<skill>" in message.content]

    assert activation_events == []
    assert len(first_skill_messages) == 1
    assert "<name>verify</name>" in first_skill_messages[0]
    assert f"<path>{skill_path}</path>" in first_skill_messages[0]
    assert "---\nname: verify" in first_skill_messages[0]
    assert second_skill_messages == first_skill_messages
    assert state.prompt_context.get("skill_injections") is None


def test_skill_asset_resolution_is_limited_to_declared_files_inside_exact_skill(tmp_path) -> None:
    skills_root = tmp_path / "skills"
    skill_path = _write_skill(
        skills_root,
        "visual",
        "Visual workflow.",
        "Use the declared asset.",
        openai_yaml="""interface:
  icon_small: ./assets/icon.svg
""",
    )
    asset = skill_path.parent / "assets" / "icon.svg"
    asset.parent.mkdir()
    asset.write_text("<svg></svg>", encoding="utf-8")
    manager = SkillManager(_loader(("workspace", skills_root)))

    manager.discover()

    assert manager.resolve_asset(str(skill_path), "small") == asset
    assert manager.resolve_asset(str(tmp_path / "missing" / "SKILL.md"), "small") is None
    assert manager.resolve_asset(str(skill_path), "large") is None
