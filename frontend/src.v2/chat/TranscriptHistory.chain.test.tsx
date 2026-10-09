// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import type { ChatMessage, ContentBlock } from "../stores/types";
import { MessageList } from "./MessageList";
import { loadEarlierConversationMessages, loadEarlierToolItems } from "./historyPagination";
import { handleSessionEvent } from "./sessionEvents";
import { hydrateMessages } from "./transcriptHydration";
import type { ServerEvent } from "../protocol/events";
import type { StreamBuffer } from "../lib/stream-buffer";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), send: vi.fn(() => true), toast: vi.fn(), projections: [] as Array<{ owner?: string; root?: string; text?: string }> }));
vi.mock("../protocol/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../protocol/api")>(), fetchWithTimeout: mocks.fetch,
}));
vi.mock("../protocol/ws-outbox", async (importOriginal) => ({
  ...await importOriginal<typeof import("../protocol/ws-outbox")>(), sendClientCommand: mocks.send,
}));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "history-session" }) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("./components/ChatTurn", () => ({
  ChatTurn: ({ turn, conversationId, workspaceRoot }: {
    turn: { id: string; userCell?: { content: string }; finalAnswerCell?: { markdownSource: string } };
    conversationId?: string; workspaceRoot?: string;
  }) => {
    mocks.projections.push({ owner: conversationId, root: workspaceRoot, text: turn.userCell?.content });
    return <article data-turn={turn.id}>{turn.userCell?.content}{turn.finalAnswerCell?.markdownSource}</article>;
  },
}));

const initial = useAppStore.getState();
const pair = (id: string): ChatMessage[] => [
  { id: "user-" + id, role: "user", content: "question-" + id, artifacts: [], timestamp: 1 },
  { id: "assistant-" + id, role: "assistant", content: "answer-" + id, artifacts: [], timestamp: 2,
    terminalStatus: "completed", blocks: [{ type: "text", itemId: "answer-" + id, source: "model_final", status: "completed", content: "answer-" + id }] },
];
const backendPair = (id: string) => [
  { id: "user-" + id, role: "user", content: "question-" + id },
  { id: "assistant-" + id, role: "assistant", content: "answer-" + id, terminal_status: "completed",
    blocks: [{ type: "text", itemId: "answer-" + id, source: "model_final", status: "completed", content: "answer-" + id }] },
];
const pageResponse = (transcript = backendPair("earlier")) => new Response(JSON.stringify({
  transcript, transcript_page: { before_message_id: "user-earlier", has_more: false, total_messages: transcript.length + 2 },
}), { status: 200, headers: { "Content-Type": "application/json" } });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
const streamBuffer = (): StreamBuffer => ({ push: vi.fn(), flush: vi.fn(), destroy: vi.fn() });
const handleSession = (event: unknown) => handleSessionEvent(event as ServerEvent, {
  textStreamBuffer: streamBuffer(), thinkingStreamBuffer: streamBuffer(),
});
const backendPagedMessage = (revision: string) => ({
  ...backendPair("current")[1],
  tool_page: { before: 10, remaining: 1, total: 2, revision },
});
const toolPageResponse = () => new Response(JSON.stringify({
  message_id: "assistant-current", blocks: [tool("late-call", 0)],
  tool_page: { before: 0, remaining: 0, total: 2, revision: "old" },
}), { status: 200 });

beforeEach(() => {
  vi.clearAllMocks();
  mocks.projections.length = 0;
  useAppStore.setState({ ...initial, conversationId: "A", workingDirectory: "C:/A",
    conversations: [{ id: "A", title: "A", updatedAt: "" }, { id: "B", title: "B", updatedAt: "" }],
    messages: pair("current"), conversationMessages: {}, conversationStreaming: { A: false },
    conversationHistoryPages: { A: { beforeMessageId: "user-current", hasMore: true, loading: false } },
    sideChats: {}, isStreaming: false, turnDiffs: {} }, true);
  vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });


