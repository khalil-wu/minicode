import asyncio
import logging
import json
import time
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from backend.agent.context import ContextBuilder
from backend.agent.loop import AgentLoopSessionContext, run_agent_loop
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.message import AgentEvent, UserCommand
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, AppConfig, LLMSettings, PermissionSettings, TokenBudget, load_config
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType, ToolCallEvent
from backend.llm.errors import classify_llm_error, sanitize_llm_error_message
from backend.main import app
from backend.mcp.manager import MCPServerConfig, MCPServerManager, ServerStatus
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.mcp.client import MCPClient
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.agent_tools import TaskTool
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession


class _HungLLM(LLMAdapter):
    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        await asyncio.sleep(1.0)
        if False:
            yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return ""


class _DoneLLM(LLMAdapter):
    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="done")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "done"


class _StaticTool(BaseTool):
    permission = None

    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, args: dict[str, object], context=None) -> ToolResult:
        return ToolResult(content="ok")


class _CountingTool(BaseTool):
    permission = None

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=f"{self.name} tool",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, args: dict[str, object], context=None) -> ToolResult:
        self.calls += 1
        return ToolResult(content=f"{self.name}:{self.calls}:{args}")


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)


class _ReusableClient:
    instances = 0

    def __init__(self, **kwargs) -> None:
        type(self).instances += 1
        self.connected = False
        self.connect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        self.connected = True

    async def list_tools(self) -> list[object]:
        return []

    async def close(self) -> None:
        self.connected = False
        return True

    async def finish_pending_cleanup(self) -> bool:
        return True


class _FailingReusableClient(_ReusableClient):
    closed_count = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        self.connected = False
        raise RuntimeError("boom")

    async def close(self) -> None:
        type(self).closed_count += 1
        return await super().close()


class _SingleStreamErrorClient:
    class _Response:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"type":"error","error":{"type":"rate_limit_error","message":"Concurrency limit exceeded"}}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def __init__(self, *args, **kwargs):
        pass

    def stream(self, *args, **kwargs):
        return self._Response()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def _collect_events(stream) -> list:
    return [event async for event in stream]


def test_artifact_store_reads_persisted_artifact_after_memory_clear(tmp_path) -> None:
    store = ArtifactStore(storage_dir=tmp_path)

    artifact_id = store.save("persisted artifact body", source="unit-test")
    store.clear()

    assert store.count == 0
    assert store.get(artifact_id) == "persisted artifact body"
    assert store.get_meta(artifact_id) is not None
    assert store.get_meta(artifact_id).source == "unit-test"  # type: ignore[union-attr]


def test_artifact_store_enforces_conversation_and_workspace_owner(tmp_path) -> None:
    store = ArtifactStore(storage_dir=tmp_path)
    artifact_id = store.save(
        "private artifact body",
        source="unit-test",
        conversation_id="conv-one",
        workspace_root=tmp_path / "workspace-one",
    )

    assert store.get(
        artifact_id,
        conversation_id="conv-one",
        workspace_root=tmp_path / "workspace-one",
    ) == "private artifact body"
    assert store.get(artifact_id, conversation_id="conv-two") is None
    assert store.get_preview(artifact_id, conversation_id="conv-two") is None
    assert store.get(
        artifact_id,
        conversation_id="conv-one",
        workspace_root=tmp_path / "workspace-two",
    ) is None


def test_artifact_store_cleans_up_expired_artifacts_from_disk(tmp_path) -> None:
    store = ArtifactStore(storage_dir=tmp_path, ttl_seconds=60)

    expired_id = store.save("expired artifact", source="unit-test")
    fresh_id = store.save("fresh artifact", source="unit-test")

    # The new on-disk layout stores ``created_at`` in the metadata sidecar; the
    # TTL classifier compares against that field. Backdate the expired sidecar's
    # ``created_at`` to simulate an artifact older than the TTL.
    sidecar_path = tmp_path / f"{expired_id}.meta.json"
    sidecar_payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar_payload["created_at"] = time.time() - 120
    sidecar_path.write_text(json.dumps(sidecar_payload), encoding="utf-8")

    removed = store.cleanup_expired(now=time.time())

    assert removed == 1
    assert store.get(expired_id) is None
    assert store.get(fresh_id) == "fresh artifact"
    store.shutdown()


@pytest.mark.skip(reason="passive RAG was removed from the Agent runtime")
def test_rag_pipeline_async_path_reuses_query_embedding_and_caches_result() -> None:
    class FakeCollection:
        def __init__(self, name: str) -> None:
            self.name = name

    class FakeEmbedder:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def embed(self, text: str) -> list[float]:
            self.calls.append(text)
            return [0.25, 0.75]

    class FakeRetriever:
        def __init__(self) -> None:
            self.calls: list[list[list[float]] | None] = []

        def retrieve_and_format(
            self,
            *,
            query: str,
            collection,
            top_k: int,
            min_score: float,
            max_tokens: int,
            query_embeddings=None,
        ) -> str:
            self.calls.append(query_embeddings)
            return f"context:{collection.name}"

    pipeline = RAGPipeline(top_k=6)
    pipeline._initialized = True
    pipeline._collections = {
        "memory": FakeCollection("memory"),
        "documents": FakeCollection("documents"),
        "codebase": FakeCollection("codebase"),
    }
    pipeline._retriever = FakeRetriever()
    pipeline._embedder = FakeEmbedder()
    pipeline._ensure_initialized = lambda: True  # type: ignore[assignment]

    first = asyncio.run(pipeline.retrieve_context_async("How do I refactor websocket state?"))
    second = asyncio.run(pipeline.retrieve_context_async("How do I refactor websocket state?"))

    assert first == second
    assert pipeline._embedder.calls == ["How do I refactor websocket state?"]
    assert pipeline._retriever.calls == [[[0.25, 0.75]]] * 3


