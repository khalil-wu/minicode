from __future__ import annotations
import asyncio
import json
from types import SimpleNamespace
from pathlib import Path
import pytest
from backend.workspace.service import WorkspaceService
from backend.workspace.editor_language import (
    EditorDocument,
    EditorLanguageRequest,
    editor_language_request,
)
from backend.lsp.editor import editor_language_servers
from backend.workspace.delivery import (
    GitDeliveryRequest,
    git_delivery_action,
    git_delivery_status,
    run_delivery_command,
)
from backend.conversations.repository import ConversationRepository
from backend.conversations.import_export import import_conversation_tree


@pytest.mark.asyncio
async def test_bundled_python_uses_unsaved_documents_for_completion_navigation_diagnostics_and_formatting(
    tmp_path,
):
    (tmp_path / "main.py").write_text("# disk content\n", encoding="utf-8")
    (tmp_path / "helper.py").write_text("# disk helper\n", encoding="utf-8")
    service = WorkspaceService(get_workspace_root=lambda: tmp_path)

    async def request(method, content, line=0, character=0):
        return (
            await editor_language_request(
                service,
                EditorLanguageRequest(
                    method=method,
                    line=line,
                    character=character,
                    documents=[
                        EditorDocument(path="main.py", content=content),
                        EditorDocument(
                            path="helper.py",
                            content="def greeting() -> str:\n    return 'hello'\n",
                        ),
                    ],
                ),
            )
        )["result"]

    try:
        completion = await request("completion", "value = 'hello'\nvalue.", 1, 6)
        items = completion if isinstance(completion, list) else completion["items"]
        assert "upper" in {item["label"] for item in items}
        definitions = await request(
            "definition", "from helper import greeting\ngreeting()", 1, 4
        )
        assert any(
            "helper.py" in item.get("uri", item.get("targetUri", ""))
            for item in definitions
        )
        diagnostics = await request("diagnostics", 'value: int = "wrong"\n')
        assert any(item.get("severity") == 1 for item in diagnostics)
        formatted = await request("formatting", "value=[1,2,3]\n")
        assert formatted["text"] == "value = [1, 2, 3]\n"
        assert (tmp_path / "main.py").read_text() == "# disk content\n"
    finally:
        await editor_language_servers.shutdown()


