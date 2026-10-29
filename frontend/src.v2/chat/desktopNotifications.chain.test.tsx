/* @vitest-environment jsdom */
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { handleControlEvent } from "./controlEvents";
import { useDesktopEvents } from "../hooks/useDesktopEvents";
import type { ServerEvent } from "../protocol/events";

const mocks = vi.hoisted(() => ({ notify: vi.fn(), send: vi.fn((..._args: unknown[]) => true), hide: vi.fn(async () => 0), getState: vi.fn(async () => ({ isMaximized: false, isFocused: false })), removeFocus: vi.fn(), focus: null as null | ((state: { isMaximized: boolean; isFocused: boolean }) => void) }));
const readReceipts = vi.hoisted(() => ({ result: vi.fn<() => Promise<unknown>>() }));
vi.mock("../protocol/ws-outbox", async original => ({ ...await original<Record<string, unknown>>(), sendClientCommand: mocks.send,
  sendClientCommandAwaitResult: (command: unknown) => { mocks.send(command); return readReceipts.result(); } }));
vi.mock("../desktop/runtime", async original => ({ ...(await original<Record<string, unknown>>()),
  isDesktop: () => true, onEmbeddedBrowserEvent: () => () => {},
}));
vi.mock("../hooks/useDesktopTerminalMirror", () => ({ useDesktopTerminalMirror: () => {} }));

const request = (kind: "permission" | "question", conversationId = "conversation-A"): ServerEvent => ({
  type: "control_request", request_id: "request-1", conversation_id: conversationId,
  turn_id: "turn-1", message_id: "message-1", task_id: "task-1",
  request: kind === "permission" ? { subtype: "can_use_tool", tool_name: "read_file", input: { file_path: "actual.txt" } }
    : { subtype: "request_user_input", tool_use_id: "model-call", isBlocking: true,
      questions: [{ id: "selection", header: "Selection", question: "Actual question", isOther: false, isSecret: false, options: [{ label: "Keep", description: "Actual choice" }] }] },
});

beforeEach(() => {
  mocks.notify.mockReset(); mocks.hide.mockClear(); mocks.removeFocus.mockClear(); mocks.focus = null;
  mocks.send.mockClear();
  readReceipts.result.mockReset().mockResolvedValue({ type: "command.result", command: "conversation.read_state.set", level: "success" });
  mocks.getState.mockReset().mockResolvedValue({ isMaximized: false, isFocused: false });
  window.__MINICODE_RUNTIME__ = { desktop: { notify: mocks.notify, hideNotifications: mocks.hide,
    menu: { setState: vi.fn(async () => {}) },
    windowControls: { getState: mocks.getState, onStateChanged: (callback: typeof mocks.focus) => { mocks.focus = callback; return () => { mocks.focus = null; mocks.removeFocus(); }; } },
    onDeepLink: () => () => {},
  } } as never;
  useAppStore.setState({ conversationId: "conversation-A", pendingConversationSwitchId: null, settingsOpen: false,
    pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [],
    conversations: [], draft: "Unsent source draft", attachments: [], runtimeSession: null,
    isConnected: false,
  });
});
afterEach(() => { cleanup(); window.__MINICODE_RUNTIME__ = undefined; vi.restoreAllMocks(); });

describe("real control facts to desktop notification identity and visibility", () => {
  it.each(["permission", "question"] as const)("retains the %s control and sends its real owner plus actual visible conversation", kind => {
    render(<div className="chat-pane" />);
    handleControlEvent(request(kind));
    expect(mocks.notify).toHaveBeenCalledWith(expect.objectContaining({ kind, requestId: "request-1", turnId: "turn-1", messageId: "message-1", taskId: "task-1",
      target: { kind: "conversation", conversationId: "conversation-A" }, conversationVisible: true }));
    expect(kind === "permission" ? useAppStore.getState().pendingApproval?.requestId : useAppStore.getState().pendingAskUser?.requestId).toBe("request-1");
    expect(useAppStore.getState().draft).toBe("Unsent source draft");
  });

  it.each([
    { label: "Settings route", state: { settingsOpen: true } },
    { label: "pending switch retains old A", state: { pendingConversationSwitchId: "conversation-B" } },
    { label: "home has no active conversation", state: { conversationId: null } },
  ])("$label does not pretend the old conversation is visible", ({ state }) => {
    render(<div className="chat-pane" />);
    useAppStore.setState(state);
    handleControlEvent(request("question"));
    expect(mocks.notify).toHaveBeenCalledWith(expect.objectContaining({ conversationVisible: false, target: { kind: "conversation", conversationId: "conversation-A" } }));
  });

  it("a chat kept mounted behind the Code editor is not visible", () => {
    render(<div hidden><div className="chat-pane" /></div>);
    handleControlEvent(request("permission"));
    expect(mocks.notify).toHaveBeenCalledWith(expect.objectContaining({ conversationVisible: false }));
  });

  it("replay keeps the real prompt without reposting a system notification", () => {
    render(<div className="chat-pane" />);
    handleControlEvent({ ...request("question"), replayed: true } as ServerEvent);
    expect(mocks.notify).not.toHaveBeenCalled();
    expect(useAppStore.getState().pendingAskUser?.requestId).toBe("request-1");
  });

  it("authoritative cancellation hides the exact request and leaves another owner untouched", () => {
    handleControlEvent(request("question"));
    handleControlEvent({ ...request("question", "conversation-B"), request_id: "foreign-request" } as ServerEvent);
    mocks.hide.mockClear();
    handleControlEvent({ type: "approval.cancelled", conversation_id: "conversation-A", request_ids: ["request-1"], reason: "approval_timeout" } as ServerEvent);
    expect(mocks.hide).toHaveBeenCalledExactlyOnceWith({ conversationId: "conversation-A", requestId: "request-1" });
    expect(useAppStore.getState().pendingAskUser?.requestId).toBe("foreign-request");
  });
});

