/* @vitest-environment jsdom */
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { handleChatStreamEvent } from "./chatStreamEvents";
import { handleRuntimeEvent } from "./runtimeEvents";
import { handleNoticeEvent } from "./noticeEvents";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { normalizeContentBlocks } from "./transcriptHydration";
import { ContextCompactionCell } from "./cells/ContextCompactionCell";
import { ChatTurn } from "./components/ChatTurn";
import { LanguageProvider } from "../i18n";
import type { ServerEvent } from "../protocol/events";
import type { ContextCompactionCellState } from "./cells/cellTypes";

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true) }));
const initial = useAppStore.getState();
const time = Date.parse("2026-10-13T10:00:00Z");
const buffers = { textStreamBuffer: { push() {}, flush() {}, destroy() {} }, thinkingStreamBuffer: { push() {}, flush() {}, destroy() {} } };
const assistant = () => ({ id: "assistant", role: "assistant" as const, turnId: "turn", content: "", blocks: [], timestamp: time, isStreaming: true });
const event = (completed = false, owner = "owner", id = "compact-1") => ({ type: completed ? "item.completed" : "item.started",
  conversation_id: owner, message_id: "assistant", turn_id: "turn", iteration_id: "iteration", timestamp: new Date(time).toISOString(),
  item: { id, type: "context_compaction", source: "auto", status: completed ? "completed" : "in_progress", started_at: time,
    ...(completed ? { finished_at: time + 200 } : {}) },
});
const accept = (payload: Record<string, unknown>) => {
  const wire = normalizeInboundServerEvent(payload); expect(wire).not.toBeNull();
  expect(handleChatStreamEvent(wire!, String(payload.conversation_id), buffers)).toBe(true);
};
const cells = () => projectMessagesToTurns(useAppStore.getState().messages, true)[0]?.committedCells || [];
beforeEach(() => { useAppStore.setState({ ...initial, appMode: "cowork", conversationId: "owner", messages: [assistant()],
  conversationMessages: {}, conversationStreaming: { owner: true }, isStreaming: true, draft: "UNSENT_DRAFT", contextUsage: { used: 900, limit: 1000 }, inspectorEntries: [] }, true); });
afterEach(() => { cleanup(); vi.useRealTimers(); useAppStore.setState(initial, true); });

