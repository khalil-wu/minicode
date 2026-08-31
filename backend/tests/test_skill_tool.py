import asyncio
from pathlib import Path

from backend.artifact.store import ArtifactStore
from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.tool_registry_factory import build_tool_registry
from backend.skills.loader import SkillFull, SkillMeta
from backend.skills.manager import SkillManager
from backend.tools.list_files import ListFilesTool
from backend.tools.read_file import ReadFileTool
from backend.tools.search_tools import GlobFilesTool
from backend.tools.write_file import WriteFileTool


class _Loader:
    def __init__(self, skills: list[SkillFull]) -> None:
        self.skills = list(skills)

    def discover(self):
        return [skill.meta for skill in self.skills]

    def list_skill_names(self):
        return list(dict.fromkeys(skill.meta.name for skill in self.skills))

    def get_metas(self, name: str):
        return [skill.meta for skill in self.skills if skill.meta.name == name]

    def get_unambiguous_meta(self, name: str):
        matches = self.get_metas(name)
        return matches[0] if len(matches) == 1 else None

    def get_meta_by_path(self, path):
        if not path:
            return None
        target = Path(path).resolve()
        return next((skill.meta for skill in self.skills if skill.meta.source_path.resolve() == target), None)

    def load_full(self, name: str, path=None):
        meta = self.get_meta_by_path(path) if path else self.get_unambiguous_meta(name)
        return next((skill for skill in self.skills if skill.meta is meta), None)

    def get_all_layer1(self):
        return "\n".join(skill.meta.to_layer1_summary() for skill in self.skills)

    def list_metas(self):
        return [skill.meta for skill in self.skills]


def test_model_tool_registry_has_no_private_skill_lifecycle_tools(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    names = {schema["function"]["name"] for schema in registry.get_schemas()}
    all_names = set(registry.list_tools())

    assert {"load_skill", "unload_skill", "list_skills", "skill_search", "skill"}.isdisjoint(names)
    assert {"load_skill", "unload_skill", "list_skills", "skill_search", "skill"}.isdisjoint(all_names)


def test_skill_manager_requires_an_exact_path_when_names_are_ambiguous(tmp_path) -> None:
    first_path = tmp_path / "project" / "review" / "SKILL.md"
    second_path = tmp_path / "user" / "review" / "SKILL.md"
    first = SkillFull(
        SkillMeta(name="review", description="Project review", source_path=first_path),
        "Project instructions",
        "---\nname: review\ndescription: Project review\n---\nProject instructions",
    )
    second = SkillFull(
        SkillMeta(name="review", description="User review", source_path=second_path),
        "User instructions",
        "---\nname: review\ndescription: User review\n---\nUser instructions",
    )
    manager = SkillManager(_Loader([first, second]))

    assert manager.load_skill_payload("review") is None
    payload = manager.load_skill_payload("review", source_path=second_path)

    assert payload is not None
    assert payload["path"] == str(second_path)
    assert payload["content"].endswith("User instructions")


def test_discovered_skill_directory_is_read_only_outside_workspace(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    skill_root = tmp_path / "user-skills" / "review"
    workspace.mkdir()
    skill_root.mkdir(parents=True)
    skill_file = skill_root / "SKILL.md"
    reference_file = skill_root / "references" / "rules.md"
    reference_file.parent.mkdir()
    skill_file.write_text("---\nname: review\ndescription: Review code.\n---\nRead references/rules.md", encoding="utf-8")
    reference_file.write_text("Use typed lifecycle events.", encoding="utf-8")

    skill = SkillFull(
        SkillMeta(name="review", description="Review code.", source_path=skill_file),
        "Read references/rules.md",
        skill_file.read_text(encoding="utf-8"),
    )
    manager = SkillManager(_Loader([skill]))
    manager.discover()
    readable_roots = [str(path) for path in manager.readable_roots()]
    permission = PermissionContext(filesystem_constraints={
        "allowlist": [".", *readable_roots],
        "readable_roots": readable_roots,
    })
    checker = PermissionChecker(PermissionSettings(), workspace)
    context = ToolExecutionContext(
        permission=permission,
        workspace_root=workspace,
        permission_checker=checker,
    )
    artifact_store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    read_tool = ReadFileTool(artifact_store)

    decision = checker.evaluate(
        "read_file",
        {"file_path": str(reference_file)},
        context=permission,
        tool=read_tool,
    )
    read_result = asyncio.run(read_tool.execute({"file_path": str(reference_file)}, context=context))
    list_result = asyncio.run(ListFilesTool().execute({"directory": str(skill_root)}, context=context))
    glob_result = asyncio.run(GlobFilesTool().execute(
        {"directory": str(skill_root), "pattern": "**/*.md"},
        context=context,
    ))
    write_result = asyncio.run(WriteFileTool().execute(
        {"file_path": str(skill_root / "new.md"), "content": "must not write"},
        context=context,
    ))

    assert decision.capability_allowed is True
    assert read_result.is_error is False
    assert "typed lifecycle" in read_result.content
    assert list_result.is_error is False
    assert "references/" in list_result.content
    assert glob_result.is_error is False
    assert "references/rules.md" in glob_result.content.replace("\\", "/")
    assert write_result.is_error is True
    assert "workspace boundary" in write_result.content.lower()