describe("history ownership, commit and reading position", () => {
  it("retains the reader's latest position when the requested history prepends", async () => {
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    render(<MessageList />);
    const scroll = screen.getByTestId("message-list-scroll");
    Object.defineProperties(scroll, {
      scrollHeight: { configurable: true, get: () => useAppStore.getState().messages.length * 200 },
      clientHeight: { configurable: true, value: 100 },
    });
    scroll.scrollTop = 70;
    fireEvent.click(screen.getByRole("button", { name: "加载更早的消息" }));
    scroll.scrollTop = 90;
    fireEvent.scroll(scroll);
    await act(async () => { pending.resolve(pageResponse()); });
    await waitFor(() => expect(document.querySelector('[data-turn="assistant-earlier"]')).not.toBeNull());
    expect(scroll.scrollTop).toBe(490);
    expect(scroll.scrollTop).not.toBe(scroll.scrollHeight);
    expect(mocks.fetch.mock.calls[0][0].searchParams.get("session_id")).toBe("history-session");
  });

  it("does not let A's pending history expand B's recent window", async () => {
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    render(<MessageList />);
    fireEvent.click(screen.getByRole("button", { name: "加载更早的消息" }));
    act(() => useAppStore.setState({ conversationId: "B", workingDirectory: "C:/B",
      messages: Array.from({ length: 45 }, (_, index) => pair("B-" + index)).flat(),
      conversationMessages: { A: pair("current") }, conversationStreaming: { A: false, B: false } }));
    await act(async () => { pending.resolve(pageResponse()); });
    expect(screen.getByText("显示更早的消息（5）")).toBeTruthy();
    expect(document.querySelector('[data-turn="assistant-B-0"]')).toBeNull();
    expect(useAppStore.getState().conversationMessages.A[0].id).toBe("user-earlier");
    expect(useAppStore.getState().messages[0].id).toBe("user-B-0");
  });

  it("never projects deferred A messages with B's owner or workspace", () => {
    render(<MessageList />);
    mocks.projections.length = 0;
    act(() => useAppStore.setState({ conversationId: "B", workingDirectory: "C:/B", messages: pair("B") }));
    expect(mocks.projections.some((projection) => projection.owner === "B" && projection.text === "question-current")).toBe(false);
    expect(mocks.projections.some((projection) => projection.owner === "B" && projection.root === "C:/B" && projection.text === "question-B")).toBe(true);
  });

  it("does not apply or invoke the list commit hook after a fresh snapshot replaces the cursor", async () => {
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    const beforePrepend = vi.fn();
    const request = loadEarlierConversationMessages("A", beforePrepend);
    useAppStore.setState({ messages: pair("fresh"), conversationHistoryPages: {
      A: { beforeMessageId: "user-fresh", hasMore: false, loading: false },
    } });
    pending.resolve(pageResponse());
    await request;
    expect(useAppStore.getState().messages[0].id).toBe("user-fresh");
    expect(beforePrepend).not.toHaveBeenCalled();
  });

  it("deduplicates loaded ids while retaining a newer live tail", async () => {
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    const request = loadEarlierConversationMessages("A");
    const tail = pair("tail");
    tail[1] = { ...tail[1], terminalStatus: undefined, isStreaming: true };
    const current = [...pair("current"), ...tail];
    useAppStore.setState({ messages: current, conversationStreaming: { A: true }, isStreaming: true });
    pending.resolve(pageResponse([...backendPair("earlier"), ...backendPair("current")]));
    await request;
    expect(useAppStore.getState().messages.map((message) => message.id)).toEqual([
      "user-earlier", "assistant-earlier", "user-current", "assistant-current", "user-tail", "assistant-tail",
    ]);
    expect(useAppStore.getState().messages.at(-1)).toBe(tail[1]);
    expect(useAppStore.getState().isStreaming).toBe(true);
  });

  it("refreshes the active conversation when its durable anchor no longer exists", async () => {
    mocks.fetch.mockResolvedValueOnce(new Response(JSON.stringify({ detail: "anchor removed" }), { status: 409 }));
    await loadEarlierConversationMessages("A");
    expect(mocks.send).toHaveBeenCalledWith({ type: "conversation.switch", conversation_id: "A" });
    expect(useAppStore.getState().conversationHistoryPages.A.loading).toBe(false);
  });
});

