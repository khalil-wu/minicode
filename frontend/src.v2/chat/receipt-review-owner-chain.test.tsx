// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { act, cleanup, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ToolCallEvent, ToolResultEvent } from "../protocol/events";
import type { ChatMessage } from "../stores/types";
import { useAppStore } from "../stores";
import { reduceToolCallResult, reduceToolCallStart, type ToolCallRecord } from "../lib/tool-call-reducer";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import { recordStreamingToolUpdate, streamingMessageUpdate } from "../lib/message-changes";
import { buildReviewHistory } from "../lib/review-history";
import { createStreamBuffer } from "../lib/stream-buffer";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { hydrateMessages } from "./transcriptHydration";
import { handleChatStreamEvent } from "./chatStreamEvents";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { handleDiffEvent } from "./diffEvents";
import { useTurnChanges } from "./useTurnChanges";
import { ActivityCell } from "./cells/ActivityCell";
import { ExecCell } from "./cells/ExecCell";
import { ToolCallCard } from "./tool-calls/ToolCallCard";
import { InlineDiff } from "./diff/InlineDiff";
import { useWorkspaceGit } from "../hooks/useWorkspaceGit";
import { fetchWorkspaceGitWorktree } from "../protocol/workspace";

vi.mock("../protocol/workspace", async (original) => ({ ...await original<typeof import("../protocol/workspace")>(), fetchWorkspaceGitWorktree: vi.fn() }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

const capture = JSON.parse(readFileSync(resolve(process.cwd(), "../.tmp/full-chain-audit-20261002/parallel-receipt-review-chain-20261004/receipt-capture.json"), "utf8")) as {
  call: ToolCallEvent; result: ToolResultEvent; restored: ToolCallRecord;
};
const initial = useAppStore.getState();
const patch = (value: string) => `diff --git a/file.ts b/file.ts\n--- a/file.ts\n+++ b/file.ts\n@@ -1 +1 @@\n-old\n+${value}\n`;
const message = (id: string, turnId: string, record?: ToolCallRecord): ChatMessage => ({
  id, role: "assistant", turnId, content: "", artifacts: [], timestamp: 1,
  blocks: record ? [{ type: "tool_call", record }] : [],
});
const record = (): ToolCallRecord => reduceToolCallResult(reduceToolCallStart(new Map(), capture.call, 1), capture.result, 999).get(capture.call.id)!;

beforeEach(() => {
  vi.mocked(fetchWorkspaceGitWorktree).mockReset().mockImplementation(() => new Promise(() => {}));
  useAppStore.setState({ ...initial, conversationId: "receipt-owner", workingDirectory: "C:/receipt-owner", messages: [],
    conversationMessages: {}, sideChats: {}, conversationStreaming: {}, turnDiffs: {},
    conversationWorkbenchStates: {}, diffReview: null, gitReviewRequest: null, workspaceGit: null, isConnected: false });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it("retains the exact real public result receipt and completion time through live and restored tools", () => {
  const decoded = normalizeInboundServerEvent(capture.result)! as ToolResultEvent;
  const live = reduceToolCallResult(reduceToolCallStart(new Map(), capture.call, 1), decoded, 999).get(capture.call.id)!;
  expect(live.cleanupReceipt).toEqual(capture.result.cleanup_receipt);
  expect(live.finishedAt).toBe(capture.result.completed_at_ms);
  const restored = hydrateMessages([{ ...message("restore", "actual-run"), blocks: [{ type: "tool_call", record: capture.restored }] }]);
  expect(getToolCallsFromMessage(restored[0])[0].cleanupReceipt).toEqual(live.cleanupReceipt);
  expect(getToolCallsFromMessage(restored[0])[0].finishedAt).toBe(live.finishedAt);
});

it("accepts a legacy unsequenced result after a sequenced start without inventing a new fence", () => {
  const started = reduceToolCallStart(new Map(), { ...capture.call, seq: 7 }, 1);
  const completed = reduceToolCallResult(started, capture.result).get(capture.call.id)!;
  expect(completed.status).toBe("failed");
  expect(completed.seq).toBe(7);
  const assistant = { ...message("live", "actual-run"), isStreaming: true };
  useAppStore.setState({ messages: [assistant], isStreaming: true });
  const buffer = createStreamBuffer(() => {});
  handleChatStreamEvent({ ...capture.call, seq: 7 }, "receipt-owner", { textStreamBuffer: buffer });
  handleChatStreamEvent(capture.result, "receipt-owner", { textStreamBuffer: buffer });
  expect(getToolCallsFromMessage(useAppStore.getState().messages[0])[0]).toMatchObject({ status: "failed", cleanupReceipt: capture.result.cleanup_receipt });
  buffer.destroy();
});

it("fills missing cleanup facts on an unsequenced terminal replay without replacing its status", () => {
  const completed = { ...record(), cleanupReceipt: undefined, seq: undefined };
  const next = reduceToolCallResult(new Map([[completed.id, completed]]), { ...capture.result, status: "success", is_error: false }).get(completed.id)!;
  expect(next.status).toBe("failed");
  expect(next.cleanupReceipt).toEqual(capture.result.cleanup_receipt);
});

it.each(["activity", "card"])("shows the actual unresolved process receipt in collapsed %s UI", (kind) => {
  const tool = record();
  if (kind === "activity") render(<ActivityCell cell={{ kind: "activity", id: "pending-process", activityKind: "workspaceSearch", title: "Search", status: "failed", collapsed: true, toolCallRecords: [tool], startedAt: 1 }} conversationId="receipt-owner" />);
  else render(<ToolCallCard record={{ ...tool, status: "cancelled" }} viewMode="concise" conversationId="receipt-owner" />);
  expect(screen.getByText(/资源清理尚未确认（PID 4242）/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /kill|终止进程|停止进程/i })).toBeNull();
});

it("keeps the unresolved receipt visible through actual command cell projection", () => {
  const command = { ...record(), name: "run_command", activityKind: "commandExecution", resultKind: "command", args: { command: "recorded command" } };
  const turns = projectMessagesToTurns([message("command", "actual-run", command)], false, "C:/receipt-owner");
  const cells = turns.flatMap((turn) => turn.committedCells);
  const exec = cells.find((cell) => cell.kind === "exec");
  expect(exec?.kind).toBe("exec");
  if (exec?.kind === "exec") render(<ExecCell cell={exec} />);
  expect(screen.getByText(/资源清理尚未确认（PID 4242）/)).toBeTruthy();
});

it("does not retain a pending notice after a sequenced proof of completion", () => {
  const prior = { ...record(), seq: 1 };
  const settled = reduceToolCallResult(new Map([[prior.id, prior]]), { ...capture.result, seq: 2, cleanup_receipt: { completed: true, pending: 0 } }).get(prior.id)!;
  render(<ToolCallCard record={settled} conversationId="receipt-owner" />);
  expect(screen.queryByText(/资源清理尚未确认/)).toBeNull();
});

it("shows the same actual process identity when its producer receipt is unwrapped", () => {
  render(<ToolCallCard record={{ ...record(), cleanupReceipt: capture.result.cleanup_receipt?.resource_cleanup as Record<string, unknown> }} conversationId="receipt-owner" />);
  expect(screen.getByText(/资源清理尚未确认（PID 4242）/)).toBeTruthy();
});

it.each(["failed", "blocked", "cancelled", "running", "pending", "partial"] as const)("excludes %s input proposals from applied review history", (status) => {
  const proposal = { id: "proposal", name: "edit_file", args: { path: "file.ts", patch: patch("proposal") }, status, startedAt: 1, diff: { plus: 1, minus: 1 } };
  expect(buildReviewHistory([message("proposal", "turn", proposal)])[0].files).toEqual([]);
});

it("keeps actual partial changes but clears net-zero and removed temporary edits", () => {
  const applied: ToolCallRecord = { id: "applied", name: "edit_file", args: { path: "file.ts" }, status: "partial", startedAt: 1, diff: { plus: 1, minus: 1, patch: patch("actual") } };
  const messages = [message("applied", "turn", applied)];
  expect(buildReviewHistory(messages)[0].files).toHaveLength(1);
  expect(buildReviewHistory(messages, { threadId: "receipt-owner", turnId: "turn", diff: "", updatedAt: 1 })[0].files).toEqual([]);
  expect(buildReviewHistory([message("removed", "turn", { ...applied, temporaryRemoved: true })])[0].files).toEqual([]);
});

it("does not apply another conversation or message's aggregate to current history", () => {
  const applied: ToolCallRecord = { id: "applied", name: "edit_file", args: {}, status: "success", startedAt: 1, diff: { plus: 1, minus: 1, patch: patch("original") } };
  const messages = [message("owned-message", "turn", applied)];
  for (const owner of [{ threadId: "other" }, { threadId: "receipt-owner", messageId: "other-message" }]) {
    const history = buildReviewHistory(messages, { ...owner, turnId: "turn", diff: patch("foreign"), updatedAt: 1 }, "receipt-owner");
    expect(history[0].files[0].revisions[0].diff).toContain("+original");
  }
});

it("rejects older-turn and duplicate-revision diffs using transcript owner order", () => {
  useAppStore.setState({ messages: [message("first", "turn-1"), message("second", "turn-2")], turnDiffs: { "receipt-owner": { threadId: "receipt-owner", turnId: "turn-2", messageId: "second", diff: patch("latest"), revision: 3, updatedAt: 1 } } });
  handleDiffEvent({ type: "turn.diff.updated", thread_id: "receipt-owner", conversation_id: "receipt-owner", turn_id: "turn-1", message_id: "first", diff: patch("old"), revision: 999 });
  handleDiffEvent({ type: "turn.diff.updated", thread_id: "receipt-owner", conversation_id: "receipt-owner", turn_id: "turn-2", message_id: "second", diff: patch("duplicate"), revision: 3 });
  expect(useAppStore.getState().turnDiffs["receipt-owner"].diff).toBe(patch("latest"));
});

it("cannot open a captured turn review after the active conversation changes", () => {
  const turnDiff = { threadId: "receipt-owner", turnId: "turn", messageId: "owned", diff: patch("actual"), updatedAt: 1 };
  useAppStore.setState({ messages: [message("owned", "turn")], turnDiffs: { "receipt-owner": turnDiff } });
  const hook = renderHook(useTurnChanges);
  const open = hook.result.current.openReview;
  act(() => useAppStore.setState({ conversationId: "other", workingDirectory: "C:/other", messages: [] }));
  act(open);
  expect(useAppStore.getState().diffReview).toBeNull();
});

it("preserves renamed source identity from actual Git patch metadata", () => {
  useAppStore.setState({ gitChanges: { workingTree: [], staged: [], untracked: [], loading: true } });
  handleDiffEvent({ type: "diff.git_working_tree", conversation_id: "receipt-owner", workspace_root: "C:/receipt-owner", files: [{ path: "new.ts", patch: "diff --git a/old.ts b/new.ts\nsimilarity index 100%\nrename from old.ts\nrename to new.ts\n", additions: 0, deletions: 0 }] });
  expect(useAppStore.getState().gitChanges.workingTree[0]).toMatchObject({ path: "new.ts", oldPath: "old.ts" });
});

it("marks elided context and never fabricates source coordinates for fragments", () => {
  const view = render(<InlineDiff patch={"@@ -1,8 +1,8 @@\n a\n b\n c\n-old\n+new\n d\n e\n f\n g"} contextLines={0} />);
  expect([...view.container.querySelectorAll(".inline-diff-line-marker")].map((node) => node.textContent)).toEqual([" …", " …"]);
  view.rerender(<InlineDiff patch={"-old\n+new"} />);
  expect([...view.container.querySelectorAll(".inline-diff-number")].map((node) => node.textContent)).toEqual(["", ""]);
});

it("invalidates the streaming projection fast path when canonical group ownership changes", () => {
  const base = { ...message("stream", "turn", { ...record(), groupId: "group-1" }), isStreaming: true };
  const next = { ...base, blocks: [{ type: "tool_call" as const, record: { ...record(), groupId: "group-2" } }] };
  recordStreamingToolUpdate(base, next, 0);
  expect(streamingMessageUpdate(next)).toBeUndefined();
});

it("retains actual file boundaries when a multi-file tool patch is shown inline", () => {
  render(<InlineDiff patch={patch("first").replaceAll("file.ts", "first.ts") + patch("second").replaceAll("file.ts", "second.ts")} />);
  expect(screen.getByText("文件：first.ts")).toBeTruthy();
  expect(screen.getByText("文件：second.ts")).toBeTruthy();
});

it("does not commit a previous workspace's Git response before React effect cleanup", async () => {
  let resolveFetch!: (value: Awaited<ReturnType<typeof fetchWorkspaceGitWorktree>>) => void;
  vi.mocked(fetchWorkspaceGitWorktree).mockImplementationOnce(() => new Promise((resolve) => { resolveFetch = resolve; }));
  renderHook(useWorkspaceGit);
  expect(useAppStore.getState().workspaceGit).toBeNull();
  await act(async () => {
    useAppStore.setState({ workingDirectory: "C:/other" });
    resolveFetch({ current_branch: "stale-branch", is_worktree: false, current_path: "C:/receipt-owner", worktrees: [], worktree_count: 1 });
    await Promise.resolve();
  });
  expect(useAppStore.getState().workspaceGit?.branch).not.toBe("stale-branch");
});