@pytest.mark.skip(reason="passive RAG was removed from the Agent runtime")
def test_rag_pipeline_uses_fallback_friendly_default_min_score() -> None:
    pipeline = RAGPipeline()

    assert pipeline._min_score == 0.35


@pytest.mark.skip(reason="passive RAG was removed from the Agent runtime")
def test_retriever_falls_back_to_query_texts_when_query_embeddings_fail() -> None:
    class _EmbeddingMismatchCollection:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def query(self, **kwargs):
            self.calls.append(kwargs)
            if "query_embeddings" in kwargs:
                raise ValueError("Collection expecting embedding with dimension of 384, got 1536")
            return {
                "ids": [["doc-1"]],
                "documents": [["Recovered via text query"]],
                "metadatas": [[{"source": "documents"}]],
                "distances": [[0.05]],
            }

    collection = _EmbeddingMismatchCollection()
    retriever = Retriever(default_top_k=3, default_min_score=0.7)

    chunks = retriever.retrieve(
        query="Scientific Data 数据集 贡献",
        collection=collection,
        top_k=2,
        min_score=0.7,
        query_embeddings=[[0.1, 0.2, 0.3]],
    )

    assert [chunk.content for chunk in chunks] == ["Recovered via text query"]
    assert collection.calls[0]["query_embeddings"] == [[0.1, 0.2, 0.3]]
    assert collection.calls[1]["query_texts"] == ["Scientific Data 数据集 贡献"]


def test_mcp_client_does_not_replay_timed_out_tool_call() -> None:
    class _RetryingClient(MCPClient):
        def __init__(self) -> None:
            super().__init__(server_name="websearch")
            self._connected = True
            self._read_only_tools.add("mcp__websearch__search")
            self.calls: list[tuple[str, dict[str, object] | None]] = []

        async def _request(self, method: str, params: dict[str, object] | None = None):
            self.calls.append((method, params))
            from mcp import types
            from mcp.shared.exceptions import McpError

            raise McpError(types.ErrorData(code=408, message="Timed out"))

    client = _RetryingClient()

    result = asyncio.run(client.call_tool("mcp__websearch__search", {"query": "北京今天天气"}))

    assert result.is_error is True
    assert "timed out" in result.text.lower()
    assert len(client.calls) == 1


def test_mcp_manager_uses_default_tool_timeout_without_name_special_cases() -> None:
    manager = MCPServerManager()
    client = manager._create_client(  # type: ignore[attr-defined]
        MCPServerConfig(name="external", command="external-mcp")
    )

    assert client._tool_timeout == 100_000.0


def test_load_config_normalizes_legacy_orchestrator_settings(tmp_path, monkeypatch) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "agent": {
                    "agent_mode": "planner",
                    "orchestrator_complexity_threshold": 7,
                    "plan_max_steps": 5,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", settings_file)

    config = load_config()

    assert config.agent.agent_mode == "react"
    assert not hasattr(config.agent, "orchestrator_complexity_threshold")
    assert not hasattr(config.agent, "plan_max_steps")


def test_task_tool_runs_subagent_with_events_and_isolated_context() -> None:
    class _SubagentLLM(LLMAdapter):
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def stream_chat(self, messages: list[LLMMessage], tools=None):
            self.prompts.append(messages[-1].content)
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="explored subsystem")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages: list[LLMMessage]) -> str:
            return ""

    llm = _SubagentLLM()
    emitted: list[tuple[str, dict[str, object]]] = []

    async def emit_event(event_type: str, data: dict[str, object]) -> None:
        emitted.append((event_type, data))

    tool = TaskTool(
        llm_provider=llm,
        tool_registry_provider=ToolRegistry(),
        artifact_store=ArtifactStore(),
        permission_checker_provider=PermissionChecker(PermissionSettings()),
        agent_settings_provider=AgentSettings(max_iterations=1),
        token_budget_provider=TokenBudget(),
    )

    runtime = AgentRuntime()
    result = asyncio.run(
        tool.execute(
            {
                "description": "Inspect routing",
                "prompt": "Find how routing is wired.",
                "agent_type": "explore",
            },
            context=ToolExecutionContext(
                permission=PermissionChecker(PermissionSettings()).build_context(mode="confirm"),
                session_id="session-1",
                task_id="parent-1",
                emit_event=emit_event,
                run_context=RunContext(agent_runtime=runtime),
            ),
        )
    )

    assert result.is_error is False
    assert "explored subsystem" in result.content
    event_types = [event_type for event_type, _data in emitted]
    assert event_types[0] == "subagent.start"
    assert event_types[-1] == "subagent.done"
    assert set(event_types[1:-1]) <= {"subagent.progress"}
    assert event_types.count("subagent.progress") >= 1
    assert emitted[0][1]["role"] == "explore"
    assert "read-only exploration agent" in llm.prompts[0]
    assert "task" in TaskTool._build_permission_context(
        "explore",
        ToolExecutionContext(
                permission=PermissionChecker(PermissionSettings()).build_context(mode="confirm"),
        ),
    ).tool_deny_rules


