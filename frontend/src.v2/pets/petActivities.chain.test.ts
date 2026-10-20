/* @vitest-environment jsdom */
import { beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { translate, type Translate } from "../i18n";
import { desktopPetActivity, petTaskActions, recordPetCompletionNotification, submitPetFollowup } from "./petActivities";
import type { CompletionNotification } from "../protocol/notification-types";

const mocks = vi.hoisted(() => ({ response: vi.fn(), command: vi.fn(), followup: vi.fn() }));
vi.mock("../protocol/ws-outbox", async load => ({ ...await load<Record<string, unknown>>(), sendPromptResponseCommand: (...args: unknown[]) => mocks.response(...args), sendClientCommandAwaitResult: (...args: unknown[]) => mocks.command(...args) }));
vi.mock("../chat/sendChatMessage", () => ({ sendChatMessage: (...args: unknown[]) => mocks.followup(...args) }));
const t: Translate = (key, values) => translate("en-US", key, values);
beforeEach(() => {
  mocks.response.mockReset().mockResolvedValue({ level: "success" });
  mocks.command.mockReset().mockResolvedValue({ level: "success" });
  mocks.followup.mockReset().mockResolvedValue(true);
  useAppStore.setState({ conversationId: "chat", conversations: [{ id: "chat", title: "Actual task", updatedAt: "2026-10-19" }],
    messages: [], conversationMessages: {}, sideChats: {}, conversationStreaming: {}, isStreaming: false, pendingApproval: null, approvalQueue: [],
    pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [] });
});

it("stops the actual turn and sends a targeted follow-up without consuming the main draft", async () => {
  useAppStore.setState({ isConnected: true, isStreaming: true, draft: "Keep my main draft", followUpBehavior: "queue",
    messages: [{ id: "assistant", turnId: "running-turn", role: "assistant", content: "", timestamp: 1, artifacts: [], isStreaming: true }] });
  const stop = petTaskActions(useAppStore.getState(), "chat", t)[0];
  await stop.submit();
  expect(mocks.command).toHaveBeenCalledWith({ type: "interrupt", conversation_id: "chat", turn_id: "running-turn", message_id: "assistant" }, "interrupt");
  expect(useAppStore.getState().isStreaming).toBe(true);
  await submitPetFollowup("chat", "Actual follow-up", t);
  expect(mocks.followup).toHaveBeenCalledWith({ displayContent: "Actual follow-up", backendContent: "Actual follow-up", conversationId: "chat", allowWhileStreaming: true, busyBehavior: "queue", awaitAdmission: true });
  expect(useAppStore.getState().draft).toBe("Keep my main draft");
  mocks.followup.mockResolvedValueOnce(false);
  await expect(submitPetFollowup("chat", "Rejected text", t)).rejects.toThrow("not accepted");
});

it("keeps an original approval pending until its backend receipt and includes its full owner", async () => {
  let accept!: (value: unknown) => void;
  mocks.response.mockImplementationOnce(() => new Promise(resolve => { accept = resolve; }));
  useAppStore.setState({ pendingApproval: { requestId: "request", conversationId: "chat", turnId: "turn", messageId: "message",
    toolName: "run_command", args: { command: "npm test" }, displayCommand: "npm test", requestDigest: "frozen", availableDecisions: ["approved", "abort"] } });
  const activity = desktopPetActivity(useAppStore.getState(), t);
  expect(activity.tasks[0]).toMatchObject({ id: "chat", title: "Actual task", status: "waiting", detail: "npm test" });
  const actions = petTaskActions(useAppStore.getState(), "chat", t);
  expect(actions).toHaveLength(2);
  const response = actions[0].submit();
  expect(useAppStore.getState().pendingApproval?.requestId).toBe("request");
  expect(mocks.response).toHaveBeenCalledWith(expect.objectContaining({ request_id: "request", conversation_id: "chat", turn_id: "turn", message_id: "message",
    response: { subtype: "success", response: { action: "approve", decision: "approved", request_digest: "frozen" } } }));
  accept({ level: "success" });
  await response;
  expect(useAppStore.getState().pendingApproval).toBeNull();
});

it("keeps rejection evidence and cannot reuse an action after its frozen request changes", async () => {
  useAppStore.setState({ pendingApproval: { requestId: "same-id", conversationId: "chat", turnId: "old", toolName: "run_command", args: {}, requestDigest: "old-digest" } });
  const oldId = petTaskActions(useAppStore.getState(), "chat", t)[0].id;
  useAppStore.setState({ pendingApproval: { ...useAppStore.getState().pendingApproval!, turnId: "new", requestDigest: "new-digest" } });
  const actions = petTaskActions(useAppStore.getState(), "chat", t);
  expect(actions.every(action => action.id !== oldId)).toBe(true);
  mocks.response.mockResolvedValueOnce({ level: "error", message: "Original approval no longer exists" });
  await expect(actions[0].submit()).rejects.toThrow("Original approval no longer exists");
  expect(useAppStore.getState().pendingApproval?.requestDigest).toBe("new-digest");
});

it("replies with the real question id and excludes secret or multi-question input from Mini choices", async () => {
  useAppStore.setState({ pendingAskUser: { requestId: "ask", conversationId: "chat", turnId: "turn", question: "Pick a color", requestUserInput: {
    toolCallId: "tool", isBlocking: true, questions: [{ id: "color", header: "Color", question: "Pick a color", options: [{ label: "Blue", description: "Blue pet" }] }] } } });
  const actions = petTaskActions(useAppStore.getState(), "chat", t);
  expect(actions.map(action => action.label)).toEqual(["Blue"]);
  await actions[0].submit();
  expect(mocks.response).toHaveBeenCalledWith(expect.objectContaining({ request_id: "ask", response: { subtype: "success", response: { answers: { color: { answers: ["Blue"] } } } } }));
  useAppStore.setState({ pendingAskUser: { requestId: "secret", conversationId: "chat", question: "Private", secret: true, options: [{ label: "Secret", value: "secret" }] } });
  expect(petTaskActions(useAppStore.getState(), "chat", t).map(action => ({ label: action.label, openInMainWindow: action.openInMainWindow }))).toEqual([{ label: "Review", openInMainWindow: true }]);
  expect(desktopPetActivity(useAppStore.getState(), t).tasks[0].status).toBe("waiting");
});

it("archived and delegated records cannot reappear through streaming maps or pending request queues while user forks remain", () => {
  useAppStore.setState({ conversations: [
    { id: "archived", title: "Archived", updatedAt: "2026-10-20", archived: true },
    { id: "delegated", title: "Delegated", updatedAt: "2026-10-20", threadSource: "subagent" },
    { id: "fork", title: "User fork", updatedAt: "2026-10-20", parentConversationId: "chat", branchKind: "clone", sessionStatus: "running" },
  ], conversationStreaming: { archived: true, delegated: true }, pendingApproval: { requestId: "archived-request", conversationId: "archived", toolName: "run_command", args: {} } });
  const activity = desktopPetActivity(useAppStore.getState(), t);
  expect(activity.tasks.map(task => task.id)).toEqual(["fork"]);
  expect(activity.running).toBe(true);
  expect(activity.waiting).toBe(false);
});

it("quiet completion receipts suppress only their completed turn and never suppress a real pending request", () => {
  const notice: CompletionNotification = { thread_source: "automation", automation_kind: "heartbeat", automation_notification_decision: "DONT_NOTIFY",
    heartbeat_assistant_message: null, last_agent_message: "DONT_NOTIFY", has_pending_continuation: false, eligible: true, body: null, should_mark_unread: false };
  useAppStore.setState({ conversationId: "quiet", conversations: [{ id: "quiet", title: "Quiet", updatedAt: "2026-10-20", hasUnreadTurn: true }],
    messages: [{ id: "completed", turnId: "quiet-turn", role: "assistant", content: "Actual quiet evidence", timestamp: 1, artifacts: [], terminalStatus: "completed" }] });
  recordPetCompletionNotification("quiet", notice, { turnId: "quiet-turn", messageId: "completed" });
  expect(desktopPetActivity(useAppStore.getState(), t).tasks).toEqual([]);
  useAppStore.setState({ pendingApproval: { requestId: "needs-user", conversationId: "quiet", turnId: "next-turn", toolName: "run_command", args: {} } });
  expect(desktopPetActivity(useAppStore.getState(), t).tasks[0].status).toBe("waiting");
  useAppStore.setState({ pendingApproval: null, messages: [{ id: "next-message", turnId: "next-turn", role: "assistant", content: "Actual failure", timestamp: 2, artifacts: [], terminalStatus: "failed" }] });
  expect(desktopPetActivity(useAppStore.getState(), t).tasks[0].status).toBe("failed");
  recordPetCompletionNotification("quiet", undefined, {});
});

it("complex forms, authentication, multiple questions and teammate plans remain actionable in the original main task", () => {
  const requests = [
    { promptType: "secret" as const, options: [{ label: "Must not answer here", value: "secret" }] },
    { promptType: "manual_code" as const }, { inputSchema: { type: "object", required: ["name"] } }, { elicitation: { mode: "form" as const } },
    { provider: "provider-auth", options: [{ label: "Authenticate", value: "auth" }] },
    { planReview: { subagentId: "actual-teammate" } },
    { requestUserInput: { toolCallId: "actual-call", isBlocking: true, questions: [
      { id: "one", header: "One", question: "First", options: [{ label: "A", description: "First choice" }] },
      { id: "two", header: "Two", question: "Second", options: [{ label: "B", description: "Second choice" }] },
    ] }, options: [{ label: "Incomplete flattened answer", value: "A" }] },
  ];
  for (const request of requests) {
    useAppStore.setState({ pendingAskUser: { requestId: "complex", conversationId: "chat", turnId: "actual-turn", question: "Actual complex input", ...request } });
    const actions = petTaskActions(useAppStore.getState(), "chat", t);
    expect(actions.map(action => [action.label, action.openInMainWindow])).toEqual([["Review", true]]);
    expect(desktopPetActivity(useAppStore.getState(), t).tasks[0]).toMatchObject({ status: "waiting", followUpAllowed: false });
  }
  expect(mocks.response).not.toHaveBeenCalled();
});

it("Mini permissions offer the observed turn-only allow and deny and patch Review never sends rejection", async () => {
  expect(t("Mini 拒绝")).toBe("Deny");
  expect(t("拒绝")).toBe("Reject");
  useAppStore.setState({ pendingApproval: { requestId: "permission", conversationId: "chat", turnId: "actual-turn", toolName: "request_permissions", args: {},
    permissionRequest: { permissions: { network: { enabled: true } } } } });
  const actions = petTaskActions(useAppStore.getState(), "chat", t);
  expect(actions.map(action => action.label)).toEqual(["Allow once", "Deny"]);
  await actions[0].submit();
  expect(mocks.response).toHaveBeenCalledWith(expect.objectContaining({ response: { subtype: "success", response: { permissions: { network: { enabled: true } }, scope: "turn" } } }));
  useAppStore.setState({ pendingDiffReview: { requestId: "patch", conversationId: "chat", turnId: "actual-turn", diff: "Actual diff", filePath: "actual.ts" } });
  const patch = petTaskActions(useAppStore.getState(), "chat", t);
  expect(patch.map(action => [action.label, Boolean(action.openInMainWindow)])).toEqual([["Apply", false], ["Review", true]]);
  expect(desktopPetActivity(useAppStore.getState(), t).tasks[0].status).toBe("waiting");
});

it("an old successful backend receipt cannot clear its replacement request", async () => {
  let accept!: (result: unknown) => void;
  mocks.response.mockImplementationOnce(() => new Promise(resolve => { accept = resolve; }));
  useAppStore.setState({ pendingApproval: { requestId: "same-id", conversationId: "chat", turnId: "turn", toolName: "run_command", args: {}, requestDigest: "first" } });
  const submitted = petTaskActions(useAppStore.getState(), "chat", t)[0].submit();
  useAppStore.setState({ pendingApproval: { ...useAppStore.getState().pendingApproval!, requestDigest: "replacement" } });
  accept({ level: "success" }); await submitted;
  expect(useAppStore.getState().pendingApproval?.requestDigest).toBe("replacement");
});
