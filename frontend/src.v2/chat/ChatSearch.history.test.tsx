// @vitest-environment jsdom
import { createRef } from "react";
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatSearch } from "./ChatSearch";
import { MessageList } from "./MessageList";
import { useAppStore } from "../stores";
import type { ChatMessage } from "../stores/types";
import { ExecCell } from "./cells/ExecCell";
import { TranscriptSearchContext } from "./TranscriptSearchContext";
import * as historyPagination from "./historyPagination";

// Keep windowing/projection/search real; isolate the presentational renderer.
vi.mock("./components/ChatTurn", () => ({ ChatTurn: ({ turn }: { turn: {
  id: string; userCell?: { content: string }; finalAnswerCell?: { markdownSource: string };
  activeCell?: { partialMarkdown: string };
} }) => <article data-turn={turn.id}>{turn.userCell?.content}{turn.finalAnswerCell?.markdownSource}{turn.activeCell?.partialMarkdown}</article> }));

const initial = useAppStore.getState();
const pair = (index: number): ChatMessage[] => [
  { id: `user-${index}`, role: "user", content: `question ${index}`, artifacts: [], timestamp: index + 1 },
  { id: `assistant-${index}`, role: "assistant", content: index === 0 ? "historic-exact-needle" : `answer ${index}`,
    artifacts: [], timestamp: index + 1, terminalStatus: "completed", isStreaming: false,
    blocks: [{ type: "text", itemId: `answer-${index}`, source: "model_final", status: "completed", isStreaming: false, content: index === 0 ? "historic-exact-needle" : `answer ${index}` }] },
];
beforeEach(() => {
  useAppStore.setState({ ...initial, messages: Array.from({ length: 60 }, (_, index) => pair(index)).flat(),
    conversationId: "A", conversations: [{ id: "A", title: "search owner", updatedAt: "2026-09-30" }],
    isStreaming: false, conversationHistoryPages: {}, turnDiffs: {}, workingDirectory: "" }, true);
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function surface(searchActive: boolean) {
  const containerRef = createRef<HTMLDivElement>();
  return <><ChatSearch containerRef={containerRef} onClose={() => {}} />
    <main ref={containerRef}><MessageList searchActive={searchActive} /></main></>;
}

describe("loaded transcript search", () => {
  it("finds a folded command output during search and restores its disclosure preference afterward", async () => {
    const containerRef = createRef<HTMLDivElement>();
    const cell = { kind: "exec" as const, id: "folded-command", command: "build", status: "success" as const, stdoutPreview: ["hidden-error-needle"], stderrPreview: [], collapsed: true, createdAt: 1 };
    const content = (active: boolean) => <><ChatSearch containerRef={containerRef} onClose={() => {}} /><main ref={containerRef}><TranscriptSearchContext.Provider value={active}><ExecCell cell={cell} /></TranscriptSearchContext.Provider></main></>;
    const view = render(content(false));
    expect(view.queryByText("hidden-error-needle")).toBeNull();
    view.rerender(content(true));
    fireEvent.change(view.getByLabelText("搜索对话内容"), { target: { value: "hidden-error-needle" } });
    await waitFor(() => expect(view.getByText("1/1")).toBeTruthy());
    expect(window.getSelection()?.toString()).toBe("hidden-error-needle");
    view.rerender(content(false));
    expect(view.queryByText("hidden-error-needle")).toBeNull();
  });

  it("states the loaded-history boundary and offers the owner-scoped earlier page", () => {
    const load = vi.spyOn(historyPagination, "loadEarlierConversationMessages").mockResolvedValue(undefined);
    useAppStore.setState({ conversationHistoryPages: { A: { beforeMessageId: "old", hasMore: true, loading: false } } });
    const view = render(surface(true));
    expect(view.getByText(/还有更早历史未检索/)).toBeTruthy();
    fireEvent.click(view.getByRole("button", { name: "载入更早历史继续搜索" }));
    expect(load).toHaveBeenCalledWith("A");
  });
  it("mounts all loaded turns during search and returns to windowing when closed", async () => {
    const view = render(surface(true));
    expect(document.querySelector('[data-turn="assistant-0"]')).not.toBeNull();
    expect(document.querySelector('[data-testid="virtual-turn-list"]')).toBeNull();
    fireEvent.change(view.getByLabelText("搜索对话内容"), { target: { value: "historic-exact-needle" } });
    await waitFor(() => expect(view.getByText("1/1")).toBeTruthy());
    expect(window.getSelection()?.toString()).toBe("historic-exact-needle");
    view.rerender(surface(false));
    expect(document.querySelector('[data-turn="assistant-0"]')).toBeNull();
    expect(view.getByText("显示更早的消息（20）")).toBeTruthy();
  });

  it("recomputes the same query after actual streamed DOM text changes", async () => {
    const view = render(surface(true));
    fireEvent.change(view.getByLabelText("搜索对话内容"), { target: { value: "stream-exact-needle" } });
    expect(view.getByText("无匹配项")).toBeTruthy();
    act(() => {
      const messages = useAppStore.getState().messages.slice();
      messages[messages.length - 1] = { ...messages.at(-1)!, content: "stream-exact-needle",
        terminalStatus: undefined, isStreaming: true,
        blocks: [{ type: "text", itemId: "stream-tail", isStreaming: true, content: "stream-exact-needle" }] };
      useAppStore.setState({ messages, isStreaming: true });
    });
    await waitFor(() => expect(view.getByText("1/1")).toBeTruthy());
    expect(window.getSelection()?.toString()).toBe("stream-exact-needle");
  });

  it("searches only its transcript ref, not composer or context text", () => {
    const containerRef = createRef<HTMLDivElement>();
    const view = render(<><ChatSearch containerRef={containerRef} onClose={() => {}} />
      <aside>not-a-transcript-needle</aside><main ref={containerRef}><p>actual transcript</p></main></>);
    fireEvent.change(view.getByLabelText("搜索对话内容"), { target: { value: "not-a-transcript-needle" } });
    expect(view.getByText("无匹配项")).toBeTruthy();
  });

  it("uses original Unicode offsets and clears selection when the query is emptied", () => {
    const containerRef = createRef<HTMLDivElement>();
    const view = render(<><ChatSearch containerRef={containerRef} onClose={() => {}} />
      <main ref={containerRef}><p>İx [literal]</p></main></>);
    const input = view.getByLabelText("搜索对话内容");
    fireEvent.change(input, { target: { value: "x [literal]" } });
    expect(view.getByText("1/1")).toBeTruthy();
    expect(window.getSelection()?.toString()).toBe("x [literal]");
    fireEvent.change(input, { target: { value: "" } });
    expect(window.getSelection()?.rangeCount).toBe(0);
    expect((view.getByRole("button", { name: "下一个匹配项" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("matches inline text without joining unrelated paragraphs", () => {
    const containerRef = createRef<HTMLDivElement>();
    const view = render(<><ChatSearch containerRef={containerRef} onClose={() => {}} />
      <main ref={containerRef}><p>join<strong>ed</strong></p><p>cat</p><p>alog</p></main></>);
    const input = view.getByLabelText("搜索对话内容");
    fireEvent.change(input, { target: { value: "joined" } });
    expect(window.getSelection()?.toString()).toBe("joined");
    fireEvent.change(input, { target: { value: "catalog" } });
    expect(view.getByText("无匹配项")).toBeTruthy();
  });

  it("retains the selected match without scrolling again during a streamed DOM update", async () => {
    const containerRef = createRef<HTMLDivElement>();
    const view = render(<><ChatSearch containerRef={containerRef} onClose={() => {}} />
      <main ref={containerRef}><p>needle first</p><p>needle second</p><p data-tail>stream tail</p></main></>);
    fireEvent.change(view.getByLabelText("搜索对话内容"), { target: { value: "needle" } });
    fireEvent.click(view.getByRole("button", { name: "下一个匹配项" }));
    expect(view.getByText("2/2")).toBeTruthy();
    const selectedNode = window.getSelection()?.getRangeAt(0).startContainer;
    const scroll = vi.mocked(HTMLElement.prototype.scrollIntoView);
    scroll.mockClear();
    await act(async () => { view.container.querySelector("[data-tail]")!.textContent = "stream tail grew"; });
    await waitFor(() => expect(view.getByText("2/2")).toBeTruthy());
    expect(window.getSelection()?.getRangeAt(0).startContainer).toBe(selectedNode);
    expect(scroll).not.toHaveBeenCalled();
  });
});