def test_task_tool_blocks_recursive_subagent_calls() -> None:
    tool = TaskTool(
        llm_provider=_DoneLLM(),
        tool_registry_provider=ToolRegistry(),
        artifact_store=ArtifactStore(),
        permission_checker_provider=PermissionChecker(PermissionSettings()),
        agent_settings_provider=AgentSettings(max_iterations=1),
        token_budget_provider=TokenBudget(),
    )

    result = asyncio.run(
        tool.execute(
            {
                "description": "Nested delegation",
                "prompt": "Spawn another agent.",
            },
            context=ToolExecutionContext(
                permission=PermissionContext(mode="auto", tool_deny_rules=["task"], source="subagent:explore"),
                task_id="subagent-parent",
            ),
        )
    )

    assert result.is_error is True
    assert result.status == "blocked"
    assert "recursive subagent" in result.content.casefold()


def test_task_tool_parallel_tasks_are_valid_without_single_task_fields() -> None:
    from backend.agent.tool_execution import (
        missing_required_tool_argument_names,
    )

    tool = TaskTool(
        llm_provider=_DoneLLM(),
        tool_registry_provider=ToolRegistry(),
        artifact_store=ArtifactStore(),
        permission_checker_provider=PermissionChecker(PermissionSettings()),
        agent_settings_provider=AgentSettings(max_iterations=1),
        token_budget_provider=TokenBudget(),
    )
    registry = ToolRegistry()
    registry.register(tool)
    call = ToolCallEvent(
        id="task_parallel",
        name="task",
        arguments={
            "parallel_tasks": [
                {"description": "Inspect routing", "prompt": "Find routing code."},
                {"description": "Inspect state", "prompt": "Find state code."},
            ],
        },
    )

    assert "description" not in tool.get_schema().parameters.get("required", [])
    assert "prompt" not in tool.get_schema().parameters.get("required", [])
    assert tool.get_schema().parameters.get("anyOf")
    assert missing_required_tool_argument_names(call, registry) == []


def test_workspace_overview_uses_list_files_through_agent_loop(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    (tmp_path / "backend").mkdir()

    class _WorkspaceOverviewLLM(LLMAdapter):
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, messages: list[LLMMessage], tools: list[dict[str, object]] | None = None):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL,
                    tool_calls=[
                        ToolCallEvent(id="list_workspace", name="list_files", arguments={"directory": "."})
                    ],
                )
                yield StreamEvent(type=StreamEventType.DONE)
                return
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="README.md\nbackend/")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages: list[LLMMessage]) -> str:
            return "README.md\nbackend/"

    registry = ToolRegistry()
    from backend.tools.file_tools import ListFilesTool

    registry.register(ListFilesTool())

    events = asyncio.run(
        _collect_events(
            run_agent_loop(
                user_message="当前工作区有什么",
                llm=_WorkspaceOverviewLLM(),
                tool_registry=registry,
                artifact_store=ArtifactStore(),
                permission_checker=PermissionChecker(PermissionSettings(), workspace_root=tmp_path),
                agent_settings=AgentSettings(max_iterations=2),
                token_budget=TokenBudget(),
                session_context=AgentLoopSessionContext(workspace_root=tmp_path),
            )
        )
    )

    tool_calls = [event.data for event in events if event.type == "tool_call"]
    tool_results = [event.data for event in events if event.type == "tool_result"]
    text = "".join(
        event.data.get("item", {}).get("text", "")
        for event in events
        if event.type == "item.completed"
    )

    assert tool_calls[0]["name"] == "list_files"
    assert tool_results[0]["result_kind"] == "file"
    assert "README.md" in text
    assert "backend/" in text


@pytest.mark.skip(reason="vector memory was removed in favor of file memory")
def test_vector_memory_fallback_recall_uses_prebuilt_token_index(monkeypatch, tmp_path) -> None:
    def force_fallback(self) -> None:
        self._client = None
        self._collection = None
        self._load_fallback_entries()

    monkeypatch.setattr(VectorMemory, "_init_backend", force_fallback)

    vm = VectorMemory(storage_dir=tmp_path)
    original_tokenize = vm._tokenize
    calls = {"count": 0}

    def counting_tokenize(text: str):
        calls["count"] += 1
        return original_tokenize(text)

    vm._tokenize = counting_tokenize  # type: ignore[method-assign]
    vm.remember("python websocket search")
    vm.remember("typescript stream batching")
    calls["count"] = 0

    results = vm.recall("python websocket", top_k=5, min_score=0.0)

    assert results
    assert calls["count"] == 1


@pytest.mark.skip(reason="vector memory was removed in favor of file memory")
def test_vector_memory_fallback_auto_flushes_after_batch_threshold(monkeypatch, tmp_path) -> None:
    def force_fallback(self) -> None:
        self._client = None
        self._collection = None
        self._load_fallback_entries()

    monkeypatch.setattr(VectorMemory, "_init_backend", force_fallback)

    vm = VectorMemory(storage_dir=tmp_path)
    flushes: list[int] = []

    def fake_save() -> None:
        flushes.append(len(vm._fallback_entries))
        vm._dirty = False

    monkeypatch.setattr(vm, "_save_fallback_entries", fake_save)

    for index in range(9):
        vm.remember(f"entry-{index}")

    assert flushes == []

    vm.remember("entry-9")

    assert flushes == [10]


@pytest.mark.skip(reason="ChromaDB was removed from the desktop runtime")
def test_vector_memory_reuses_persistent_client_for_same_storage_dir(monkeypatch, tmp_path) -> None:
    import sys
    import types

    class FakeClient:
        instances = 0

        def __init__(self, *, path: str) -> None:
            type(self).instances += 1
            self.path = path
            self.collections: dict[str, object] = {}

        def get_or_create_collection(self, name: str) -> object:
            self.collections.setdefault(name, object())
            return self.collections[name]

    fake_chromadb = types.SimpleNamespace(PersistentClient=FakeClient)
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)
    VectorMemory._shared_clients = {}  # type: ignore[attr-defined]

    first = VectorMemory(storage_dir=tmp_path, collection_name="memory")
    second = VectorMemory(storage_dir=tmp_path, collection_name="documents")

    assert first._client is second._client
    assert FakeClient.instances == 1


