from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.message import UserCommand
from backend.agent.state import AgentState
from backend.attachments.store import AttachmentStore
from backend.llm.base import LLMMessage, ToolCallEvent
from backend.llm.capabilities import ProviderCapabilities
from backend.llm.openai_adapter import _openai_chat_messages
from backend.ws.command_dispatcher import SessionCommandDispatcher


@pytest.mark.parametrize("command_type", ["llm.model.set", "llm.config.set"])
def test_model_selection_finishes_before_following_image_message(command_type):
    async def scenario():
        entered, release, sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
        lock = asyncio.Lock()
        models = []
        session = SimpleNamespace(
            selected_model="text-only", connection_generation=1, ws_manager=None,
            conversation_lifecycle_lock=lambda: lock,
            event_outbox=SimpleNamespace(bind_connection_generation=lambda _: nullcontext()),
            send_event=AsyncMock(),
        )
        dispatcher = object.__new__(SessionCommandDispatcher)
        dispatcher._session = session
        async def handle(command):
            if command.type == command_type:
                entered.set()
                await release.wait()
                session.selected_model = "vision"
            else:
                models.append(session.selected_model)
                sent.set()
        dispatcher._handle_command_inner = handle
        selection = asyncio.create_task(dispatcher._handle_command(UserCommand(type=command_type, data={"model":"vision"})))
        await entered.wait()
        message = asyncio.create_task(dispatcher._handle_command(UserCommand(type="user_message", data={"content":"Read this image"})))
        await asyncio.sleep(0)
        sent_early = sent.is_set()
        release.set()
        await asyncio.gather(selection, message)
        assert not sent_early
        assert models == ["vision"]
    asyncio.run(scenario())


@pytest.mark.parametrize("reload", [False, True])
def test_image_survives_model_switches_without_stale_capability_hints(tmp_path, reload):
    async def scenario():
        root = tmp_path / "project"
        root.mkdir()
        owner = "image-owner"
        store = AttachmentStore(tmp_path / "attachments")
        attachment = {"artifact_id":"image-1", "kind":"image", "file_name":"image.png", "media_type":"image/png"}
        store.save(artifact_id="image-1", content="Original image", native_data="UElYRUxT", metadata={
            "conversation_id":owner, "workspace_root":str(root), "attachment":attachment,
        })
        text_model = SimpleNamespace(capabilities=ProviderCapabilities(provider="test", model="text-only", vision=False))
        builder = ContextBuilder(llm=text_model, conversation_id=owner, workspace_root=root)
        builder._attachment_store = store
        state = AgentState(user_message="Read this image", conversation_id=owner, workspace_root=root)
        state.attachments = [attachment]
        first = await builder.build(state.user_message, state)
        assert first[-1].images == []
        assert "does not support native image input" in first[-1].content
        assert builder._history[-1].images
        assert "does not support native image input" not in builder._history[-1].content
        if reload:
            snapshot = builder.export_snapshot()
            builder = ContextBuilder(llm=text_model, conversation_id=owner, workspace_root=root)
            builder._attachment_store = store
            builder.load_snapshot(snapshot)
        for vision in [True, False, True]:
            builder.bind_llm(SimpleNamespace(capabilities=ProviderCapabilities(provider="test", model=str(vision), vision=vision)))
            result = await builder.build(state)
            image_turn = next(message for message in result if message.attachment_refs)
            assert bool(image_turn.images) is vision
            assert ("does not support native image input" in image_turn.content) is not vision
            assert builder._history[-1].images[0]["data"] == "UElYRUxT"
    asyncio.run(scenario())


def test_chat_tool_images_follow_the_complete_parallel_tool_result_batch():
    pixels = {"media_type":"image/png", "data":"UElYRUxT"}
    history = [
        LLMMessage(role="assistant", tool_calls=[ToolCallEvent(id="read-1", name="read_file", arguments={}), ToolCallEvent(id="read-2", name="read_file", arguments={})]),
        LLMMessage(role="tool", tool_call_id="read-1", name="read_file", content="Image loaded", images=[pixels]),
        LLMMessage(role="tool", tool_call_id="read-2", name="read_file", content="Text loaded"),
    ]
    for trailing in [[], [LLMMessage(role="assistant", content="I see it.")]]:
        wire = _openai_chat_messages(history + trailing)
        assert [item["role"] for item in wire[:4]] == ["assistant", "tool", "tool", "user"]
        assert wire[3]["content"][1] == {"type":"image_url", "image_url":{"url":"data:image/png;base64,UElYRUxT"}}
        assert history[1].images == [pixels]


@pytest.mark.parametrize("reload", [False, True])
def test_mixed_pdf_image_text_and_code_keep_originals_across_model_switches(tmp_path, reload):
    async def scenario():
        root = tmp_path / "project"
        root.mkdir()
        owner = "mixed-owner"
        store = AttachmentStore(tmp_path / "attachments")
        entries = [
            ("image", "image.png", "image/png", "pixels", "UElYRUxT"),
            ("document", "paper.pdf", "application/pdf", "PDF_BODY_UNIQUE", "JVBERi0xLjQ="),
            ("document", "notes.txt", "text/plain", "NOTES_BODY_UNIQUE", "Tk9URVM="),
            ("code", "main.py", "text/x-python", "print('CODE_BODY_UNIQUE')", "Q09ERQ=="),
        ]
        refs = []
        for index, (kind, name, media_type, text, original) in enumerate(entries):
            ref = {"artifact_id":f"file-{index}", "file_name":name, "media_type":media_type, "kind":kind}
            refs.append(ref)
            store.save(artifact_id=ref["artifact_id"], content=text, native_data=original, metadata={
                "conversation_id":owner, "workspace_root":str(root), "attachment":ref,
            })
        builder = ContextBuilder(conversation_id=owner, workspace_root=root)
        builder._attachment_store = store
        builder.bind_llm(SimpleNamespace(capabilities=ProviderCapabilities(provider="test", wire_api="chat", vision=False, native_pdf=False)))
        state = AgentState(user_message="Compare all files", conversation_id=owner, workspace_root=root)
        state.attachments = refs
        await builder.start_turn(state.user_message, state)
        if reload:
            snapshot = builder.export_snapshot()
            builder = ContextBuilder(conversation_id=owner, workspace_root=root)
            builder._attachment_store = store
            builder.load_snapshot(snapshot)
        for native in [False, True, False, True]:
            builder.bind_llm(SimpleNamespace(capabilities=ProviderCapabilities(provider="test", wire_api="anthropic" if native else "chat", vision=native, native_pdf=native)))
            result = await builder.build(state)
            turn = next(message for message in result if message.attachment_refs)
            assert len(turn.images) == int(native)
            assert len(turn.documents) == int(native)
            assert turn.content.count("NOTES_BODY_UNIQUE") == 1
            assert turn.content.count("CODE_BODY_UNIQUE") == 1
            assert turn.content.count("PDF_BODY_UNIQUE") == (0 if native else 1)
            for ref, entry in zip(refs, entries):
                assert store.get_native_data(ref["artifact_id"], conversation_id=owner, workspace_root=str(root)) == entry[-1]
                assert store.get_native_data(ref["artifact_id"], conversation_id="another-owner", workspace_root=str(root)) is None
    asyncio.run(scenario())
