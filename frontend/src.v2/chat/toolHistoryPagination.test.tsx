/* @vitest-environment jsdom */
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { useAppStore } from "../stores";
import { loadEarlierToolItems } from "./historyPagination";
import { hydrateMessages } from "./transcriptHydration";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { AgentTimeline } from "../agent-loop/components/AgentTimeline";
import { projectChatTurnToAgentLoop } from "../agent-loop/projection/project-turn";
import * as outbox from "../protocol/ws-outbox";

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
  const replacement = { ...original, blocks: [] };
  useAppStore.setState({ messages: [replacement] });
  resolve(new Response(JSON.stringify({ message_id: "old", blocks: [tool(1)], tool_page: { before: 1, remaining: 1, total: 5 } })));
  await loading;
  expect(useAppStore.getState().messages[0]).toBe(replacement);
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
