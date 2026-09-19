from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.attachments.store import AttachmentStore
from backend.config import TokenBudget
from backend.llm.base import LLMMessage
from backend.llm.capabilities import ProviderCapabilities
from backend.skills.executor import SkillExecutor
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager


def _adapter(*, pdf=False):
    return SimpleNamespace(capabilities=ProviderCapabilities(vision=True, native_pdf=pdf, wire_api="responses" if pdf else "chat"))


def _attachment(store, workspace, *, kind, content=""):
    ref = {"artifact_id": f"audit-{kind}", "file_name": f"audit.{kind}", "kind": kind,
           "media_type": "image/png" if kind == "image" else "application/pdf", "size_bytes": 5}
    store.save(artifact_id=ref["artifact_id"], content=content, native_data="aGVsbG8=",
               metadata={"conversation_id": "audit", "workspace_root": str(workspace), "attachment": ref})
    return ref


def test_rejected_media_is_only_withheld_from_current_request_and_survives_restore(tmp_path):
    store = AttachmentStore(tmp_path / "attachments")
    refs = [_attachment(store, tmp_path, kind=kind) for kind in ["image", "document"]]
    llm = _adapter(pdf=True)
    builder = ContextBuilder(llm=llm, conversation_id="audit", workspace_root=tmp_path)
    builder._attachment_store = store
    builder._history_store.append(LLMMessage(role="user", content="inspect both", attachment_refs=refs))
    state = AgentState(user_message="inspect both", conversation_id="audit", workspace_root=tmp_path)
    asyncio.run(builder.build(state))
    assert builder.strip_historical_media(keep_recent_user_turns=0) == {"messages": 1, "images": 1, "documents": 1}
    projected = asyncio.run(builder.build(state))
    assert not any(m.images or m.documents for m in projected)
    assert any("read_artifact('audit-image')" in m.content and "read_artifact('audit-document')" in m.content for m in projected)
    assert builder._history[0].attachment_refs == refs
    assert len(builder._history[0].images) == len(builder._history[0].documents) == 1
    assert "media-size recovery" not in builder._history[0].content
    assert builder.strip_historical_media()["messages"] == 0

    restored = ContextBuilder(llm=_adapter(pdf=True), conversation_id="audit", workspace_root=tmp_path)
    restored._attachment_store = store
    restored.load_snapshot(builder.export_snapshot())
    messages = asyncio.run(restored.build(state))
    assert sum(len(m.images) for m in messages) == 1
    assert sum(len(m.documents) for m in messages) == 1
    builder.bind_llm(_adapter(pdf=True))
    assert any(m.images and m.documents for m in asyncio.run(builder.build(state)))


def test_pdf_budget_uses_sent_text_and_large_pdf_is_available_for_paged_read(tmp_path):
    store = AttachmentStore(tmp_path / "attachments")
    builder = ContextBuilder(llm=_adapter(), token_budget=TokenBudget(total=32768, response_reserve=4096),
                             conversation_id="audit", workspace_root=tmp_path)
    builder._attachment_store = store
    state = AgentState(user_message="inspect PDF", conversation_id="audit", workspace_root=tmp_path)
    text = "PDFCONTENT\n" * 16000
    ref = _attachment(store, tmp_path, kind="document", content=text)
    builder._history_store.append(LLMMessage(role="user", content="inspect PDF", attachment_refs=[ref]))
    messages = asyncio.run(builder.build(state))
    budget = builder.get_budget_snapshot(state, messages=messages)
    assert budget["used"] == sum(builder._estimate_history_message(m) for m in messages)
    assert budget["used"] < 32768 - 4096
    assert any("read_artifact('audit-document', offset=1, limit=100)" in m.content for m in messages)
    assert store.get("audit-document", conversation_id="audit", workspace_root=str(tmp_path)) == text
    assert builder._history[0].attachment_refs == [ref]

    # A smaller PDF is inlined, and its actual text is included in admission.
    _attachment(store, tmp_path, kind="document", content="PDFCONTENT\n" * 1000)
    messages = asyncio.run(builder.build(state))
    assert any("PDFCONTENT\\nPDFCONTENT" in m.content for m in messages)
    assert builder.get_budget_snapshot(state)["used"] == sum(builder._estimate_history_message(m) for m in messages)


def test_final_extension_input_is_counted_by_the_same_budget_contract():
    builder = ContextBuilder(token_budget=TokenBudget(total=32768, response_reserve=4096))
    state = AgentState(user_message="hello")
    builder.append_user("hello")
    ordinary = asyncio.run(builder.build(state))
    assert not builder.needs_compaction(state, messages=ordinary)
    transformed = [*ordinary, LLMMessage(role="developer", content="hook output " * 16000)]
    assert builder.needs_compaction(state, messages=transformed)


def test_project_skill_catalog_uses_owned_snapshot_instead_of_global_executor(tmp_path, monkeypatch):
    roots = [tmp_path / "a", tmp_path / "b"]
    for name, root in zip(["audit-a", "audit-b"], roots):
        path = root / ".minicode" / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\ndescription: {name} scoped skill\n---\n{name} instructions\n", encoding="utf-8")
    monkeypatch.setattr(SkillLoader, "_search_dirs", lambda self: [("workspace", self._project_root / ".minicode" / "skills")] if self._project_root else [])
    global_manager = SkillManager(SkillLoader())
    global_manager.discover()
    session_manager = SkillManager(SkillLoader(roots[0]))
    session_manager.discover()
    running = ContextBuilder(skill_executor=SkillExecutor(global_manager), skill_manager=session_manager, workspace_root=roots[0])
    before = running._build_skill_catalog()
    assert "audit-a" in before
    session_manager.set_project_root(roots[1])
    assert running._build_skill_catalog() == before
    assert [m.name for m in running.skill_manager.list_metas()] == ["audit-a"]
    next_run = ContextBuilder(skill_manager=session_manager, workspace_root=roots[1])
    assert "audit-b" in next_run._build_skill_catalog()
