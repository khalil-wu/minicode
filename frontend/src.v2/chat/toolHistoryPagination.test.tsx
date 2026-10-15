/* @vitest-environment jsdom */
import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useAppStore } from "../stores";
import { loadEarlierToolItems } from "./historyPagination";
import { hydrateMessages } from "./transcriptHydration";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { AgentTimeline } from "../agent-loop/components/AgentTimeline";
import { projectChatTurnToAgentLoop } from "../agent-loop/projection/project-turn";
import * as outbox from "../protocol/ws-outbox";
import { ChatTurn } from "./components/ChatTurn";
import { releaseConversationReadingState } from "./transcriptReadingState";
import { pushToast } from "../overlays/ToastContainer";

vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "session" }) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const tool = (index: number) => ({ type: "tool_call", transcriptIndex: index,
  record: { id: `t${index}`, name: "read_file", args: { file_path: `f${index}.py` }, status: "success", startedAt: index } });
const answer = { type: "text", itemId: "final", source: "model_final", status: "completed", content: "Final answer", transcriptIndex: 5 };

it("keeps a history disclosure when all currently loaded steps are hidden", () => {
  const message = hydrateMessages([{ id: "old", role: "assistant", terminal_status: "completed", blocks: [answer],
    tool_page: { before: 4, remaining: 4, total: 5 } }])[0];
  const turn = projectMessagesToTurns([message], false)[0];
  expect(turn.committedCells).toHaveLength(0);
  expect(projectChatTurnToAgentLoop(turn).hasProcessContent).toBe(true);
});

it("merges indexed tool pages in order and retains a newer streaming tail", async () => {
  const historical = hydrateMessages([{ id: "old", role: "assistant", content: "Final answer", terminal_status: "completed",
    blocks: [tool(4), answer], tool_page: { before: 4, remaining: 4, total: 5 } }])[0];
  useAppStore.setState({ conversationId: "conv", messages: [historical, { id: "live", role: "assistant", content: "", timestamp: 1,
    artifacts: [], isStreaming: true, blocks: [{ type: "text", itemId: "stream", content: "Live", source: "model_final", isStreaming: true }] }],
    conversationMessages: {}, conversationStreaming: { conv: true }, conversationHistoryPages: {}, isStreaming: true });
  let resolve!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((done) => { resolve = done; })));
  const loading = loadEarlierToolItems("conv", "old");
  useAppStore.getState().appendAgentMessageDelta("stream", " more", "conv", "live");
  const live = useAppStore.getState().messages[1];
  resolve(new Response(JSON.stringify({ message_id: "old", blocks: [tool(2), tool(3)], tool_page: { before: 2, remaining: 2, total: 5 } })));
  await loading;
  const messages = useAppStore.getState().messages;
  expect(messages[0].blocks!.map((block) => block.transcriptIndex)).toEqual([2, 3, 4, 5]);
  expect(messages[1]).toBe(live);
  expect(projectMessagesToTurns([messages[0]], false)[0].finalAnswerCell?.markdownSource).toBe("Final answer");
});

it("drops an older page after an authoritative message replacement", async () => {
  const original = hydrateMessages([{ id: "old", role: "assistant", terminal_status: "completed", blocks: [tool(4), answer],
    tool_page: { before: 4, remaining: 4, total: 5 } }])[0];
  useAppStore.setState({ conversationId: "conv", messages: [original], conversationMessages: {}, conversationStreaming: {} });
  let resolve!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((done) => { resolve = done; })));
  const loading = loadEarlierToolItems("conv", "old");
  const replacement = { ...original, blocks: [], toolPage: { ...original.toolPage! } };
  useAppStore.setState({ messages: [replacement] });
  resolve(new Response(JSON.stringify({ message_id: "old", blocks: [tool(1)], tool_page: { before: 1, remaining: 1, total: 5 } })));
  await loading;
  expect(useAppStore.getState().messages[0]).toBe(replacement);
});

