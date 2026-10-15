/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage, ContentBlock } from "../stores/types";
import { useAppStore } from "../stores";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { ChatTurn } from "./components/ChatTurn";
import { ExecCell } from "./cells/ExecCell";
import { UserMessageCell } from "./cells/UserMessageCell";
import { releaseConversationReadingState } from "./transcriptReadingState";

const { openArtifactPreview } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  return { openArtifactPreview: vi.fn() };
});
vi.mock("./openAttachmentPreview", () => ({ openArtifactPreview, openAttachmentPreview: vi.fn(), openWorkspaceFilePreview: vi.fn(), openLocalFilePreview: vi.fn() }));

const assistant = (blocks: ContentBlock[], patch: Partial<ChatMessage> = {}): ChatMessage => ({ id: "assistant", role: "assistant", content: "", timestamp: 100_000, artifacts: [], blocks, ...patch });
beforeEach(() => {
  releaseConversationReadingState("owner");
  openArtifactPreview.mockClear();
  useAppStore.setState({ conversationId: "active-other", workingDirectory: "C:/other", draft: "原草稿" });
});
afterEach(cleanup);

describe("Codex item, output and reading projection", () => {
  it("retains a verified child startup milestone alongside the actual execution failure", () => {
    const turn = projectMessagesToTurns([assistant([{ type: "tool_call", record: { id: "task", name: "task", args: { task_name: "真实子任务" },
      status: "failed", startedAt: 100_000, outputPreview: "Child provider failed", delegations: [{ agent_id: "child", name: "真实子任务", status: "failed", started_at: 101_000 }] } }])], false)[0];
    expect(turn.committedCells).toEqual(expect.arrayContaining([
      expect.objectContaining({ kind: "collaboration", entries: [expect.objectContaining({ agentId: "child", agentStartedAt: 101_000 })] }),
      expect.objectContaining({ kind: "activity", status: "failed", toolCallRecords: [expect.objectContaining({ outputPreview: "Child provider failed" })] }),
    ]));
  });
  it("keeps an independent terminal model failure after an earlier failed tool", () => {
    const turn = projectMessagesToTurns([assistant([{ type: "tool_call", record: { id: "read", name: "read_file", args: { file_path: "missing.md" },
      status: "failed", startedAt: 101_000, userSummary: "File not found", outputPreview: "File not found" } }],
    { terminalStatus: "failed", completedAt: 105_000, failureMessage: "The server closed the stream before returning an answer." })], false)[0];
    render(<ChatTurn turn={turn} conversationId="owner" />);
    expect(screen.queryByText("File not found")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "展开活动详情" }));
    expect(screen.getByText("File not found")).toBeTruthy();
    expect(screen.getByText("The server closed the stream before returning an answer.")).toBeTruthy();
    expect(turn.committedCells.filter((cell) => cell.kind === "error")).toHaveLength(1);
    expect(useAppStore.getState().draft).toBe("原草稿");
  });

  it("does not add a second terminal error when the same failure belongs to the tool item", () => {
    const turn = projectMessagesToTurns([assistant([{ type: "tool_call", record: { id: "read", name: "read_file", args: { file_path: "missing.md" },
      status: "failed", startedAt: 101_000, userSummary: "File not found", outputPreview: "File not found" } }],
    { terminalStatus: "failed", failureMessage: "File not found" })], false)[0];
    expect(turn.committedCells.some((cell) => cell.kind === "error")).toBe(false);
    render(<ChatTurn turn={turn} conversationId="owner" />);
    expect(screen.queryByText("File not found")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "展开活动详情" }));
    expect(screen.getAllByText("File not found")).toHaveLength(1);
  });

  it("projects the retained full command output and opens it against the transcript owner", () => {
    const turn = projectMessagesToTurns([assistant([{ type: "tool_call", record: { id: "command", name: "run_command", args: { command: "python report.py" },
      status: "success", startedAt: 100_000, finishedAt: 103_000, activityKind: "commandExecution", stdoutPreview: "first line\n[…]\nlast line",
      artifactId: "retained-command-output", artifactKind: "command_output", artifactMediaType: "text/plain" } }])], false)[0];
    const cell = turn.committedCells.find((cell) => cell.kind === "exec")!;
    expect(cell.kind).toBe("exec");
    if (cell.kind !== "exec") throw new Error("Expected the actual command projection");
    render(<ExecCell cell={cell} conversationId="owner" />);
    expect(screen.queryByText(/first line/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^展开命令详情/ }));
    fireEvent.click(screen.getByRole("button", { name: "查看完整输出" }));
    expect(openArtifactPreview).toHaveBeenCalledWith(expect.objectContaining({ artifactId: "retained-command-output", kind: "text", conversationId: "owner" }));
  });

  it("opens file patches independently and preserves that choice across transcript remount", () => {
    const turn = projectMessagesToTurns([assistant([{ type: "tool_call", record: { id: "edit", name: "apply_patch", args: {}, status: "success", startedAt: 100_000,
      activityKind: "fileChange", diff: { plus: 2, minus: 2, files: [
        { path: "a.ts", plus: 1, minus: 1, patch: "@@ -1 +1 @@\n-old-a\n+new-a" },
        { path: "b.ts", plus: 1, minus: 1, patch: "@@ -1 +1 @@\n-old-b\n+new-b" },
      ] } } }])], false)[0];
    const first = render(<ChatTurn turn={turn} conversationId="owner" isTranscriptMode />);
    fireEvent.click(screen.getByRole("button", { name: "展开活动详情" }));
    fireEvent.click(screen.getByRole("button", { name: "展开 a.ts 的修改" }));
    expect(screen.getByText("new-a")).toBeTruthy();
    expect(screen.queryByText("new-b")).toBeNull();
    first.unmount();
    render(<ChatTurn turn={turn} conversationId="owner" isTranscriptMode />);
    expect(screen.getByText("new-a")).toBeTruthy();
    expect(screen.queryByText("new-b")).toBeNull();
  });

  it("places a quote above the separate text bubble", () => {
    const view = render(<UserMessageCell conversationId="owner" cell={{ kind: "user_message", id: "user", content: "按这个继续", createdAt: 1,
      quotedMessage: { id: "quoted", role: "assistant", content: "已经确认的结论" } }} />);
    const quote = view.container.querySelector(".user-cell-quote")!;
    const bubble = view.container.querySelector(".edit-bubble-wrap")!;
    expect(quote.nextElementSibling).toBe(bubble);
    expect(bubble.querySelector(".message-quote")).toBeNull();
    expect(screen.getByText("已经确认的结论")).toBeTruthy();
  });

  it("starts elapsed work at the assistant run instead of when a queued user message was created", () => {
    const user: ChatMessage = { id: "queued-user", role: "user", content: "等前一轮结束再执行", timestamp: 1_000, artifacts: [] };
    const turn = projectMessagesToTurns([user, assistant([], { isStreaming: true, runStartedAt: 95_000.25 })], true)[0];
    expect(turn.startedAt).toBe(95_000.25);
    const unknownStart = projectMessagesToTurns([user, assistant([], { isStreaming: true })], true)[0];
    expect(unknownStart.startedAt).toBeUndefined();
  });
});