describe("native window lifecycle instead of renderer DOM focus", () => {
  const Harness = () => { useDesktopEvents(); return <div className="chat-pane" />; };
  it("only the actually visible native focused reader clears a newly completed unread turn", async () => {
    useAppStore.setState({ isConnected: true, conversations: [{ id: "conversation-A", title: "Actual task", updatedAt: "2026-10-14", hasUnreadTurn: true }] });
    render(<Harness />); await act(async () => {});
    expect(mocks.send).not.toHaveBeenCalled();
    act(() => mocks.focus!({ isMaximized: false, isFocused: true }));
    expect(mocks.send).toHaveBeenCalledExactlyOnceWith({ type: "conversation.read_state.set", conversation_id: "conversation-A", has_unread_turn: false });
    expect(useAppStore.getState().draft).toBe("Unsent source draft");
    await act(async () => {});
    mocks.send.mockClear();
    act(() => mocks.focus!({ isMaximized: false, isFocused: false }));
    act(() => useAppStore.setState({ conversations: [{ id: "conversation-A", title: "Actual next turn", updatedAt: "2026-10-14", hasUnreadTurn: true }] }));
    expect(mocks.send).not.toHaveBeenCalled();
    act(() => useAppStore.setState({ settingsOpen: true }));
    act(() => mocks.focus!({ isMaximized: false, isFocused: true }));
    expect(mocks.send).not.toHaveBeenCalled();
    act(() => useAppStore.setState({ settingsOpen: false }));
    expect(mocks.send).toHaveBeenCalledOnce();
  });

  it("coalesces repeated completion/focus projections while admitting a newer unread revision", async () => {
    let complete!: (value: unknown) => void;
    readReceipts.result.mockImplementation(() => new Promise(resolve => { complete = resolve; }));
    useAppStore.setState({ isConnected: true, conversations: [{ id: "conversation-A", title: "Task", updatedAt: "2026-10-29", revision: 10, hasUnreadTurn: true }] });
    render(<Harness />); await act(async () => {});
    act(() => mocks.focus!({ isMaximized: false, isFocused: true }));
    act(() => useAppStore.setState({ conversations: [...useAppStore.getState().conversations] }));
    act(() => mocks.focus!({ isMaximized: false, isFocused: true }));
    expect(mocks.send).toHaveBeenCalledOnce();
    expect(useAppStore.getState().conversations[0].hasUnreadTurn).toBe(true);
    await act(async () => { complete({}); });
    act(() => useAppStore.setState({ conversations: [{ ...useAppStore.getState().conversations[0], revision: 11, hasUnreadTurn: true }] }));
    expect(mocks.send).toHaveBeenCalledTimes(2);
    await act(async () => { complete({}); });
  });

  it("a native focused window does not read a conversation hidden behind Code", async () => {
    useAppStore.setState({ isConnected: true, conversations: [{ id: "conversation-A", title: "Actual task", updatedAt: "2026-10-14", hasUnreadTurn: true }] });
    render(<div hidden><Harness /></div>); await act(async () => {});
    act(() => mocks.focus!({ isMaximized: false, isFocused: true }));
    expect(mocks.send).not.toHaveBeenCalled();
  });
  it("actual native focus hides the visible conversation even when a child view owns DOM focus", async () => {
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    const view = render(<Harness />);
    await act(async () => {});
    expect(mocks.hide).not.toHaveBeenCalled();
    act(() => mocks.focus!({ isMaximized: false, isFocused: true }));
    expect(mocks.hide).toHaveBeenCalledExactlyOnceWith({ conversationId: "conversation-A", onlyIfFocused: true });
    view.unmount();
    expect(mocks.removeFocus).toHaveBeenCalledOnce();
    expect(mocks.focus).toBeNull();
  });

  it("native focus leaves notifications while Settings or a pending switch replaces the reader, then hides the actual destination", async () => {
    render(<Harness />); await act(async () => {});
    act(() => useAppStore.setState({ settingsOpen: true }));
    act(() => mocks.focus!({ isMaximized: false, isFocused: true }));
    expect(mocks.hide).not.toHaveBeenCalled();
    act(() => useAppStore.setState({ settingsOpen: false, pendingConversationSwitchId: "conversation-B" }));
    act(() => mocks.focus!({ isMaximized: false, isFocused: true }));
    expect(mocks.hide).not.toHaveBeenCalled();
    act(() => useAppStore.setState({ conversationId: "conversation-B", pendingConversationSwitchId: null }));
    expect(mocks.hide).toHaveBeenCalledExactlyOnceWith({ conversationId: "conversation-B", onlyIfFocused: true });
  });
});
