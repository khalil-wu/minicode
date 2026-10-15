/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SubagentsTab } from "./SubagentsTab";
import { useAppStore } from "../../stores";
import { handleRuntimeEvent } from "../../chat/runtimeEvents";
import { hydrateMessages, type BackendTranscriptMessage } from "../../chat/transcriptHydration";
import { releaseConversationReadingState } from "../../chat/transcriptReadingState";
import { normalizeInboundServerEvent } from "../../protocol/server-event-validation";

const { sendClientCommandAwaitResult } = vi.hoisted(() => ({ sendClientCommandAwaitResult: vi.fn() }));
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({
  matches: false, addEventListener() {}, removeEventListener() {},
}) }));
vi.mock("../../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(() => true), sendClientCommandAwaitResult,
  commandResultSucceeded: (result: { level?: string }) => result.level !== "error",
}));
vi.mock("../../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

const owner = "child-process-disclosure";
const childId = "actual-child";
const processBlock = (id: string, content: string) => ({ type: "process", id,
  item_kind: "process_text", content, source: "model_preamble", status: "completed", visibility: "timeline", timestamp: 1000 });
const finalBlock = (id: string, content: string) => ({ type: "text", item_id: id,
  content, source: "model_final", status: "completed", is_streaming: false });
const runningMessages: BackendTranscriptMessage[] = [{ id: "subagent-turn-first", turn_id: "first", role: "assistant",
  content: "", timestamp: 1000, is_streaming: true, blocks: [processBlock("first-process", "检查第一轮文件。")],
}];
const completedMessages: BackendTranscriptMessage[] = [{ ...runningMessages[0], content: "第一轮完成。", is_streaming: false,
  terminal_status: "completed", completed_at: 5000, blocks: [processBlock("first-process", "检查第一轮文件。"), finalBlock("first-answer", "第一轮完成。")],
}];

function publish(event: Record<string, unknown>) {
  const normalized = normalizeInboundServerEvent({ conversation_id: owner, subagent_id: childId, ...event });
  expect(normalized).toBeTruthy();
  expect(handleRuntimeEvent(normalized!)).toBe(true);
}
function prepare(messages = runningMessages, status: "running" | "done" | "failed" = "running") {
  sendClientCommandAwaitResult.mockResolvedValue({ type: "command.result", command: "subagent.transcript",
    level: "success", message: "", data: { seq: 1, messages },
  });
  useAppStore.setState({ focusedSubagentId: childId, subagents: [{ id: childId, role: "reviewer", displayName: "Ada",
    status, transcriptSeq: 1, transcriptMessages: hydrateMessages(messages),
  }] });
}

beforeEach(() => {
  vi.clearAllMocks();
  releaseConversationReadingState(owner);
  useAppStore.setState({ conversationId: owner, workingDirectory: "C:\\workspace", isConnected: true,
    messages: [], conversationMessages: {}, conversationStreaming: {},
    subagents: [], focusedSubagentId: null, pendingAskUser: null, askUserQueue: [], promptDrafts: {}, viewMode: "normal",
    draft: "用户主聊天草稿", attachments: [],
  });
});
afterEach(cleanup);

describe("real child transcript process disclosure", () => {
  it("folds completed work on the first child detail visit and preserves a manual reading choice on return", async () => {
    prepare(completedMessages, "done");
    render(<SubagentsTab />);
    expect(await screen.findByText("第一轮完成。")).toBeTruthy();
    expect(screen.queryByText("检查第一轮文件。")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "展开处理步骤" }));
    expect(screen.getByText("检查第一轮文件。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "返回子智能体列表" }));
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体任务：Ada" }));
    expect(await screen.findByText("检查第一轮文件。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "收起处理步骤" }));
    expect(screen.queryByText("检查第一轮文件。")).toBeNull();
    expect(screen.getByText("第一轮完成。")).toBeTruthy();
  });

  it("allows running work to stay folded as real events arrive, then preserves that choice across final and terminal delivery", async () => {
    prepare();
    render(<SubagentsTab />);
    expect(await screen.findByText("检查第一轮文件。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "收起处理步骤" }));
    await act(async () => publish({ type: "subagent.progress", transcript_delta: { kind: "events", base_seq: 1, seq: 2,
      events: [{ event_id: "live-reading", event_type: "system", seq: 2, ts_ms: 2000, payload: {
        transcript_only: true, kind: "process_text", item_id: "second-process", content: "已核对新增文件。", source: "model_preamble", status: "completed", visibility: "timeline",
      } }],
    } }));
    expect(screen.queryByText("已核对新增文件。")).toBeNull();
    expect(useAppStore.getState().subagents[0].transcriptMessages?.[0].blocks).toHaveLength(2);
    await act(async () => publish({ type: "subagent.progress", transcript_delta: { kind: "events", base_seq: 2, seq: 3,
      events: [{ event_id: "final-item", event_type: "progress", seq: 3, ts_ms: 3000, payload: {
        kind: "assistant_message", item_id: "first-answer", content: "第一轮完成。", source: "model_final", status: "completed",
      } }],
    } }));
    expect(screen.getByText("第一轮完成。")).toBeTruthy();
    expect(screen.queryByText("检查第一轮文件。")).toBeNull();
    await act(async () => publish({ type: "subagent.done", result: { status: "completed", content: "第一轮完成。" },
      transcript_snapshot: { seq: 4, messages: completedMessages },
    }));
    expect(screen.getByRole("button", { name: "展开处理步骤" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "展开处理步骤" }));
    expect(screen.getByText("检查第一轮文件。")).toBeTruthy();
    expect(useAppStore.getState().draft).toBe("用户主聊天草稿");
  });

  it("automatically folds untouched running work when the final item arrives before the child stops", async () => {
    prepare();
    render(<SubagentsTab />);
    expect(await screen.findByText("检查第一轮文件。")).toBeTruthy();
    await act(async () => publish({ type: "subagent.progress", transcript_delta: { kind: "events", base_seq: 1, seq: 2,
      events: [{ event_id: "final-item", event_type: "progress", seq: 2, ts_ms: 3000, payload: {
        kind: "assistant_message", item_id: "first-answer", content: "第一轮完成。", source: "model_final", status: "completed",
      } }],
    } }));
    expect(useAppStore.getState().subagents[0].status).toBe("running");
    expect(screen.getByText("第一轮完成。")).toBeTruthy();
    expect(screen.queryByText("检查第一轮文件。")).toBeNull();
    expect(screen.getByRole("button", { name: "展开处理步骤" })).toBeTruthy();
  });

  it("keeps completed child rounds folded independently of the current live round and restores those boundaries", async () => {
    const messages = [...completedMessages, { id: "subagent-turn-second", turn_id: "second", role: "assistant",
      content: "", timestamp: 6000, is_streaming: true, blocks: [processBlock("second-process", "检查第二轮文件。")],
    }];
    prepare(messages);
    const live = render(<SubagentsTab />);
    expect(await screen.findByText("检查第二轮文件。")).toBeTruthy();
    expect(screen.queryByText("检查第一轮文件。")).toBeNull();
    const rounds = live.container.querySelectorAll(".chat-turn");
    expect(rounds).toHaveLength(2);
    fireEvent.click(within(rounds[1] as HTMLElement).getByRole("button", { name: "收起处理步骤" }));
    expect(screen.queryByText("检查第二轮文件。")).toBeNull();
    const finished = [...completedMessages, { ...messages[1], content: "第二轮完成。", is_streaming: false,
      terminal_status: "completed", completed_at: 9000, blocks: [processBlock("second-process", "检查第二轮文件。"), finalBlock("second-answer", "第二轮完成。")],
    }];
    await act(async () => publish({ type: "subagent.done", result: { status: "completed", content: "第二轮完成。" },
      transcript_snapshot: { seq: 5, messages: finished },
    }));
    await waitFor(() => expect(screen.getByText("第二轮完成。")).toBeTruthy());
    live.unmount();
    releaseConversationReadingState(owner);
    prepare(finished, "done");
    render(<SubagentsTab />);
    expect(await screen.findByText("第二轮完成。")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "展开处理步骤" })).toHaveLength(2);
    expect(screen.queryByText("检查第一轮文件。")).toBeNull();
    expect(screen.queryByText("检查第二轮文件。")).toBeNull();
  });

  it.each(["failed", "partial", "interrupted"] as const)("starts %s evidence open and keeps it available after a manual fold", async (terminalStatus) => {
    prepare([{ ...runningMessages[0], content: "", is_streaming: false, terminal_status: terminalStatus,
      completed_at: 5000, ...(terminalStatus === "failed" ? { failure_message: "真实文件读取失败" } : {}),
    }], "failed");
    render(<SubagentsTab />);
    expect(await screen.findByText("检查第一轮文件。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "收起处理步骤" }));
    expect(screen.queryByText("检查第一轮文件。")).toBeNull();
    if (terminalStatus === "failed") expect(screen.getByText("真实文件读取失败")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "展开处理步骤" }));
    expect(screen.getByText("检查第一轮文件。")).toBeTruthy();
    expect(useAppStore.getState().draft).toBe("用户主聊天草稿");
  });
});
