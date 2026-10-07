import { beforeEach, expect, it, vi } from "vitest";
import type { ServerEvent, StreamResumeEvent } from "../protocol/events";
import { useAppStore } from "../stores";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { handleChatStreamEvent, resetStreamResumePages } from "./chatStreamEvents";

vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
let counter = 0;
let owner: string;
const handlers = { textStreamBuffer: { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() }, thinkingStreamBuffer: { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() } };
const tool = (index: number) => ({ id: `tool-${index}`, name: "read_file", args: { path: `${index}.ts` }, status: "running", started_at: 1, turn_id: "turn" });
const page = (part: number, complete: boolean, tools = [tool(part)]): StreamResumeEvent => ({ type: "stream_resume", conversation_id: owner,
  message_id: "reply", turn_id: "turn", event_seq: 100, stream_status: "running", snapshot_id: "snapshot", snapshot_part: part, snapshot_complete: complete,
  tool_calls_pending: [], tool_states: tools, content_blocks: tools.map((record) => ({ type: "tool_call", record })) });
const receive = (event: Record<string, unknown> | StreamResumeEvent) => handleChatStreamEvent(event as ServerEvent, owner, handlers);

beforeEach(() => {
  resetStreamResumePages(); owner = `paged-resume-${++counter}`;
  useAppStore.setState({ conversationId: owner, pendingConversationSwitchId: null, isStreaming: true, draft: "new draft",
    conversationStreaming: { [owner]: true }, conversationMessages: {}, sideChats: {},
    messages: [{ id: "reply", role: "assistant", content: "", timestamp: 1, isStreaming: true, turnId: "turn", blocks: [] }] });
});

it("atomically restores all history beyond one frame's tool and block limits", () => {
  for (let part = 0; part < 4; part++) {
    const event = page(part, part === 3, Array.from({ length: 450 }, (_, index) => tool(part * 450 + index)));
    expect(normalizeInboundServerEvent(event)).toBeTruthy();
    expect(receive(event)).toBe(true);
    expect(getToolCallsFromMessage(useAppStore.getState().messages[0]).length).toBe(part === 3 ? 1800 : 0);
  }
  expect(useAppStore.getState().draft).toBe("new draft");
});

it("applies the frozen snapshot once, then replays only later source events from the same turn", () => {
  receive(page(0, false));
  receive({ type: "tool_result", conversation_id: owner, message_id: "reply", turn_id: "turn", source_event_seq: 99,
    id: "tool-0", status: "failed", summary: "Already covered" });
  receive({ type: "tool_result", conversation_id: owner, message_id: "reply", turn_id: "turn", source_event_seq: 101,
    id: "tool-0", status: "success", summary: "New live output" });
  expect(useAppStore.getState().messages[0].blocks).toEqual([]);
  receive(page(1, true));
  const record = getToolCallsFromMessage(useAppStore.getState().messages[0])[0];
  expect(record.status).toBe("success");
  expect(record.summary).toBe("New live output");
});

it("delivers a terminal event after the complete multi-frame snapshot without reviving the turn", () => {
  receive(page(0, false));
  receive({ type: "done", conversation_id: owner, message_id: "reply", turn_id: "turn", source_event_seq: 101, status: "completed", usage: {} });
  expect(useAppStore.getState().isStreaming).toBe(true);
  receive(page(1, true));
  expect(useAppStore.getState().isStreaming).toBe(false);
  expect(useAppStore.getState().messages[0].terminalStatus).toBe("completed");
});

it("requires contiguous pages and discards pending pages at the connection generation boundary", () => {
  receive(page(0, false));
  expect(receive(page(2, true))).toBe(false);
  resetStreamResumePages();
  expect(receive(page(1, true))).toBe(false);
  expect(useAppStore.getState().messages[0].blocks).toEqual([]);
});

it("preserves the legacy single-frame resume contract", () => {
  const { snapshot_id, snapshot_part, snapshot_complete, ...legacy } = page(0, true);
  expect(receive(legacy)).toBe(true);
  expect(getToolCallsFromMessage(useAppStore.getState().messages[0])).toHaveLength(1);
});