it("merges the requested snapshot with same-message deltas and keeps every unindexed live block", async () => {
  const original = hydrateMessages([{ id: "old", role: "assistant", is_streaming: true, blocks: [tool(4),
    { ...answer, status: "in_progress", is_streaming: true }], tool_page: { before: 4, remaining: 4, total: 5 } }], { live: true })[0];
  useAppStore.setState({ conversationId: "conv", messages: [original], isStreaming: true, conversationMessages: {}, conversationStreaming: { conv: true } });
  let resolve!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((done) => { resolve = done; })));
  const loading = loadEarlierToolItems("conv", "old");
  const state = useAppStore.getState();
  state.appendAgentMessageDelta("final", " more", "conv", "old");
  state.startAgentMessage("new-commentary", "conv", "old", "commentary");
  state.appendAgentMessageDelta("new-commentary", "正在继续", "conv", "old", "commentary");
  state.appendToolCallBlock({ id: "new-tool", name: "read_file", args: { file_path: "next.py" }, status: "running", startedAt: 10 }, "conv", "old");
  const liveBlocks = useAppStore.getState().messages[0].blocks!;
  const appended = liveBlocks.filter((block) => block.transcriptIndex === undefined);
  expect(appended).toHaveLength(2);
  resolve(new Response(JSON.stringify({ message_id: "old", blocks: [tool(2), tool(3)], tool_page: { before: 2, remaining: 2, total: 5 } })));
  await loading;
  const blocks = useAppStore.getState().messages[0].blocks!;
  expect(blocks.map((block) => block.transcriptIndex)).toEqual([2, 3, 4, 5, undefined, undefined]);
  expect(blocks.slice(-2)).toEqual(appended);
  expect(blocks[3]).toMatchObject({ content: "Final answer more" });
});

it("reloads the active conversation when its tool cursor is obsolete", async () => {
  const original = hydrateMessages([{ id: "old", role: "assistant", terminal_status: "completed", blocks: [tool(4), answer],
    tool_page: { before: 4, remaining: 4, total: 5, revision: "old" } }])[0];
  useAppStore.setState({ conversationId: "conv", messages: [original], conversationMessages: {}, conversationStreaming: {} });
  const send = vi.spyOn(outbox, "sendClientCommand").mockImplementation(() => true);
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "The message changed" }), { status: 409 })));
  await loadEarlierToolItems("conv", "old");
  expect(send).toHaveBeenCalledWith({ type: "conversation.switch", conversation_id: "conv" });
  expect(useAppStore.getState().messages[0]).toBe(original);
});

it("shows the newly prepended work without resetting its disclosure", () => {
  const project = (start: number) => projectMessagesToTurns(hydrateMessages([{ id: "old", role: "assistant", terminal_status: "completed",
    blocks: Array.from({ length: 80 - start }, (_, index) => tool(index + start)) }]), false)[0].committedCells;
  const view = render(<AgentTimeline cells={project(40)} expandWorkGroups renderCell={({ key, cell }) => <div key={key} data-testid="cell">{cell.id}</div>} />);
  expect(screen.getAllByTestId("cell")).toHaveLength(40);
  const retained = screen.getByText("t60");
  view.rerender(<AgentTimeline cells={project(0)} expandWorkGroups renderCell={({ key, cell }) => <div key={key} data-testid="cell">{cell.id}</div>} />);
  expect(screen.getAllByTestId("cell")).toHaveLength(80);
  expect(screen.getByText("t60")).toBe(retained);
});

const HistoricalTurn = () => {
  const messages = useAppStore((state) => state.messages);
  return <ChatTurn turn={projectMessagesToTurns(messages, false)[0]} conversationId="conv" />;
};
const pagingResponse = (start: number, remaining: number) => new Response(JSON.stringify({ message_id: "old",
  blocks: [tool(start), tool(start + 1)], tool_page: { before: start, remaining, total: 6, revision: "snapshot" },
}));
const prepareHistory = (toolsLoaded = true) => {
  releaseConversationReadingState("conv");
  const historical = hydrateMessages([{ id: "old", role: "assistant", terminal_status: "completed", content: "Final answer",
    blocks: [...(toolsLoaded ? [tool(4), tool(5)] : []), { ...answer, transcriptIndex: 6 }],
    tool_page: { before: 4, remaining: 4, total: 6, revision: "snapshot" },
  }])[0];
  useAppStore.setState({ conversationId: "conv", messages: [historical], conversationMessages: {}, conversationStreaming: {},
    conversationHistoryPages: {}, isStreaming: false, pendingConversationSwitchId: null, draft: "保留的用户草稿", viewMode: "normal",
  });
};