@pytest.mark.skip(reason="ChromaDB was removed from the desktop runtime")
def test_vector_memory_disables_chroma_product_telemetry(monkeypatch, tmp_path) -> None:
    import sys
    import types

    class FakeSettings:
        def __init__(
            self,
            *,
            anonymized_telemetry: bool = True,
            chroma_product_telemetry_impl: str = "",
            chroma_telemetry_impl: str = "",
        ) -> None:
            self.anonymized_telemetry = anonymized_telemetry
            self.chroma_product_telemetry_impl = chroma_product_telemetry_impl
            self.chroma_telemetry_impl = chroma_telemetry_impl

    class FakeClient:
        calls: list[dict[str, object]] = []

        def __init__(self, *, path: str, settings=None) -> None:
            type(self).calls.append({"path": path, "settings": settings})

        def get_or_create_collection(self, name: str, metadata=None) -> object:
            return object()

    fake_chromadb = types.SimpleNamespace(
        PersistentClient=FakeClient,
        config=types.SimpleNamespace(Settings=FakeSettings),
    )
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)
    VectorMemory._shared_clients = {}  # type: ignore[attr-defined]

    VectorMemory(storage_dir=tmp_path, collection_name="memory")

    settings = FakeClient.calls[0]["settings"]
    assert settings is not None
    assert settings.anonymized_telemetry is False
    assert settings.chroma_product_telemetry_impl == "backend.chroma_telemetry.NoopProductTelemetryClient"
    assert settings.chroma_telemetry_impl == "backend.chroma_telemetry.NoopProductTelemetryClient"


@pytest.mark.skip(reason="ChromaDB was removed from the desktop runtime")
def test_rag_pipeline_disables_chroma_product_telemetry(monkeypatch, tmp_path) -> None:
    import sys
    import types

    class FakeSettings:
        def __init__(
            self,
            *,
            anonymized_telemetry: bool = True,
            chroma_product_telemetry_impl: str = "",
            chroma_telemetry_impl: str = "",
        ) -> None:
            self.anonymized_telemetry = anonymized_telemetry
            self.chroma_product_telemetry_impl = chroma_product_telemetry_impl
            self.chroma_telemetry_impl = chroma_telemetry_impl

    class FakeCollection:
        def count(self) -> int:
            return 1

    class FakeClient:
        calls: list[dict[str, object]] = []

        def __init__(self, *, path: str, settings=None) -> None:
            type(self).calls.append({"path": path, "settings": settings})

        def get_or_create_collection(self, name: str, metadata=None) -> object:
            return FakeCollection()

    fake_chromadb = types.SimpleNamespace(
        PersistentClient=FakeClient,
        config=types.SimpleNamespace(Settings=FakeSettings),
    )
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)
    monkeypatch.setattr("backend.rag.pipeline.DATA_DIR", tmp_path)

    pipeline = RAGPipeline()

    assert pipeline.is_available() is True
    settings = FakeClient.calls[0]["settings"]
    assert settings is not None
    assert settings.anonymized_telemetry is False
    assert settings.chroma_product_telemetry_impl == "backend.chroma_telemetry.NoopProductTelemetryClient"
    assert settings.chroma_telemetry_impl == "backend.chroma_telemetry.NoopProductTelemetryClient"


@pytest.mark.skip(reason="ChromaDB was removed from the desktop runtime")
def test_vector_memory_uses_tuned_hnsw_collection_metadata(monkeypatch, tmp_path) -> None:
    import sys
    import types

    class FakeClient:
        def __init__(self, *, path: str) -> None:
            self.path = path
            self.calls: list[dict[str, object]] = []

        def get_or_create_collection(self, name: str, metadata=None) -> object:
            self.calls.append({"name": name, "metadata": metadata})
            return object()

    fake_chromadb = types.SimpleNamespace(PersistentClient=FakeClient)
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)
    VectorMemory._shared_clients = {}  # type: ignore[attr-defined]

    memory = VectorMemory(storage_dir=tmp_path, collection_name="memory")

    assert memory._client.calls[0]["metadata"] == {  # type: ignore[union-attr]
        "hnsw:space": "cosine",
        "hnsw:ef_construction": 200,
        "hnsw:M": 32,
        "hnsw:search_ef": 100,
    }


@pytest.mark.skip(reason="ChromaDB was removed from the desktop runtime")
def test_rag_pipeline_uses_tuned_hnsw_collection_metadata(monkeypatch, tmp_path) -> None:
    import sys
    import types

    class FakeCollection:
        def count(self) -> int:
            return 1

    class FakeClient:
        instances: list["FakeClient"] = []

        def __init__(self, *, path: str) -> None:
            self.path = path
            self.calls: list[dict[str, object]] = []
            type(self).instances.append(self)

        def get_or_create_collection(self, name: str, metadata=None) -> object:
            self.calls.append({"name": name, "metadata": metadata})
            return FakeCollection()

    fake_chromadb = types.SimpleNamespace(PersistentClient=FakeClient)
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)
    monkeypatch.setattr("backend.rag.pipeline.DATA_DIR", tmp_path)

    pipeline = RAGPipeline()

    assert pipeline.is_available() is True
    assert FakeClient.instances
    for call in FakeClient.instances[0].calls:
        assert call["metadata"] == {
            "hnsw:space": "cosine",
            "hnsw:ef_construction": 200,
            "hnsw:M": 32,
            "hnsw:search_ef": 100,
        }