@pytest.mark.asyncio
async def test_bundled_yaml_uses_local_schema_for_completion_and_diagnostics(tmp_path):
    (tmp_path / "schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"serverPort": {"type": "integer"}},
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    service = WorkspaceService(get_workspace_root=lambda: tmp_path)
    prefix = (
        "# yaml-language-server: $schema=" + (tmp_path / "schema.json").as_uri() + "\n"
    )

    async def request(method, content, line=0, character=0):
        return (
            await editor_language_request(
                service,
                EditorLanguageRequest(
                    method=method,
                    line=line,
                    character=character,
                    documents=[EditorDocument(path="config.yaml", content=content)],
                ),
            )
        )["result"]

    try:
        result = await request("completion", prefix + "ser", 1, 3)
        items = result if isinstance(result, list) else result["items"]
        assert "serverPort" in {item["label"] for item in items}
        diagnostics = await request("diagnostics", prefix + "serverPort: wrong\n")
        assert any(item.get("severity") == 1 for item in diagnostics)
        result = await request("formatting", "hello:   world\n")
        assert result
    finally:
        await editor_language_servers.shutdown()


@pytest.mark.asyncio
async def test_git_delivery_staged_only_branch_and_local_push(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    await git_delivery_action(root, GitDeliveryRequest(action="init"))
    await run_delivery_command(root, ["git", "config", "user.name", "MiniCode Test"])
    await run_delivery_command(
        root, ["git", "config", "user.email", "test@example.invalid"]
    )
    (root / "tracked.txt").write_text("first\n")
    (root / "untracked.txt").write_text("keep untracked\n")
    await run_delivery_command(root, ["git", "add", "tracked.txt"])
    await git_delivery_action(
        root,
        GitDeliveryRequest(
            action="commit", message="First\n\nMultiline body", expected_branch="main"
        ),
    )
    output, _, _ = await run_delivery_command(
        root, ["git", "ls-tree", "--name-only", "HEAD"]
    )
    assert output == "tracked.txt"
    remote = tmp_path / "remote.git"
    await run_delivery_command(tmp_path, ["git", "init", "--bare", str(remote)])
    await run_delivery_command(root, ["git", "remote", "add", "origin", str(remote)])
    await git_delivery_action(
        root, GitDeliveryRequest(action="push", remote="origin", expected_branch="main")
    )
    assert (await git_delivery_status(root))["upstream"] == "origin/main"
    await git_delivery_action(
        root,
        GitDeliveryRequest(
            action="branch", branch="feature/test", expected_branch="main"
        ),
    )
    assert (await git_delivery_status(root))["branch"] == "feature/test"


def test_conversation_import_remaps_tree_without_overwriting_and_preserves_history(
    tmp_path,
):
    repo = ConversationRepository(base_dir=tmp_path)
    original = repo.create_conversation(title="Original")
    child = repo.create_conversation(title="Child", parent_conversation_id=original.id)
    original.transcript = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    repo.save_conversation(original)
    payload = repo.export_conversation_tree(original.id)
    imported = import_conversation_tree(repo, payload, "/new/project")
    assert imported["count"] == 2 and imported["conversation_id"] != original.id
    copied = repo.get_conversation(imported["conversation_id"])
    assert [entry["content"] for entry in copied.transcript] == ["hello", "world"]
    imported_child = next(
        repo.get_conversation(item["id"])
        for item in imported["conversations"]
        if item["title"] == "Child"
    )
    assert imported_child.parent_conversation_id == copied.id
    assert (
        copied.permission_mode == "confirm" and copied.workspace_root == "/new/project"
    )
    assert repo.get_conversation(child.id).parent_conversation_id == original.id


@pytest.mark.asyncio
async def test_inline_code_has_no_tools_and_cancels_provider_on_disconnect(
    tmp_path, monkeypatch
):
    from backend.workspace.editor_ai import (
        EditorCompletionRequest,
        editor_completion_response,
    )

    (tmp_path / "main.py").write_text("x = 1")
    started, closed = asyncio.Event(), asyncio.Event()

    class Adapter:
        async def side_query(self, messages, *, options, turn_context):
            assert options.operation == "editor_inline_complete"
            assert options.max_tokens == 128 and options.max_retries == 0
            assert json.loads(messages[1].content)["prefix"] == "x = "
            started.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda *args, **kwargs: Adapter(),
    )
    monkeypatch.setattr(
        "backend.config.load_config",
        lambda **kwargs: SimpleNamespace(llm=SimpleNamespace(model="test")),
    )
    response = editor_completion_response(
        WorkspaceService(get_workspace_root=lambda: tmp_path),
        EditorCompletionRequest(
            path="main.py", prefix="x = ", suffix="", max_tokens=128
        ),
    )
    task = asyncio.create_task(anext(response.body_iterator))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.asyncio
async def test_mcp_tool_selection_preserves_connection_credentials_and_other_policy(
    monkeypatch,
):
    from backend.services import mcp_service
    import copy

    original = {
        "transport": "http",
        "url": "https://example.invalid/mcp",
        "headers": {"Authorization": "Bearer test-only"},
        "required": True,
        "tool_timeout_sec": 91,
        "enabled_tools": ["read"],
        "disabled_tools": ["write"],
    }
    memory = {"servers": {"docs": copy.deepcopy(original)}}
    monkeypatch.setattr(mcp_service, "_read_current_config_data", lambda: memory)

    async def write(data, **kwargs):
        memory.update(data)

    monkeypatch.setattr(mcp_service, "_write_config_data", write)

    class Manager:
        def get_server_config(self, name):
            return SimpleNamespace(source="user")

        def get_all_status(self):
            return []

        async def reload_config(self):
            pass

    await mcp_service.update_mcp_server(
        Manager(),
        {
            "original_name": "docs",
            "tools_only": True,
            "enabled_tools": ["read", "write"],
        },
    )
    assert memory["servers"]["docs"] == {
        **original,
        "enabled_tools": ["read", "write"],
        "disabled_tools": [],
    }


@pytest.mark.asyncio
async def test_speech_uses_multipart_with_existing_provider_headers_and_closes_client():
    import httpx
    from backend.config import LLMSettings
    from backend.llm.openai_adapter import OpenAIAdapter

    captured = []

    def handle(request):
        captured.append(request)
        return httpx.Response(200, json={"text": "这是可编辑的草稿"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = OpenAIAdapter(
            LLMSettings(
                provider="custom",
                api_key="test-only",
                base_url="https://example.invalid/v1",
                model="chat",
            ),
            http_client=client,
        )
        text = await adapter.transcribe_audio(
            b"audio",
            filename="recording.webm",
            model="whisper-1",
            language="zh",
            prompt="MiniCode",
        )
        assert text == "这是可编辑的草稿"
        assert (
            captured[0]
            .headers["content-type"]
            .startswith("multipart/form-data; boundary=")
        )
        assert captured[0].headers["authorization"] == "Bearer test-only"
        assert captured[0].url.path == "/v1/audio/transcriptions"
        assert b"MiniCode" in captured[0].content
        await adapter.aclose()


@pytest.mark.asyncio
async def test_environment_stream_cancellation_stops_its_owned_process(
    tmp_path, monkeypatch
):
    import sys
    from backend.workspace import environment_setup

    monkeypatch.setattr(
        environment_setup,
        "environment_commands",
        lambda root: {
            "node_dependencies": [
                sys.executable,
                "-u",
                "-c",
                'import time; print("ready", flush=True); time.sleep(60)',
            ]
        },
    )
    process = None
    spawn = environment_setup.spawn_exec

    async def capture(*args, **kwargs):
        nonlocal process
        process = await spawn(*args, **kwargs)
        return process

    monkeypatch.setattr(environment_setup, "spawn_exec", capture)
    response = environment_setup.environment_setup_response(
        tmp_path, environment_setup.EnvironmentSetupRequest(action="node_dependencies")
    )
    assert "ready" in await anext(response.body_iterator)
    await response.body_iterator.aclose()
    assert process.returncode is not None