it("loads a real tool page on process disclosure, then loads older pages by scrolling without extra controls", async () => {
  prepareHistory();
  let completeOlder!: (response: Response) => void;
  const fetch = vi.fn().mockResolvedValueOnce(pagingResponse(2, 2))
    .mockImplementationOnce(() => new Promise<Response>((resolve) => { completeOlder = resolve; }));
  vi.stubGlobal("fetch", fetch);
  const view = render(<HistoricalTurn />);
  expect(fetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "展开处理步骤" }));
  await waitFor(() => expect(useAppStore.getState().messages[0].toolPage?.before).toBe(2));
  expect(new URL(String(fetch.mock.calls[0][0])).searchParams.get("before")).toBe("4");
  expect(screen.queryByRole("button", { name: /显示更早|加载更多|加载更早/ })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "已读取文件" }));
  expect(screen.getByText("f2.py")).toBeTruthy();
  expect(screen.getByText("f5.py")).toBeTruthy();
  const list = view.container.querySelector<HTMLElement>(".agent-loop-timeline-group-items")!;
  fireEvent.wheel(list, { deltaY: -100 });
  fireEvent.wheel(list, { deltaY: -100 });
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(new URL(String(fetch.mock.calls[1][0])).searchParams.get("before")).toBe("2");
  await act(async () => completeOlder(pagingResponse(0, 0)));
  expect(screen.getByText("f0.py")).toBeTruthy();
  expect(screen.getByText("f5.py")).toBeTruthy();
  expect(view.container.querySelectorAll(".activity-cell")).toHaveLength(6);
  fireEvent.wheel(list, { deltaY: -100 });
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(screen.getByText("Final answer")).toBeTruthy();
  expect(useAppStore.getState().draft).toBe("保留的用户草稿");
});

it("can disclose real unloaded history when the initial page has no visible operations", async () => {
  prepareHistory(false);
  const fetch = vi.fn().mockResolvedValue(pagingResponse(2, 2));
  vi.stubGlobal("fetch", fetch);
  render(<HistoricalTurn />);
  fireEvent.click(screen.getByRole("button", { name: "展开处理步骤" }));
  expect(await screen.findByRole("button", { name: "已读取文件" })).toBeTruthy();
  expect(fetch).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole("button", { name: "已读取文件" }));
  expect(screen.getByText("f2.py")).toBeTruthy();
  expect(screen.queryByRole("button", { name: /显示更早|加载更多|加载更早/ })).toBeNull();
});

it("retains the actual page error and cursor, then lets an explicit upward scroll retry it", async () => {
  prepareHistory();
  vi.mocked(pushToast).mockClear();
  const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ detail: "实际历史读取 HTTP 503" }), { status: 503 }))
    .mockResolvedValueOnce(pagingResponse(2, 2));
  vi.stubGlobal("fetch", fetch);
  const view = render(<HistoricalTurn />);
  fireEvent.click(screen.getByRole("button", { name: "展开处理步骤" }));
  await waitFor(() => expect(pushToast).toHaveBeenCalledWith("实际历史读取 HTTP 503", "error"));
  expect(useAppStore.getState().messages[0].toolPage?.before).toBe(4);
  expect(fetch).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole("button", { name: "已读取文件" }));
  expect(screen.getByText("f4.py")).toBeTruthy();
  fireEvent.wheel(view.container.querySelector(".agent-loop-timeline-group-items")!, { deltaY: -100 });
  expect(await screen.findByText("f2.py")).toBeTruthy();
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(useAppStore.getState().messages[0].toolPage?.before).toBe(2);
  expect(useAppStore.getState().draft).toBe("保留的用户草稿");
});