@pytest.mark.skip(reason="ChromaDB was removed from the desktop runtime")
def test_rag_pipeline_falls_back_when_existing_collection_rejects_hnsw_metadata(monkeypatch, tmp_path) -> None:
    import sys
    import types

    class InvalidArgumentError(Exception):
        pass

    class FakeCollection:
        def __init__(self, name: str) -> None:
            self.name = name

        def count(self) -> int:
            return 1

    class FakeClient:
        def __init__(self, *, path: str) -> None:
            self.path = path
            self.calls: list[dict[str, object]] = []

        def get_or_create_collection(self, name: str, metadata=None) -> object:
            self.calls.append({"name": name, "metadata": metadata})
            if metadata is not None:
                raise InvalidArgumentError("Failed to parse hnsw parameters from segment metadata")
            return FakeCollection(name)

    fake_chromadb = types.SimpleNamespace(
        PersistentClient=FakeClient,
        errors=types.SimpleNamespace(InvalidArgumentError=InvalidArgumentError),
    )
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)
    monkeypatch.setattr("backend.rag.pipeline.DATA_DIR", tmp_path)

    pipeline = RAGPipeline()

    assert pipeline.is_available() is True
    assert pipeline.stats["available"] is True
    assert pipeline.stats["collections"] == {"memory": 1, "documents": 1, "codebase": 1}


def test_tool_registry_rejects_conflicting_tool_names(caplog) -> None:
    # Registration now fails closed instead of warning and silently overriding:
    # a plugin/MCP tool must not be able to shadow an already-registered name
    # (backend/tools/registry.py::register). Replacement is possible only when
    # the caller asks for it explicitly, and that path still warns and names
    # the previous owner.
    registry = ToolRegistry()
    first = _StaticTool("duplicate_tool", "first")
    registry.register(first, owner="builtin")

    with pytest.raises(ValueError, match="Tool name conflict for 'duplicate_tool'"):
        registry.register(_StaticTool("duplicate_tool", "second"))

    assert registry.get_tool("duplicate_tool") is first
    assert registry.get_tool_owner("duplicate_tool") == "builtin"

    replacement = _StaticTool("duplicate_tool", "second")
    with caplog.at_level(logging.WARNING):
        registry.register(replacement, replace=True, owner="plugin")

    assert "duplicate_tool" in caplog.text
    assert registry.get_tool("duplicate_tool") is replacement
    assert registry.get_tool_owner("duplicate_tool") == "plugin"


def test_tool_registry_executes_read_tools_again_instead_of_serving_stale_results() -> None:
    registry = ToolRegistry()
    read_tool = _CountingTool("read_file")
    registry.register(read_tool)

    first = asyncio.run(registry.execute("read_file", {"file_path": "README.md"}))
    second = asyncio.run(registry.execute("read_file", {"file_path": "README.md"}))

    assert first.content != second.content
    assert read_tool.calls == 2


def test_tool_registry_executes_reads_independently_in_each_workspace(tmp_path: Path) -> None:
    registry = ToolRegistry()
    read_tool = _CountingTool("read_file")
    registry.register(read_tool)
    workspace_a = tmp_path / "project-a"
    workspace_b = tmp_path / "project-b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    context_a = ToolExecutionContext(
        permission=PermissionContext(mode="confirm", source="test"),
        workspace_root=workspace_a,
    )
    context_b = ToolExecutionContext(
        permission=PermissionContext(mode="confirm", source="test"),
        workspace_root=workspace_b,
    )

    first = asyncio.run(registry.execute("read_file", {"file_path": "README.md"}, context=context_a))
    second = asyncio.run(registry.execute("read_file", {"file_path": "README.md"}, context=context_b))
    third = asyncio.run(registry.execute("read_file", {"file_path": "README.md"}, context=context_a))

    assert first.content != second.content
    assert third.content != first.content
    assert read_tool.calls == 3


def test_tool_registry_reads_again_after_write() -> None:
    registry = ToolRegistry()
    read_tool = _CountingTool("read_file")
    write_tool = _CountingTool("write_file")
    read_tool.read_only = True
    write_tool.mutates_workspace = True
    registry.register(read_tool)
    registry.register(write_tool)

    first = asyncio.run(registry.execute("read_file", {"file_path": "README.md"}))
    asyncio.run(registry.execute("write_file", {"file_path": "README.md", "content": "updated"}))
    second = asyncio.run(registry.execute("read_file", {"file_path": "README.md"}))

    assert first.content != second.content
    assert read_tool.calls == 2
    assert write_tool.calls == 1


def test_status_and_llm_settings_endpoints_set_short_cache_headers() -> None:
    with TestClient(app) as client:
        status_response = client.get("/api/status")
        settings_response = client.get("/api/llm/settings")

    assert status_response.headers["Cache-Control"] == "public, max-age=30"
    assert settings_response.headers["Cache-Control"] in {"public, max-age=30", "no-store"}


