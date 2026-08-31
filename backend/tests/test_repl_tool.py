from backend.artifact.store import ArtifactStore
from backend.services.tool_registry_factory import build_tool_registry


def test_repl_does_not_expand_the_default_tool_surface(tmp_path) -> None:
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"))

    assert registry.get_tool("repl") is None
    direct_names = {schema["function"]["name"] for schema in registry.get_schemas()}
    assert "repl" not in direct_names
