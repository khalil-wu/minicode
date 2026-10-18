from __future__ import annotations

import asyncio
import threading
from contextlib import aclosing
from types import SimpleNamespace

import pytest

from backend.agent.message import AgentEvent
from backend.agent.query_engine import AgentSession
from backend.agent.runtime import AgentRuntime
from backend.artifact.store import ArtifactStore
from backend.async_cleanup import retain_cleanup_task
from backend.config import AgentSettings, AppConfig, LLMSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry


class _BoundaryModel(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        raise AssertionError("The lifecycle boundary test must not request a model")
        yield

    async def simple_chat(self, messages, **kwargs):
        raise AssertionError("The lifecycle boundary test must not request a model")


def _session(tmp_path, store, *, owns):
    return AgentSession(
        llm=object(), tool_registry=ToolRegistry(), artifact_store=store,
        owns_artifact_store=owns,
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
        agent_settings=AgentSettings(), token_budget=TokenBudget(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_close", [False, True])
@pytest.mark.parametrize("scan_fails", [False, True])
async def test_close_joins_actual_disk_scan_and_preserves_failure(
    tmp_path, monkeypatch, cancel_close, scan_fails,
):
    entered, release = threading.Event(), threading.Event()

    def scan(*_args):
        entered.set()
        assert release.wait(3), "test did not release the disk worker"
        if scan_fails:
            raise OSError("artifact-disk-scan-marker")
        return []

    monkeypatch.setattr("backend.artifact.store._scan_expired", scan)
    store = ArtifactStore(storage_dir=tmp_path / "artifacts", cleanup_interval_seconds=0.001)
    store.save("saved before closing", source="lifecycle")
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        cleanup = store._cleanup_task
        closing = asyncio.create_task(store.aclose())
        await asyncio.sleep(0)
        if cancel_close:
            closing.cancel()
            await asyncio.sleep(0)
        assert not closing.done()
        assert not cleanup.done() and cleanup.cancelling() == 0
    finally:
        release.set()
        if closing is not None:
            if scan_fails:
                with pytest.raises(RuntimeError, match="OSError: artifact-disk-scan-marker"):
                    await closing
            elif cancel_close:
                with pytest.raises(asyncio.CancelledError):
                    await closing
            else:
                await closing
        else:
            await store.aclose()
    assert cleanup.done() and not cleanup.cancelled()
    assert store.cleanup_status() == {
        "cleanup_pending": scan_fails,
        "error": "OSError: artifact-disk-scan-marker" if scan_fails else "",
    }


@pytest.mark.asyncio
async def test_borrowed_store_remains_usable_when_agent_session_closes(tmp_path):
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    async with aclosing(store):
        store.save("first", source="lifecycle")
        cleanup = store._cleanup_task
        await _session(tmp_path, store, owns=False).aclose()
        assert not store._cleanup_stop.is_set()
        assert not cleanup.done()
        store.save("second", source="lifecycle")
        assert store._cleanup_task is cleanup
    assert cleanup.done() and cleanup.result() is None


@pytest.mark.asyncio
async def test_owned_session_waits_for_late_registered_artifact_borrower(tmp_path):
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    session = _session(tmp_path, store, owns=True)
    release_first, child_entered, release_child = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def child():
        store.save("late child artifact", source="lifecycle")
        child_entered.set()
        await release_child.wait()

    async def first():
        await release_first.wait()
        retain_cleanup_task(asyncio.create_task(child()), session.lifecycle_cleanup_tasks)

    retain_cleanup_task(asyncio.create_task(first()), session.lifecycle_cleanup_tasks)
    await session.aclose()
    closer = session._command_shutdown_task
    try:
        assert not store._cleanup_stop.is_set() and not closer.done()
        release_first.set()
        await asyncio.wait_for(child_entered.wait(), 2)
        await asyncio.sleep(0)
        assert not store._cleanup_stop.is_set() and not closer.done()
        cleanup = store._cleanup_task
    finally:
        release_first.set()
        release_child.set()
        await closer
    assert cleanup.done() and cleanup.result() is None
    assert store._cleanup_stop.is_set()
    await session.aclose()


@pytest.mark.asyncio
async def test_deferred_session_close_reports_actual_scan_failure(tmp_path, monkeypatch, caplog):
    failed, release = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()

    def scan(*_args):
        loop.call_soon_threadsafe(failed.set)
        raise OSError("deferred-session-scan-marker")

    monkeypatch.setattr("backend.artifact.store._scan_expired", scan)
    store = ArtifactStore(storage_dir=tmp_path / "artifacts", cleanup_interval_seconds=0.001)
    session = _session(tmp_path, store, owns=True)
    retain_cleanup_task(asyncio.create_task(release.wait()), session.lifecycle_cleanup_tasks)
    store.save("borrowed artifact", source="lifecycle")
    await session.aclose()
    closer = session._command_shutdown_task
    try:
        await asyncio.wait_for(failed.wait(), 2)
        await store._cleanup_task
        assert not closer.done()
        release.set()
        with pytest.raises(RuntimeError, match="OSError: deferred-session-scan-marker"):
            await closer
        assert any(
            record.name == "backend.agent.query_engine"
            and "resource shutdown failed" in record.getMessage()
            and "deferred-session-scan-marker" in str(record.exc_info[1])
            for record in caplog.records
        )
        with pytest.raises(RuntimeError, match="OSError: deferred-session-scan-marker"):
            await session.aclose()
    finally:
        release.set()
        await asyncio.gather(closer, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["sdk", "rest", "memory", "eval"])
@pytest.mark.parametrize("fail_query", [False, True])
async def test_creator_transfers_store_to_session_on_success_and_error(
    tmp_path, monkeypatch, boundary, fail_query,
):
    import backend.evals.minicode_driver as driver
    import backend.memory.consolidation_agent as memory
    import backend.sdk as sdk
    import backend.services.chat_api_service as rest

    config = AppConfig(llm=LLMSettings(api_key=""))
    llm = _BoundaryModel()
    created, submissions = [], []

    def create_store():
        store = ArtifactStore(storage_dir=tmp_path / "artifacts")
        created.append(store)
        return store

    class SavingEngine:
        async def submit(self, submission):
            submissions.append(submission)
            submission.session.artifact_store.save("boundary artifact", source=boundary)
            if fail_query:
                raise RuntimeError("query-owner-marker")
            submission.state.terminal_status = "completed"
            yield AgentEvent.done(status="completed")

    module = {"sdk": sdk, "rest": rest, "memory": memory, "eval": driver}[boundary]
    monkeypatch.setattr(module, "ArtifactStore", create_store)
    monkeypatch.setattr(module, "QueryEngine", SavingEngine)
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", enable_lease_heartbeat=False)
    bootstrap = SimpleNamespace(
        create_tool_registry=lambda *_args, **_kwargs: ToolRegistry(),
        create_permission_checker=lambda **_kwargs: PermissionChecker(PermissionSettings(), tmp_path),
        create_llm=lambda **_kwargs: llm,
    )

    async def invoke():
        if boundary == "sdk":
            return [event async for event in sdk.query(
                "inspect", config=config, llm=llm, tool_registry=ToolRegistry(), workspace_root=tmp_path,
            )]
        if boundary == "rest":
            monkeypatch.setattr(rest, "default_runtime", lambda: runtime)
            monkeypatch.setattr(rest, "load_config", lambda **_kwargs: config)
            return await rest.run_rest_chat(
                message="inspect", max_iterations=1, bootstrap=bootstrap,
                query_engine=SavingEngine(), workspace_root=tmp_path,
            )
        if boundary == "memory":
            return await memory.run_memory_consolidation_agent(
                llm=llm, memory_root=tmp_path, prompt="inspect",
            )
        monkeypatch.setenv("MINICODE_EVAL_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("MINICODE_EVAL_API_KEY", "local-fixture")
        monkeypatch.setenv("MINICODE_EVAL_MODEL", "fixture")
        monkeypatch.setattr(driver, "build_wire_adapter", lambda *_args, **_kwargs: llm)
        monkeypatch.setattr(driver, "build_tool_registry", lambda *_args, **_kwargs: ToolRegistry())
        return await driver._run("inspect")

    try:
        if fail_query:
            with pytest.raises(RuntimeError, match="query-owner-marker"):
                await invoke()
        else:
            await invoke()
        assert len(created) == len(submissions) == 1
        store, submission = created[0], submissions[0]
        assert submission.session.owns_artifact_store
        assert store._cleanup_stop.is_set()
        assert store._cleanup_task.done() and store._cleanup_task.result() is None
        if boundary == "memory":
            assert store._owner_context.get() == ("", "")
    finally:
        for store in created:
            await store.aclose()
        runtime.close(release_lease=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("borrowed", [False, True])
async def test_sdk_mcp_closes_only_its_own_store_after_real_artifact_save(tmp_path, monkeypatch, borrowed):
    import backend.sdk as sdk

    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    monkeypatch.setattr(sdk, "ArtifactStore", lambda: store)

    @sdk.tool
    async def artifact_probe(context):
        context.artifact_store.save("MCP artifact", source="sdk-mcp")
        return "saved"

    try:
        output = await sdk._mcp_callable_for_tool(
            artifact_probe, workspace_root=tmp_path, artifact_store=store if borrowed else None,
        )()
        assert output.splitlines()[0] == "saved"
        assert store._cleanup_stop.is_set() is not borrowed
        assert store._cleanup_task.done() is not borrowed
    finally:
        await store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("scan_fails", [False, True])
async def test_sdk_mcp_timeout_retains_store_through_late_save_and_child(
    tmp_path, monkeypatch, scan_fails,
):
    import backend.sdk as sdk

    monkeypatch.setattr("backend.agent.execution_journal.JOURNAL_ROOT", tmp_path / "journals")
    monkeypatch.setattr("backend.agent.tool_execution.CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr("backend.tools.registry.CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.001)
    release, child_entered, child_release, failed = (asyncio.Event() for _ in range(4))
    loop = asyncio.get_running_loop()
    captured = []
    store = ArtifactStore(storage_dir=tmp_path / "artifacts", cleanup_interval_seconds=0.001)
    monkeypatch.setattr(sdk, "ArtifactStore", lambda: store)
    if scan_fails:
        def scan(*_args):
            loop.call_soon_threadsafe(failed.set)
            raise OSError("late-artifact-scan-marker")

        monkeypatch.setattr("backend.artifact.store._scan_expired", scan)

    @sdk.tool
    async def late_artifact(context):
        captured.append(context)
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        context.artifact_store.save("late result", source="sdk-mcp")

        async def child():
            child_entered.set()
            await child_release.wait()
            context.artifact_store.save("late child result", source="sdk-mcp")

        retain_cleanup_task(asyncio.create_task(child()), context.pending_cleanup_tasks)
        return "saved after timeout"

    late_artifact.timeout_seconds = 0.001
    output = await sdk._mcp_callable_for_tool(late_artifact, workspace_root=tmp_path)()
    context = captured[0]
    close_owner = next(task for task in context.pending_cleanup_tasks
                       if task.get_name().startswith("sdk-mcp-artifact-close["))
    try:
        assert "timed out" in output
        assert not store._cleanup_stop.is_set()
        release.set()
        await asyncio.wait_for(child_entered.wait(), 2)
        if scan_fails:
            await asyncio.wait_for(failed.wait(), 2)
        await asyncio.sleep(0)
        assert not close_owner.done() and not store._cleanup_stop.is_set()
    finally:
        release.set()
        child_release.set()
        await asyncio.gather(*tuple(context.pending_cleanup_tasks), return_exceptions=True)
        await asyncio.sleep(0)
    assert not context.pending_cleanup_tasks
    assert store._cleanup_task.done()
    receipt = context.cleanup_receipts[f"{context.tool_call_id}:artifact_store"]
    assert receipt["completed"] and receipt["pending"] == 0
    if scan_fails:
        assert "OSError: late-artifact-scan-marker" in receipt["error"]
        assert isinstance(close_owner.exception(), RuntimeError)
        journals = list((tmp_path / "journals").rglob("*.jsonl"))
        assert any("late-artifact-scan-marker" in path.read_text(encoding="utf-8") for path in journals)
    else:
        assert close_owner.result() is None


@pytest.mark.asyncio
async def test_websocket_connection_and_session_join_owned_cleanup(tmp_path):
    from backend.tests.test_ws_cold_connection import _connection, _release_session
    from backend.ws.manager import WebSocketManager, _dispose_unadopted_connection_resources

    unadopted = _connection(tmp_path / "unadopted")
    orphan_store = unadopted["artifact_store"]
    orphan_store.save("unadopted artifact", source="ws")
    await _dispose_unadopted_connection_resources(unadopted["llm"], orphan_store)
    assert orphan_store._cleanup_task.done() and orphan_store._cleanup_task.result() is None
    assert orphan_store.clear_calls == 1

    manager = WebSocketManager()
    connection = _connection(tmp_path / "adopted", session_id="artifact-lifecycle")
    store = connection["artifact_store"]
    session, _generation = await manager.connect(**connection)
    try:
        store.save("session artifact", source="ws")
        await _dispose_unadopted_connection_resources(
            session.llm, store, adopted_session=session,
        )
        assert not store._cleanup_stop.is_set() and not store._cleanup_task.done()
    finally:
        await _release_session(session)
    assert store._cleanup_task.done() and store._cleanup_task.result() is None
    assert (store.flush_calls, store.shutdown_calls, store.clear_calls) == (1, 1, 1)