describe("Source ContextCompaction item to live/history UI", () => {
  it("accepts the canonical lifecycle, settles one standalone item and keeps budget metadata out of duplicate notices", () => {
    accept(event());
    expect(cells()).toEqual([expect.objectContaining({ kind: "context_compaction", status: "running", createdAt: time })]);
    accept(event(true));
    accept(event(true));
    expect(cells()).toEqual([expect.objectContaining({ kind: "context_compaction", status: "completed", createdAt: time, completedAt: time + 200 })]);
    expect(handleRuntimeEvent({ type: "context_compacted", conversation_id: "owner", compaction_item_id: "compact-1",
      summary: "", before_tokens: 900, after_tokens: 100 } as unknown as ServerEvent, "owner")).toBe(true);
    expect(useAppStore.getState().contextUsage?.used).toBe(100);
    expect(useAppStore.getState().messages.some(message => message.role === "system")).toBe(false);
    expect(useAppStore.getState().draft).toBe("UNSENT_DRAFT");
    const { container } = render(<ContextCompactionCell cell={cells()[0] as ContextCompactionCellState} />);
    expect(screen.getByText("已优化对话")).toBeTruthy();
    expect(container.querySelectorAll("svg path")).toHaveLength(4);
    expect(container.textContent).not.toMatch(/tokens|摘要|new_context|工具调用/);
  });

  it("uses Source ten-second delay text and stops scanning when the real completed item arrives", () => {
    vi.useFakeTimers(); vi.setSystemTime(time); accept(event());
    const { rerender, container } = render(<ContextCompactionCell cell={cells()[0] as ContextCompactionCellState} />);
    expect(screen.getByText("正在优化对话")).toBeTruthy();
    act(() => vi.advanceTimersByTime(9999)); expect(screen.queryByText(/可能需要几分钟/)).toBeNull();
    act(() => vi.advanceTimersByTime(1)); expect(screen.getByText("正在优化对话 · 可能需要几分钟")).toBeTruthy();
    accept(event(true)); rerender(<ContextCompactionCell cell={cells()[0] as ContextCompactionCellState} />);
    expect(screen.getByText("已优化对话")).toBeTruthy();
    expect(container.querySelector('[data-processing="true"]')).toBeNull();
  });

  it("rehydrates empty-content process items and original begin/end timestamps", () => {
    const blocks = normalizeContentBlocks([{ type: "process", id: "compact-1", itemKind: "context_compaction", content: "",
      status: "completed", source: "manual", timestamp: time, startedAt: time, finishedAt: time + 200 }]);
    useAppStore.setState({ messages: [{ ...assistant(), blocks, isStreaming: false }] });
    const item = cells()[0] as ContextCompactionCellState;
    expect(item).toMatchObject({ kind: "context_compaction", status: "completed", source: "manual", createdAt: time, completedAt: time + 200 });
    render(<LanguageProvider locale="en-US"><ContextCompactionCell cell={item} /></LanguageProvider>);
    expect(screen.getByText("Context compacted")).toBeTruthy();
  });

  it("retains internal model tool receipts while projecting only their source-visible lifecycle", () => {
    useAppStore.setState({ messages: [{ ...assistant(), blocks: [{ type: "tool_call", record: { id: "internal", name: "get_context_remaining",
      args: {}, status: "success", startedAt: time, visibility: "debug", summary: "You have 123 tokens left in this context window." } }] }] });
    accept(event()); accept(event(true));
    expect(cells()).toHaveLength(1); expect(cells()[0].kind).toBe("context_compaction");
    expect(useAppStore.getState().messages[0].blocks?.[0]).toMatchObject({ type: "tool_call", record: { summary: "You have 123 tokens left in this context window." } });
  });

  it("routes a late background compaction to its owner without changing foreground draft or usage", () => {
    useAppStore.setState({ conversationMessages: { background: [assistant()] }, conversationStreaming: { owner: true, background: true } });
    accept(event(false, "background")); accept(event(true, "background"));
    expect(cells()).toHaveLength(0);
    const background = projectMessagesToTurns(useAppStore.getState().conversationMessages.background, true)[0].committedCells;
    expect(background[0]).toMatchObject({ kind: "context_compaction", status: "completed" });
    handleRuntimeEvent({ type: "context_compacted", conversation_id: "background", compaction_item_id: "compact-1", summary: "", after_tokens: 1 } as unknown as ServerEvent, "background");
    expect(useAppStore.getState().contextUsage?.used).toBe(900);
    expect(useAppStore.getState().draft).toBe("UNSENT_DRAFT");
  });

  it("projects a real standalone manual CompactTask without borrowing an assistant message", () => {
    const standalone = (completed = false) => { const payload = event(completed); delete (payload as Partial<typeof payload>).message_id; payload.item.source = "manual"; return payload; };
    accept(standalone());
    expect(useAppStore.getState().messages.filter(message => message.role === "assistant")).toHaveLength(1);
    const task = useAppStore.getState().messages.find(message => message.id === "compact-1")!;
    expect(task.role).toBe("system");
    expect(projectMessagesToTurns([task], false)[0].committedCells[0]).toMatchObject({ kind: "context_compaction", status: "running", source: "manual" });
    accept(standalone(true));
    const settled = useAppStore.getState().messages.find(message => message.id === "compact-1")!;
    expect(projectMessagesToTurns([settled], false)[0].committedCells[0]).toMatchObject({ kind: "context_compaction", status: "completed" });
    expect(useAppStore.getState().messages).toHaveLength(2);
    const { container } = render(<ChatTurn turn={projectMessagesToTurns([settled], false)[0]} conversationId="owner" />);
    expect(screen.getByText("上下文已压缩")).toBeTruthy();
    expect(container.textContent).not.toMatch(/已处理|运行中|工具调用/);
    expect(container.querySelector(".user-cell-bubble")).toBeNull();
    const malformed = { ...event(), item: { id: "ordinary", type: "agent_message", text: "", status: "in_progress" } };
    delete (malformed as Partial<typeof malformed>).message_id;
    expect(normalizeInboundServerEvent(malformed)).toBeNull();
  });

  it("accepts the actual no-summary window status and budget wire instead of closing the connection", () => {
    accept(event());
    const status = normalizeInboundServerEvent({ type: "conversation.compaction.updated", conversation_id: "owner",
      state: "compacted", summary: "", compaction_item_id: "compact-1" });
    const budget = normalizeInboundServerEvent({ type: "context_compacted", conversation_id: "owner", summary: "",
      compaction_item_id: "compact-1", before_tokens: 900, after_tokens: 100 });
    expect(status).not.toBeNull(); expect(budget).not.toBeNull();
    expect(handleNoticeEvent(status!, "owner")).toBe(true);
    expect(handleRuntimeEvent(budget!, "owner")).toBe(true);
    accept(event(true));
    expect(cells()).toHaveLength(1); expect(cells()[0]).toMatchObject({ kind: "context_compaction", status: "completed" });
    expect(useAppStore.getState().contextUsage?.used).toBe(100);
    expect(useAppStore.getState().messages.some(message => message.role === "system")).toBe(false);
    expect(normalizeInboundServerEvent({ type: "context_compacted", conversation_id: "owner", summary: "" })).toBeNull();
    expect(normalizeInboundServerEvent({ type: "conversation.compaction.updated", conversation_id: "owner", state: "failed", summary: "", compaction_item_id: "compact-1" })).toBeNull();
  });
});
