/* @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useAppStore } from "../stores";
import type { ChatMessage, ContentBlock } from "../stores/types";
import { createRecentTurnProjectionCache, projectMessagesToTurns, projectRecentMessagesToTurns } from "./chatSurfaceState";
import { AgentTimeline } from "../agent-loop/components/AgentTimeline";
import { loadEarlierConversationMessages } from "./historyPagination";

vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "audit-session" }) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

const message = (id: string, role: ChatMessage["role"], blocks: ContentBlock[] = []): ChatMessage => ({ id, role, content: "", blocks, artifacts: [], timestamp: 1 });
const assistant = (source: string = "model_final"): ChatMessage => ({ ...message("assistant", "assistant", [
  ...Array.from({ length: 1000 }, (_, index): ContentBlock => ({ type: "tool_call", record: { id: `t${index}`, name: "read_file", args: { file_path: `file${index}.py` }, status: "success", startedAt: index } })),
  { type: "text", itemId: "answer", content: "Started", source, status: "in_progress", isStreaming: true },
]), isStreaming: true });

beforeEach(() => useAppStore.setState({ conversationId: "conv", conversations: [], conversationMessages: {}, conversationStreaming: {}, conversationHistoryPages: {}, messages: [], isStreaming: true }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it.each(["model_final", "pending"])("keeps completed tools stable during %s streaming and settles the exact final answer", (source) => {
  const initial = assistant(source);
  useAppStore.setState({ messages: [initial] });
  const before = projectMessagesToTurns([initial], true)[0];
  const firstBlock = initial.blocks![0] as Extract<ContentBlock, { type: "tool_call" }>;
  const args = firstBlock.record.args;
  const access = vi.fn(() => args);
  Object.defineProperty(firstBlock.record, "args", { get: access });
  useAppStore.getState().appendAgentMessageDelta("answer", " continued", "conv", "assistant");
  const after = projectMessagesToTurns(useAppStore.getState().messages, true)[0];
  expect(access).not.toHaveBeenCalled();
  expect(after.committedCells[0]).toBe(before.committedCells[0]);
  if (source === "model_final") {
    expect(after.committedCells).toBe(before.committedCells);
    expect(after.activeCell?.partialMarkdown).toBe("Started continued");
  } else {
    expect(after.committedCells.at(-1)).toMatchObject({ kind: "thinking", content: "Started continued" });
  }
  useAppStore.getState().completeAgentMessage({ id: "answer", text: "Final authoritative answer", source: "model_final", status: "completed" }, "conv", undefined, "assistant");
  const settled = projectMessagesToTurns(useAppStore.getState().messages, true)[0];
  expect(settled.finalAnswerCell?.markdownSource).toBe("Final authoritative answer");
  expect(settled.activeCell).toBeNull();
});

it("invalidates recent topology for an arbitrary older hidden-state change", () => {
  const messages = Array.from({ length: 100 }, (_, index) => [message(`u${index}`, "user"), message(`a${index}`, "assistant")]).flat();
  const cache = createRecentTurnProjectionCache();
  expect(projectRecentMessagesToTurns(messages, true, 40, cache).totalTurnCount).toBe(100);
  const changed = messages.slice();
  changed[4] = { ...changed[4], queueState: "queued" };
  changed[5] = { ...changed[5], queueState: "queued" };
  expect(projectRecentMessagesToTurns(changed, true, 40, cache).totalTurnCount).toBe(99);
});

it("windows a large live process group and keeps an older failure visible", () => {
  const cells = projectMessagesToTurns([assistant()], true)[0].committedCells;
  cells[0] = { ...cells[0], status: "failed" } as typeof cells[number];
  render(<AgentTimeline cells={cells} isRunning renderCell={({ key, cell }) => <div key={key} data-testid="process-cell">{cell.id}</div>} />);
  expect(screen.getAllByTestId("process-cell")).toHaveLength(41);
  fireEvent.click(screen.getByRole("button", { name: /显示更早的操作/ }));
  expect(screen.getAllByTestId("process-cell")).toHaveLength(81);
});

it("prepends a page without replacing a newer streaming tail", async () => {
  let resolve!: (value: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((done) => { resolve = done; })));
  useAppStore.setState({ messages: [assistant()], conversationHistoryPages: { conv: { beforeMessageId: "assistant", hasMore: true, loading: false } }, conversationStreaming: { conv: true } });
  const loading = loadEarlierConversationMessages("conv");
  useAppStore.getState().appendAgentMessageDelta("answer", " new", "conv", "assistant");
  resolve(new Response(JSON.stringify({ transcript: [{ id: "older", role: "user", content: "Older", timestamp: 1 }], transcript_page: { before_message_id: "older", has_more: false, total_messages: 2 } }), { status: 200 }));
  await loading;
  expect(useAppStore.getState().messages.map((item) => item.id)).toEqual(["older", "assistant"]);
  expect(useAppStore.getState().messages.at(-1)?.blocks?.at(-1)).toMatchObject({ content: "Started new" });
});

it("does not apply an older page after an authoritative reload changes its cursor", async () => {
  let resolve!: (value: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((done) => { resolve = done; })));
  useAppStore.setState({ messages: [assistant()], conversationHistoryPages: { conv: { beforeMessageId: "assistant", hasMore: true, loading: false } } });
  const loading = loadEarlierConversationMessages("conv");
  useAppStore.setState({ conversationHistoryPages: { conv: { beforeMessageId: "rewound", hasMore: false, loading: false } }, messages: [message("rewound", "user")] });
  resolve(new Response(JSON.stringify({ transcript: [{ id: "obsolete", role: "user", content: "Old" }], transcript_page: { before_message_id: "obsolete", has_more: false, total_messages: 2 } }), { status: 200 }));
  await loading;
  expect(useAppStore.getState().messages.map((item) => item.id)).toEqual(["rewound"]);
});