def test_status_endpoint_reuses_short_lived_server_cache(monkeypatch) -> None:
    calls = {"count": 0}

    def fake_build_status_payload() -> dict[str, object]:
        calls["count"] += 1
        return {
            "mcp": [{"name": "docs", "status": "connected", "tools_count": 1}],
            "skills": [],
            "memory": {"available": False, "files": []},
            "rag": {"available": False},
            "llm": {"provider": "openai", "current_model": "gpt-5.4", "active_model": "gpt-5.4", "available_models": ["gpt-5.4"]},
        }

    monkeypatch.setattr("backend.main._build_status_payload", fake_build_status_payload)
    monkeypatch.setattr("backend.main._status_cache_payload", None, raising=False)
    monkeypatch.setattr("backend.main._status_cache_expires_at", 0.0, raising=False)

    with TestClient(app) as client:
        first = client.get("/api/status")
        second = client.get("/api/status")

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls["count"] == 1


def test_openai_adapter_does_not_synthesize_tool_results_for_history_repair() -> None:
    source = Path("backend/llm/openai_adapter.py").read_text(encoding="utf-8")

    assert "_repair_tool_call_message_sequence" not in source
    assert "Tool call did not complete before the next model turn" not in source


def test_git_diff_commands_reject_workspace_outside_session(monkeypatch, tmp_path) -> None:
    from backend.ws.handlers.diff import handle_diff_git_working_tree

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    calls: list[str] = []

    async def fake_get_working_tree_diff(workspace_root: str):
        calls.append(workspace_root)
        from backend.diff.git_integration import StructuredDiff

        return StructuredDiff()

    async def fake_get_untracked_files(_workspace: str):
        return []

    monkeypatch.setattr("backend.diff.git_integration.get_working_tree_diff", fake_get_working_tree_diff)
    monkeypatch.setattr("backend.diff.git_integration.get_untracked_files", fake_get_untracked_files)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")
    monkeypatch.setattr("backend.ws.session_lifecycle.SessionLifecycle.current_workspace_root", lambda self: workspace)

    async def scenario() -> list[dict[str, object]]:
        session = WebSocketSession(
            session_id="session-git-diff-boundary",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        await handle_diff_git_working_tree(session, {"workspace": str(outside)})
        await handle_diff_git_working_tree(session, {})
        return session.ws.sent

    sent = asyncio.run(scenario())

    assert calls == [str(workspace.resolve())]
    assert sent[0]["type"] in {"error", "command.result"}
    assert "inside current session workspace" in str(sent[0].get("message", sent[0].get("error", "")))
    assert sent[1]["type"] == "diff.git_working_tree"


def test_git_stage_commands_reject_absolute_or_parent_paths(monkeypatch, tmp_path) -> None:
    from backend.ws.handlers.diff import handle_diff_git_stage_file

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[tuple[str, str]] = []

    async def fake_stage_file(workspace_root: str, path: str) -> bool:
        calls.append((workspace_root, path))
        return True

    monkeypatch.setattr("backend.diff.git_integration.stage_file", fake_stage_file)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")
    monkeypatch.setattr("backend.ws.session_lifecycle.SessionLifecycle.current_workspace_root", lambda self: workspace)

    async def scenario() -> list[dict[str, object]]:
        session = WebSocketSession(
            session_id="session-git-stage-boundary",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        await handle_diff_git_stage_file(session, {"path": str(tmp_path / "outside.txt")})
        await handle_diff_git_stage_file(session, {"path": "../outside.txt"})
        await handle_diff_git_stage_file(session, {"path": "src/app.py"})
        return session.ws.sent

    sent = asyncio.run(scenario())

    assert calls == [(str(workspace.resolve()), "src/app.py")]
    types = [item["type"] for item in sent]
    assert types[-1] == "diff.git_stage_file"
    assert all(t in {"error", "command.result"} for t in types[:-1])
    assert sent[-1]["path"] == "src/app.py"


def test_git_revert_command_rejects_absolute_or_parent_paths(monkeypatch, tmp_path) -> None:
    from backend.ws.handlers.diff import handle_diff_git_revert_file

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[tuple[str, str]] = []

    async def fake_revert_file(workspace_root: str, path: str) -> bool:
        calls.append((workspace_root, path))
        return True

    monkeypatch.setattr("backend.diff.git_integration.revert_file", fake_revert_file)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")
    monkeypatch.setattr("backend.ws.session_lifecycle.SessionLifecycle.current_workspace_root", lambda self: workspace)

    async def scenario() -> list[dict[str, object]]:
        session = WebSocketSession(
            session_id="session-git-revert-boundary",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        await handle_diff_git_revert_file(session, {"path": str(tmp_path / "outside.txt")})
        await handle_diff_git_revert_file(session, {"path": "../outside.txt"})
        await handle_diff_git_revert_file(session, {"path": "src/app.py", "confirmed": True})
        return session.ws.sent

    sent = asyncio.run(scenario())

    assert calls == [(str(workspace.resolve()), "src/app.py")]
    types = [item["type"] for item in sent]
    assert types[-1] == "diff.git_revert_file"
    assert all(t in {"error", "command.result"} for t in types[:-1])
    assert sent[-1]["path"] == "src/app.py"


def test_git_stage_all_commands_reject_workspace_outside_session(monkeypatch, tmp_path) -> None:
    from backend.ws.handlers.diff import handle_diff_git_stage_all, handle_diff_git_unstage_all

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    calls: list[tuple[str, str]] = []

    async def fake_stage_all(workspace_root: str) -> bool:
        calls.append(("stage", workspace_root))
        return True

    async def fake_unstage_all(workspace_root: str) -> bool:
        calls.append(("unstage", workspace_root))
        return True

    monkeypatch.setattr("backend.diff.git_integration.stage_all", fake_stage_all)
    monkeypatch.setattr("backend.diff.git_integration.unstage_all", fake_unstage_all)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")
    monkeypatch.setattr("backend.ws.session_lifecycle.SessionLifecycle.current_workspace_root", lambda self: workspace)

    async def scenario() -> list[dict[str, object]]:
        session = WebSocketSession(
            session_id="session-git-stage-all-boundary",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        await handle_diff_git_stage_all(session, {"workspace": str(outside)})
        await handle_diff_git_unstage_all(session, {"workspace": str(outside)})
        await handle_diff_git_stage_all(session, {})
        await handle_diff_git_unstage_all(session, {})
        return session.ws.sent

    sent = asyncio.run(scenario())

    resolved = str(workspace.resolve())
    assert calls == [("stage", resolved), ("unstage", resolved)]
    types = [item["type"] for item in sent]
    assert types[-2:] == ["diff.git_stage_all", "diff.git_unstage_all"]
    assert all(t in {"error", "command.result"} for t in types[:-2])


def test_git_stage_all_commands_are_scoped_to_workspace(monkeypatch) -> None:
    from backend.diff import git_integration

    calls: list[tuple[str, tuple[str, ...]]] = []

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*args, cwd, stdout, stderr, env=None, **kwargs):
        assert isinstance(env, dict)
        calls.append((cwd, tuple(args)))
        return FakeProc()

    monkeypatch.setattr(git_integration.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    async def scenario() -> None:
        await git_integration.stage_all("workspace")
        await git_integration.unstage_all("workspace")

    asyncio.run(scenario())

    assert calls == [
        ("workspace", ("git", "add", "--all", "--", ".")),
        ("workspace", ("git", "reset", "HEAD", "--", ".")),
    ]


def test_preview_launch_config_rejects_workspace_outside_session(monkeypatch, tmp_path) -> None:
    from backend.ws.handlers.preview import handle_preview_launch_config

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    calls: list[str] = []

    def fake_load_preview_launch_configs(workspace_root: str | Path):
        calls.append(str(Path(workspace_root).resolve()))
        return []

    monkeypatch.setattr("backend.preview.load_preview_launch_configs", fake_load_preview_launch_configs)
    monkeypatch.setattr("backend.preview.running_preview_processes", lambda **_kwargs: [])
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")
    monkeypatch.setattr("backend.ws.session_lifecycle.SessionLifecycle.current_workspace_root", lambda self: workspace)

    async def scenario() -> list[dict[str, object]]:
        session = WebSocketSession(
            session_id="session-preview-config-boundary",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        conversation = session.conversation_repo.create_conversation(
            conversation_id="conv-preview-config-boundary",
            workspace_root=str(workspace),
        )
        session.active_conversation_id = conversation.id
        await handle_preview_launch_config(session, {"workspace_root": str(outside)})
        await handle_preview_launch_config(session, {})
        return session.ws.sent

    sent = asyncio.run(scenario())

    assert calls == [str(workspace.resolve())]
    assert sent[0]["type"] in {"error", "command.result"}
    assert "inside current session workspace" in str(sent[0]["message"])
    assert sent[1]["type"] == "preview.launch.config"
    assert sent[1]["workspace_root"] == str(workspace.resolve())
    assert sent[1]["conversation_id"] == "conv-preview-config-boundary"


def test_preview_launch_start_rejects_workspace_outside_session(monkeypatch, tmp_path) -> None:
    from backend.preview.launcher import PreviewLaunchConfig
    from backend.preview.verifier import PreviewVerification
    from backend.ws.handlers.preview import handle_preview_launch_start

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    calls: list[str] = []

    class FakeProcess:
        pid = 1234
        returncode = None

    class FakeLaunch:
        id = "web"
        config = PreviewLaunchConfig(
            name="web",
            command="npm run dev",
            cwd=str(workspace),
            port=5173,
            url="http://127.0.0.1:5173",
        )
        process = FakeProcess()
        status = "starting"

        @property
        def effective_url(self) -> str:
            return self.config.url

        @property
        def effective_port(self) -> int:
            return self.config.port

        def to_dict(self) -> dict[str, object]:
            return {
                "id": "web",
                "name": "web",
                "command": "npm run dev",
                "cwd": str(workspace),
                "port": 5173,
                "url": "http://127.0.0.1:5173",
                "pid": 1234,
                "status": "starting",
            }

    async def fake_start_preview_launch(
        workspace_root: str | Path,
        name=None,
        broadcast=None,
        *,
        session_id: str,
        conversation_id: str,
    ):
        calls.append(str(Path(workspace_root).resolve()))
        assert session_id == "session-preview-start-boundary"
        assert conversation_id == "conv-preview-start-boundary"
        return FakeLaunch()

    async def fake_wait_until_ready(url: str, timeout: float = 20.0, interval: float = 1.0):
        return PreviewVerification(url=url, ok=True, status_code=200, elapsed_ms=1, error="")

    async def fake_mark_preview_ready(process, broadcast=None):
        process.status = "ready"

    monkeypatch.setattr("backend.preview.start_preview_launch", fake_start_preview_launch)
    monkeypatch.setattr("backend.preview.mark_preview_ready", fake_mark_preview_ready)
    monkeypatch.setattr("backend.preview.verifier.wait_until_ready", fake_wait_until_ready)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations")
    monkeypatch.setattr("backend.ws.session_lifecycle.SessionLifecycle.current_workspace_root", lambda self: workspace)

    async def scenario() -> list[dict[str, object]]:
        session = WebSocketSession(
            session_id="session-preview-start-boundary",
            websocket=_FakeWebSocket(),
            llm=_HungLLM(),
            artifact_store=ArtifactStore(),
            tool_registry=ToolRegistry(),
            permission_checker=PermissionChecker(PermissionSettings()),
            config=AppConfig(llm=LLMSettings(api_key="")),
        )
        conversation = session.conversation_repo.create_conversation(
            conversation_id="conv-preview-start-boundary",
            workspace_root=str(workspace),
        )
        session.active_conversation_id = conversation.id
        await handle_preview_launch_start(session, {"workspace_root": str(outside)})
        await handle_preview_launch_start(session, {})
        return session.ws.sent

    sent = asyncio.run(scenario())

    assert calls == [str(workspace.resolve())]
    assert sent[0]["type"] in {"error", "command.result"}
    assert "inside current session workspace" in str(sent[0]["message"])
    assert [item["type"] for item in sent[1:4]] == [
        "preview.launch.started",
        "preview.server.detected",
        "preview.verified",
    ]
    assert all(
        item["conversation_id"] == "conv-preview-start-boundary"
        for item in sent[1:4]
    )


def test_preview_mutations_fail_closed_without_active_conversation(monkeypatch) -> None:
    from backend.ws.handlers.preview import (
        handle_preview_launch_stop,
        handle_preview_refresh,
        handle_preview_verify,
    )

    calls: list[str] = []

    async def forbidden_stop(*_args, **_kwargs):
        calls.append("stop")
        return []

    async def forbidden_verify(*_args, **_kwargs):
        calls.append("verify")
        raise AssertionError("preview verification must not run without an owner")

    monkeypatch.setattr("backend.preview.stop_preview_launch", forbidden_stop)
    monkeypatch.setattr("backend.preview.verify_preview_url", forbidden_verify)

    class Session:
        active_conversation_id = None

        def __init__(self) -> None:
            self.events: list[AgentEvent] = []

        async def send_event(self, event: AgentEvent) -> None:
            self.events.append(event)

    async def scenario() -> list[AgentEvent]:
        session = Session()
        await handle_preview_launch_stop(session, {"name": "web"})
        await handle_preview_refresh(session, {"url": "http://127.0.0.1:5173"})
        await handle_preview_verify(session, {"url": "http://127.0.0.1:5173"})
        return session.events

    events = asyncio.run(scenario())

    assert calls == []
    assert [event.type for event in events] == ["command.result"] * 3
    assert all(event.data.get("level") == "error" for event in events)


def test_mcp_manager_reuses_existing_client_for_reconnect(monkeypatch) -> None:
    async def fast_sleep(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr("backend.mcp.manager.MCPClient", _ReusableClient)
    monkeypatch.setattr("backend.mcp.manager.asyncio.sleep", fast_sleep)

    async def scenario() -> MCPServerManager:
        manager = MCPServerManager()
        config = MCPServerConfig(name="docs", transport="http", url="https://example.test/mcp")
        await manager.start_server(config)
        state = manager._servers["docs"]
        assert state.client is not None
        state.client.connected = False
        await manager._try_automatic_remote_reconnect("docs")
        return manager

    manager = asyncio.run(scenario())
    state = manager._servers["docs"]

    assert _ReusableClient.instances == 2
    assert state.client is not None
    assert state.client.connect_calls == 1


def test_mcp_manager_keeps_explicit_start_recoverable_after_failures(monkeypatch) -> None:
    _FailingReusableClient.instances = 0
    _FailingReusableClient.closed_count = 0
    monkeypatch.setattr("backend.mcp.manager.MCPClient", _FailingReusableClient)

    async def scenario() -> MCPServerManager:
        manager = MCPServerManager()
        config = MCPServerConfig(name="unstable")
        for _ in range(2):
            await manager.start_server(config)
        return manager

    manager = asyncio.run(scenario())
    state = manager._servers["unstable"]

    assert state.status in {ServerStatus.ERROR, ServerStatus.OFFLINE}
    assert "boom" in state.last_error
    assert state.client is None
    instances = _FailingReusableClient.instances
    closed_count = _FailingReusableClient.closed_count

    asyncio.run(manager.start_server(state.config))

    assert _FailingReusableClient.instances == instances + 1
    assert _FailingReusableClient.closed_count == closed_count + 1


def test_anthropic_thinking_follows_explicit_budget_without_task_heuristics() -> None:
    adapter = AnthropicAdapter(api_key="test", thinking_budget=2048)

    assert adapter._should_enable_thinking([LLMMessage(role="user", content="hi")], []) is True
    assert adapter._should_enable_thinking(
        [LLMMessage(role="user", content="debug this failing test")],
        [],
    ) is True
    assert adapter._should_enable_thinking([LLMMessage(role="user", content="hi")], [{"name": "read_file"}]) is True
    assert adapter._should_enable_thinking(
        [
            LLMMessage(
                role="user",
                content="summarize",
                documents=[{"media_type": "application/pdf", "data": "pdf123"}],
            )
        ],
        [],
    ) is True
    disabled = AnthropicAdapter(api_key="test", thinking_budget=0)
    assert disabled._should_enable_thinking(
        [LLMMessage(role="user", content="debug this failing test")],
        [{"name": "read_file"}],
    ) is False


def test_anthropic_raw_http_stream_error_surfaces(monkeypatch) -> None:
    monkeypatch.setattr("backend.llm.anthropic_adapter.httpx.AsyncClient", _SingleStreamErrorClient)
    adapter = AnthropicAdapter(api_key="test", base_url="https://gateway.example")

    events = asyncio.run(_collect_events(adapter.stream_chat([LLMMessage(role="user", content="hi")], tools=[])))

    assert events[0].type == StreamEventType.ERROR
    assert "Concurrency limit exceeded" in events[0].content
