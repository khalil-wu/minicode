import { beforeEach, describe, expect, it } from "vitest";
import { addInspectorPayload, focusInspectorEntry } from "./inspectorEntries";
import { useAppStore } from "../stores";
import { registerWebSocketSender } from "../protocol/ws-outbox";

describe("inspector entries", () => {
  beforeEach(() => {
    useAppStore.setState({
      rightStackTab: "preview",
      rightPanelOpen: false,
      rightStackTabLocked: false,
      inspectorEntries: [],
      inspectorFocus: null,
      conversationId: "conversation-1",
      conversations: [{
        id: "conversation-1",
        title: "测试会话",
        updatedAt: "2026-08-07T00:00:00Z",
        workspaceRoot: "C:\\workspace",
      }],
      workingDirectory: "C:\\workspace",
    });
    registerWebSocketSender(null);
  });

  it("records inspector payloads without opening or switching a panel", () => {
    addInspectorPayload("provider", "trace-1", { diagnostics_deferred: true });

    expect(useAppStore.getState()).toMatchObject({
      rightStackTab: "preview",
      rightPanelOpen: false,
    });
    expect(useAppStore.getState().inspectorEntries).toHaveLength(1);
  });

  it("requests deferred inspector diagnostics only when focused", () => {
    const commands: unknown[] = [];
    registerWebSocketSender((command) => {
      commands.push(command);
      return true;
    });
    const entry = {
      targetKind: "provider" as const,
      targetId: "trace-1",
      payload: { diagnostics_deferred: true },
      timestamp: 1,
    };

    focusInspectorEntry(entry);

    expect(useAppStore.getState().inspectorFocus).toEqual({ kind: "provider", id: "trace-1" });
    expect(commands).toEqual([{
      type: "inspector.focus",
      target_kind: "provider",
      target_id: "trace-1",
      conversation_id: "conversation-1",
      workspace_root: "C:\\workspace",
    }]);
  });

  it("upserts loaded diagnostics instead of retaining the compact duplicate", () => {
    const state = useAppStore.getState();
    state.addInspectorEntry({
      targetKind: "provider",
      targetId: "trace-1",
      payload: { diagnostics_deferred: true },
      timestamp: 1,
    });
    state.addInspectorEntry({
      targetKind: "provider",
      targetId: "trace-1",
      payload: { diagnostics_loaded: true, provider_timeline: [{ event: "done" }] },
      timestamp: 2,
    });

    expect(useAppStore.getState().inspectorEntries).toEqual([{
      targetKind: "provider",
      targetId: "trace-1",
      payload: { diagnostics_loaded: true, provider_timeline: [{ event: "done" }] },
      timestamp: 2,
    }]);
  });

  // Regression: answer text with no live assistant to attach it to used to be
  // discarded by `return null` with no trace anywhere, so a whole final answer
  // could vanish from the transcript unexplained.
  it("traces answer text dropped because no assistant message is streaming", () => {
    useAppStore.setState({ messages: [], isStreaming: false });
    const state = useAppStore.getState();

    state.appendAgentMessageDelta("item-1", "半句", "conversation-1", "assistant-gone");
    state.appendAgentMessageDelta("item-1", "答复", "conversation-1", "assistant-gone");
    state.completeAgentMessage(
      { id: "item-1", text: "完整的最终答复", status: "completed" },
      "conversation-1",
      undefined,
      "assistant-gone",
    );

    const entries = useAppStore.getState().inspectorEntries;
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({
      targetKind: "message",
      targetId: "dropped-answer:conversation-1:item-1",
      payload: {
        dropped: true,
        reason: "agent_message_completed",
        conversation_id: "conversation-1",
        message_id: "assistant-gone",
        dropped_events: 3,
        text_preview: "完整的最终答复",
      },
    });
    // Two deltas (2 + 2 chars) plus the 7-char final answer.
    expect(entries[0].payload.dropped_characters).toBe(11);
  });

  it("does not trace an empty delta, which loses nothing", () => {
    useAppStore.setState({ messages: [], isStreaming: false, inspectorEntries: [] });
    useAppStore.getState().appendAgentMessageDelta("item-2", "", "conversation-1", "assistant-gone");

    expect(useAppStore.getState().inspectorEntries).toHaveLength(0);
  });
});
