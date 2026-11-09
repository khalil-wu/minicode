/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { handleControlEvent } from "./controlEvents";
import { handleChatStreamEvent } from "./chatStreamEvents";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { InlineAgentPrompt } from "./InlineAgentPrompt";
import type { ServerEvent } from "../protocol/events";

const mocks = vi.hoisted(() => ({ response: vi.fn(), command: vi.fn(() => true) }));
vi.mock("../protocol/ws-outbox", async original => ({ ...await original<Record<string, unknown>>(),
  sendPromptResponseCommand: mocks.response, sendClientCommand: mocks.command }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
const receipt = (message = "", success = true) => ({ type: "command.result", command: "control_response", level: success ? "success" : "error", message, data: {} });
const deferred = () => { let resolve!: (value: ReturnType<typeof receipt>) => void;
  const promise = new Promise<ReturnType<typeof receipt>>(done => { resolve = done; }); return { promise, resolve }; };
const owner = (conversationId: string, turnId = "turn-1", taskId = "task-1") => ({ conversationId, turnId, taskId, messageId: `${turnId}-message` });
const event = (scope = owner("A"), requestId = "shared-call", diff = false) => ({
  type: "control_request", request_id: requestId, conversation_id: scope.conversationId, turn_id: scope.turnId,
  message_id: scope.messageId, task_id: scope.taskId, replayed: true,
  request: { subtype: "can_use_tool", tool_name: "run_command", tool_use_id: requestId,
    input: { command: `echo ${scope.conversationId}` }, command: ["echo", scope.conversationId], display_command: `echo ${scope.conversationId}`,
    cwd: "C:/actual-owner", request_digest: "a".repeat(64), available_decisions: ["approved", "abort"],
    ...(diff ? { diff: { files: [{ path: "actual.txt", patch: "@@ -1 +1 @@\n-old\n+new", additions: 1, deletions: 1 }] } } : {}) },
});
const receive = (raw: unknown) => { const normalized = normalizeInboundServerEvent(raw); expect(normalized).not.toBeNull(); handleControlEvent(normalized!); };
const cancel = (scope: ReturnType<typeof owner>, requestIds: string[]) => handleControlEvent({ type: "approval.cancelled",
  conversation_id: scope.conversationId, turn_id: scope.turnId, message_id: scope.messageId, task_id: scope.taskId,
  request_ids: requestIds, reason: "original turn completed" } as ServerEvent);
const buffer = { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() };

beforeEach(() => {
  mocks.response.mockReset().mockResolvedValue(receipt()); mocks.command.mockClear();
  useAppStore.setState({ conversationId: "A", pendingConversationSwitchId: null, pendingApproval: null, approvalQueue: [],
    pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [], diffReview: null, promptDrafts: {},
    subagents: [], draft: "A unsent draft", attachments: [{ id: "A-file", name: "actual.txt" } as never],
    messages: [], conversationMessages: {}, conversationAgentStates: {}, agentProgress: [], conversationStreaming: {}, isStreaming: false });
});
afterEach(cleanup);

it("keeps the original tool call id separate across conversations, turns and actual task ids", () => {
  const scopes = [owner("A"), owner("B"), owner("A", "turn-2"), owner("A", "turn-1", "task-2")];
  for (const scope of scopes) receive(event(scope));
  receive(event(scopes[0]));
  expect([useAppStore.getState().pendingApproval, ...useAppStore.getState().approvalQueue].map(item =>
    [item!.conversationId, item!.turnId, item!.taskId])).toEqual(scopes.map(scope => [scope.conversationId, scope.turnId, scope.taskId]));
});

it("retains the original pending state across a hot conversation switch and late success does not clear B", async () => {
  const completion = deferred(); mocks.response.mockReturnValueOnce(completion.promise);
  receive(event()); render(<InlineAgentPrompt />);
  fireEvent.click(screen.getByRole("button", { name: "允许使用工具" }));
  expect(useAppStore.getState().pendingApproval?.status).toBe("submitted");
  expect(mocks.response.mock.calls[0][0]).toMatchObject({ conversation_id: "A", turn_id: "turn-1", message_id: "turn-1-message", task_id: "task-1" });
  act(() => { receive(event(owner("B"))); useAppStore.setState({ conversationId: "B", draft: "B unsent draft", attachments: [{ id: "B-file", name: "B.txt" } as never] }); });
  expect((screen.getByRole("button", { name: "允许使用工具" }) as HTMLButtonElement).disabled).toBe(false);
  act(() => useAppStore.setState({ conversationId: "A" }));
  expect((screen.getByRole("button", { name: "允许使用工具" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByRole("button", { name: "允许使用工具" }).textContent).toBe("允许一次");
  expect(screen.getByRole("button", { name: "允许使用工具" }).querySelector("svg.animate-spin")).not.toBeNull();
  act(() => useAppStore.setState({ conversationId: "B" }));
  await act(async () => completion.resolve(receipt()));
  expect(useAppStore.getState().pendingApproval).toMatchObject({ conversationId: "B", requestId: "shared-call" });
  expect(useAppStore.getState().pendingApproval?.status).toBeUndefined();
  expect((screen.getByRole("button", { name: "允许使用工具" }) as HTMLButtonElement).disabled).toBe(false);
  expect(useAppStore.getState()).toMatchObject({ draft: "B unsent draft", attachments: [{ id: "B-file" }] });
});

it("keeps a late semantic refusal on its original request and does not show its error on B", async () => {
  const completion = deferred(); mocks.response.mockReturnValueOnce(completion.promise);
  receive(event()); render(<InlineAgentPrompt />); fireEvent.click(screen.getByRole("button", { name: "允许使用工具" }));
  act(() => { receive(event(owner("B"))); useAppStore.setState({ conversationId: "B" }); });
  await act(async () => completion.resolve(receipt("Original A approval was refused", false)));
  expect(screen.queryByText("Original A approval was refused")).toBeNull();
  expect((screen.getByRole("button", { name: "允许使用工具" }) as HTMLButtonElement).disabled).toBe(false);
  act(() => useAppStore.setState({ conversationId: "A" }));
  expect(screen.getByText("Original A approval was refused").closest('[role="alert"]')).not.toBeNull();
  expect((screen.getByRole("button", { name: "允许使用工具" }) as HTMLButtonElement).disabled).toBe(false);
});

it("scopes cancellation of all three prompt queues to the original conversation and turn", () => {
  const a = owner("A"), b = owner("A", "turn-2", "task-2"), parallel = owner("A", "turn-1", "parallel-task"), other = owner("B");
  for (const scope of [a, b, parallel, other]) {
    receive(event(scope)); receive(event(scope, "shared-diff", true));
    receive({ type: "control_request", request_id: "shared-input", conversation_id: scope.conversationId, turn_id: scope.turnId,
      message_id: scope.messageId, task_id: scope.taskId, replayed: true,
      request: { subtype: "elicitation", tool_use_id: "shared-input", mode: "form", prompt: "Reply to the original question", question: "Original question", schema: {} } });
  }
  useAppStore.getState().updatePromptDraft({ requestId: "shared-input", conversationId: b.conversationId,
    inputOwner: b }, { answer: "Keep B's original answer" });
  cancel(a, ["shared-call", "shared-diff", "shared-input"]);
  const state = useAppStore.getState();
  expect([state.pendingApproval, ...state.approvalQueue].map(item => item!.turnId)).toEqual([b.turnId, parallel.turnId, other.turnId]);
  expect([state.pendingDiffReview, ...state.diffReviewQueue].map(item => item!.taskId)).toEqual([b.taskId, parallel.taskId, other.taskId]);
  expect([state.pendingAskUser, ...state.askUserQueue].map(item => item!.turnId)).toEqual([b.turnId, parallel.turnId, other.turnId]);
  expect(state.diffReview).toMatchObject({ conversationId: b.conversationId, turnId: b.turnId, taskId: b.taskId });
  expect(Object.values(state.promptDrafts).some(draft => draft.answer === "Keep B's original answer")).toBe(true);
});

it("keeps the latest same-id diff review when the original panel submission resolves late", async () => {
  const completion = deferred(); mocks.response.mockReturnValueOnce(completion.promise);
  receive(event(owner("A"), "shared-diff", true));
  const submission = useAppStore.getState().submitDiffReviewWithComments();
  expect(useAppStore.getState().pendingDiffReview?.reviewState?.status).toBe("submitted");
  receive(event(owner("B", "turn-2", "task-2"), "shared-diff", true));
  const b = useAppStore.getState().diffReviewQueue[0];
  useAppStore.setState({ conversationId: "B", diffReview: b.reviewState });
  completion.resolve(receipt()); await submission;
  expect(useAppStore.getState().pendingDiffReview).toMatchObject({ conversationId: "B", taskId: "task-2" });
  expect(useAppStore.getState().diffReview).toMatchObject({ conversationId: "B", turnId: "turn-2", status: "pending" });
});

it("preserves the original diff failure after switching away while leaving B's review usable", async () => {
  const completion = deferred(); mocks.response.mockReturnValueOnce(completion.promise);
  receive(event(owner("A"), "shared-diff", true));
  const submission = useAppStore.getState().submitPartialApproval();
  receive(event(owner("B", "turn-2", "task-2"), "shared-diff", true));
  const b = useAppStore.getState().diffReviewQueue[0];
  useAppStore.setState({ conversationId: "B", diffReview: b.reviewState });
  completion.resolve(receipt("Original diff approval rejected", false)); await submission;
  expect(useAppStore.getState().diffReview).toMatchObject({ conversationId: "B", status: "pending" });
  expect(useAppStore.getState().pendingDiffReview?.reviewState).toMatchObject({ conversationId: "A", status: "error", error: "Original diff approval rejected" });
  useAppStore.setState({ conversationId: "A" }); render(<InlineAgentPrompt />);
  expect(screen.getByRole("alert")).toHaveProperty("textContent", "Original diff approval rejected");
  expect((screen.getByRole("button", { name: "允许文件更改" }) as HTMLButtonElement).disabled).toBe(false);
});

it("does not apply an original same-call error to B's pending approval or diff", () => {
  receive(event(owner("B", "turn-2", "task-2"))); receive(event(owner("B", "turn-2", "task-2"), "shared-call", true));
  useAppStore.setState({ conversationId: "B", conversationMessages: { "error-owner-A": [{ id: "error-message-A", role: "assistant", content: "", artifacts: [], timestamp: 1, turnId: "error-turn-A", isStreaming: true }] } });
  handleChatStreamEvent({ type: "error", conversation_id: "error-owner-A", message_id: "error-message-A", turn_id: "error-turn-A", task_id: "task-1",
    tool_call_id: "shared-call", message: "A's original operation error", recoverable: true } as ServerEvent, "error-owner-A", { textStreamBuffer: buffer });
  expect(useAppStore.getState().pendingApproval).toMatchObject({ conversationId: "B" });
  expect(useAppStore.getState().pendingApproval?.status).toBeUndefined();
  expect(useAppStore.getState().pendingDiffReview).toMatchObject({ conversationId: "B", taskId: "task-2" });
  expect(useAppStore.getState().diffReview?.status).not.toBe("error");
});

it("does not let an old done event clear parallel task or newer turn same-id prompts", () => {
  const a = owner("A"), next = owner("A", "turn-2", "task-2"), parallel = owner("A", "turn-1", "parallel-task");
  receive(event(a)); receive(event(parallel)); receive(event(next));
  useAppStore.setState({ messages: [{ id: a.messageId, role: "assistant", content: "Original final", artifacts: [], timestamp: 1, turnId: a.turnId, isStreaming: true }],
    conversationStreaming: { A: true }, isStreaming: true });
  handleChatStreamEvent({ type: "done", conversation_id: "A", turn_id: a.turnId, message_id: a.messageId, task_id: a.taskId,
    status: "completed", duration_ms: 1000 } as ServerEvent, "A", { textStreamBuffer: buffer });
  expect(useAppStore.getState().pendingApproval).toMatchObject({ conversationId: "A", turnId: parallel.turnId, taskId: parallel.taskId });
  expect(useAppStore.getState().approvalQueue[0]).toMatchObject({ conversationId: "A", turnId: next.turnId, taskId: next.taskId });
  expect(useAppStore.getState().draft).toBe("A unsent draft");
});

it("keeps Source's original approval label and spinner for an actual MCP approval while its response is pending", async () => {
  const completion = deferred(); mocks.response.mockReturnValueOnce(completion.promise);
  const raw = event();
  receive({ ...raw, request: { subtype: "can_use_tool", tool_name: "mcp__fixture__read", tool_use_id: raw.request_id,
    input: { path: "actual.txt" }, mcp_approval: { server: "fixture", tool_name: "read", approval_mode: "prompt", allow_session_remember: false } } });
  render(<InlineAgentPrompt />); fireEvent.click(screen.getByRole("button", { name: "允许使用工具" }));
  const allow = screen.getByRole("button", { name: "允许使用工具" });
  expect(allow.textContent).toBe("允许一次");
  expect(allow.querySelector("svg.animate-spin")).not.toBeNull();
  expect(allow.querySelector("kbd")).toBeNull();
  expect((screen.getByRole("button", { name: "拒绝使用工具" }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => completion.resolve(receipt("Actual approval refused", false)));
  expect(screen.getByText("Actual approval refused")).toBeTruthy();
});