const tool = (id: string, transcriptIndex: number): ContentBlock => ({
  type: "tool_call", transcriptIndex,
  record: { id, name: "read_file", args: {}, status: "success", startedAt: 1 },
});
describe("tool item pagination", () => {
  it("merges indexed earlier calls and narration without changing the final answer", async () => {
    const message: ChatMessage = { ...pair("current")[1], blocks: [
      { type: "process", id: "narration", itemKind: "process_text", content: "working", transcriptIndex: 1 },
      tool("latest-call", 10),
      { type: "text", content: "answer-current", source: "model_final", itemId: "final", status: "completed", transcriptIndex: 11 },
    ], toolPage: { before: 10, remaining: 1, total: 2, revision: "original" } };
    useAppStore.setState({ messages: [pair("current")[0], message] });
    mocks.fetch.mockResolvedValueOnce(new Response(JSON.stringify({
      message_id: message.id, blocks: [tool("earlier-call", 0)],
      tool_page: { before: 0, remaining: 0, total: 2, revision: "original" },
    }), { status: 200 }));
    await loadEarlierToolItems("A", message.id);
    const updated = useAppStore.getState().messages[1];
    expect(updated.blocks?.map((block) => block.transcriptIndex)).toEqual([0, 1, 10, 11]);
    expect(updated.content).toBe("answer-current");
    expect(updated.toolPage?.remaining).toBe(0);
  });

  it.each(["old", "new"])("drops a late tool page after an authoritative snapshot with revision %s", async (revision) => {
    const message = hydrateMessages([backendPagedMessage("old")])[0];
    useAppStore.setState({ messages: [message] });
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    const request = loadEarlierToolItems("A", message.id);
    handleSession({ type: "conversation.switched", conversation_id: "A", context_pending: true,
      conversation: { id: "A", title: "A", transcript: [backendPagedMessage(revision)] },
    });
    const replacement = useAppStore.getState().messages[0];
    expect(replacement.toolPage).not.toBe(message.toolPage);
    expect(replacement.toolPage?.revision).toBe(revision);
    expect(mocks.fetch.mock.calls[0][0].searchParams.get("revision")).toBe("old");
    pending.resolve(toolPageResponse());
    await request;
    expect(useAppStore.getState().messages[0]).toBe(replacement);
  });

  it("keeps a late tool page with its cached conversation after switching to another owner", async () => {
    const message = hydrateMessages([backendPagedMessage("old")])[0];
    useAppStore.setState({ messages: [message] });
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    const request = loadEarlierToolItems("A", message.id);
    handleSession({ type: "conversation.switched", conversation_id: "B", context_pending: true,
      conversation: { id: "B", title: "B", transcript: backendPair("B") },
    });
    const activeMessages = useAppStore.getState().messages;
    pending.resolve(toolPageResponse());
    await request;
    expect(useAppStore.getState().conversationId).toBe("B");
    expect(useAppStore.getState().messages).toBe(activeMessages);
    expect(useAppStore.getState().conversationMessages.A[0].toolPage?.remaining).toBe(0);
  });

  it("does not restore deleted history after the inventory removes its conversation", async () => {
    const message = hydrateMessages([backendPagedMessage("old")])[0];
    useAppStore.setState({ messages: [message], conversationMessages: { A: [message] } });
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    const request = loadEarlierToolItems("A", message.id);
    handleSession({ type: "conversation.list", conversations: [{ id: "B", title: "B" }],
      active_conversation_id: "B", active_conversation: { id: "B", title: "B", transcript: backendPair("B") },
    });
    const activeMessages = useAppStore.getState().messages;
    pending.resolve(toolPageResponse());
    await request;
    expect(useAppStore.getState().conversationId).toBe("B");
    expect(useAppStore.getState().messages).toBe(activeMessages);
    expect(useAppStore.getState().conversationMessages.A).toBeUndefined();
    expect(useAppStore.getState().conversationHistoryPages.A).toBeUndefined();
    expect(mocks.toast).not.toHaveBeenCalled();
  });

  it("does not reload a fresh snapshot for a retired tool request's conflict", async () => {
    const message = hydrateMessages([backendPagedMessage("old")])[0];
    useAppStore.setState({ messages: [message] });
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    const request = loadEarlierToolItems("A", message.id);
    handleSession({ type: "conversation.switched", conversation_id: "A", context_pending: true,
      conversation: { id: "A", title: "A", transcript: [backendPagedMessage("new")] },
    });
    const replacement = useAppStore.getState().messages[0];
    pending.resolve(new Response(JSON.stringify({ detail: "The message changed" }), { status: 409 }));
    await request;
    expect(useAppStore.getState().messages[0]).toBe(replacement);
    expect(mocks.send).not.toHaveBeenCalledWith({ type: "conversation.switch", conversation_id: "A" });
    expect(mocks.toast).not.toHaveBeenCalled();
  });
});
