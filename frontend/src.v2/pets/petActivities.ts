import type { DesktopPetActivity, DesktopPetTask } from "../desktop/runtime";
import type { Translate } from "../i18n";
import type { AppStore, PendingApproval, PendingAskUser, PendingDiffReview } from "../stores/types";
import { useAppStore } from "../stores";
import { buildApprovalResponseCommand, buildAskUserResponseCommand,
  buildNetworkApprovalResponseCommand, buildPermissionsResponseCommand, buildRequestUserInputResponseCommand } from "../protocol/prompt-responses";
import { commandResultSucceeded, sendClientCommandAwaitResult, sendPromptResponseCommand } from "../protocol/ws-outbox";
import type { ClientCommand } from "../protocol/events";
import { buildInterruptCommand, hasInterruptFence } from "../lib/interrupt-command";
import { sendChatMessage } from "../chat/sendChatMessage";
import { petCompletionNotification } from "./petNotificationReceipts";
export { recordPetCompletionNotification } from "./petNotificationReceipts";

type Action = { id: string; label: string; openInMainWindow?: boolean; submit: () => Promise<void> };
const owner = (request: PendingApproval | PendingAskUser | PendingDiffReview) => ({
  conversationId: request.conversationId, turnId: request.turnId, messageId: request.messageId,
  ...("taskId" in request ? { taskId: request.taskId } : {}),
});
const approvalRequests = (state: AppStore) => [...(state.pendingApproval ? [state.pendingApproval] : []), ...state.approvalQueue];
const askRequests = (state: AppStore) => [...(state.pendingAskUser ? [state.pendingAskUser] : []), ...state.askUserQueue];
const diffRequests = (state: AppStore) => [...(state.pendingDiffReview ? [state.pendingDiffReview] : []), ...state.diffReviewQueue];
function reviewInMain(request: PendingApproval | PendingAskUser | PendingDiffReview, t: Translate): Action {
  return { id: JSON.stringify([request.requestId, request.conversationId, request.turnId, request.messageId, "open"]), label: t("审阅"), openInMainWindow: true,
    submit: async () => { throw new Error("This request must be reviewed in its original task window."); } };
}

function canAnswerCompactly(ask: PendingAskUser) {
  return !ask.secret && ask.promptType !== "secret" && ask.promptType !== "manual_code" && !ask.provider && !ask.planReview && !ask.inputSchema && !ask.elicitation
    && (!ask.requestUserInput || ask.requestUserInput.questions.length === 1 && !ask.requestUserInput.questions[0].isSecret);
}

export function petTaskActions(state: AppStore, conversationId: string, t: Translate): Action[] {
  const approval = approvalRequests(state).find(request => request.conversationId === conversationId);
  const diff = diffRequests(state).find(request => request.conversationId === conversationId);
  const ask = askRequests(state).find(request => request.conversationId === conversationId);
  const actions: Action[] = [];
  const add = (requestId: string, decision: string, label: string, command: ClientCommand, accepted: () => void) => {
    const id = JSON.stringify([requestId, command, decision, diff?.diff, ask?.requestUserInput?.questions ?? ask?.question]);
    actions.push({ id, label, submit: async () => {
      const result = await sendPromptResponseCommand(command);
      if (!commandResultSucceeded(result)) throw new Error(result.message || t("审批未被后端接受"));
      if (petTaskActions(useAppStore.getState(), conversationId, t).some(action => action.id === id)) accepted();
    } });
  };
  if (approval) {
    if (approval.status === "submitted") return [];
    const accepted = () => useAppStore.getState().clearApproval(approval.requestId);
    if (approval.mcpApproval) return [reviewInMain(approval, t)];
    if (approval.permissionRequest) {
      add(approval.requestId, "turn", t("允许一次"), buildPermissionsResponseCommand(approval.requestId, approval.permissionRequest.permissions, "turn", owner(approval)), accepted);
      add(approval.requestId, "deny", t("Mini 拒绝"), buildPermissionsResponseCommand(approval.requestId, {}, "turn", owner(approval)), accepted);
    } else if (approval.networkApprovalContext && approval.availableDecisions) {
      for (const decision of approval.availableDecisions) {
        if (decision !== "approved" && decision !== "abort") continue;
        const label = t(decision === "approved" ? "允许一次" : "Mini 拒绝");
        add(approval.requestId, JSON.stringify(decision), label, buildNetworkApprovalResponseCommand(approval.requestId, decision,
          { requestDigest: approval.requestDigest, owner: owner(approval) }), accepted);
      }
    } else {
      const options = { requestDigest: approval.requestDigest, owner: owner(approval) };
      if (approval.availableDecisions) {
        for (const decision of approval.availableDecisions) {
          if (decision === "approved" || decision === "abort") add(approval.requestId, decision, t(decision === "approved" ? "允许一次" : "Mini 拒绝"),
            buildApprovalResponseCommand(approval.requestId, decision === "approved" ? "approve" : "reject", { ...options, sourceCommandDecision: decision }), accepted);
        }
      } else {
        add(approval.requestId, "allow", t("允许一次"), buildApprovalResponseCommand(approval.requestId, "approve", options), accepted);
        add(approval.requestId, "deny", t("Mini 拒绝"), buildApprovalResponseCommand(approval.requestId, "reject", options), accepted);
      }
    }
  } else if (diff) {
    if (diff.reviewState?.status !== "submitted") add(diff.requestId, "approve", t("应用"),
      buildApprovalResponseCommand(diff.requestId, "approve", { owner: owner(diff) }), () => useAppStore.getState().clearDiffReview(diff.requestId));
    actions.push(reviewInMain(diff, t));
  } else if (ask && canAnswerCompactly(ask)) {
    const accepted = () => useAppStore.getState().clearAskUser(ask.requestId, ask.conversationId,
      { turnId: ask.turnId, messageId: ask.messageId, taskId: ask.taskId });
    if (ask.requestUserInput?.questions.length === 1) {
      const question = ask.requestUserInput.questions[0];
      if (!question.isSecret) for (const option of question.options ?? []) add(ask.requestId, option.label, option.label,
        buildRequestUserInputResponseCommand(ask.requestId, { [question.id]: { answers: [option.label] } }, owner(ask)), accepted);
    } else if (ask.options) {
      for (const option of ask.options) add(ask.requestId, option.value, option.label,
        buildAskUserResponseCommand(ask.requestId, option.value, owner(ask)), accepted);
    }
  }
  if (ask && actions.length === 0) actions.push(reviewInMain(ask, t));
  if (!approval && !diff && !ask) {
    const interrupt = buildInterruptCommand(state, conversationId);
    if (hasInterruptFence(interrupt)) actions.push({ id: JSON.stringify(interrupt), label: t("停止"), submit: async () => {
      const result = await sendClientCommandAwaitResult(interrupt, "interrupt");
      if (!commandResultSucceeded(result)) throw new Error(result.message);
    } });
  }
  return actions;
}

export async function submitPetFollowup(conversationId: string, prompt: string, t: Translate) {
  const state = useAppStore.getState();
  const task = desktopPetActivity(state, t).tasks.find(task => task.id === conversationId);
  if (!task?.followUpAllowed) throw new Error("This task cannot accept a follow-up right now.");
  const accepted = await sendChatMessage({ displayContent: prompt, backendContent: prompt, conversationId,
    allowWhileStreaming: true, busyBehavior: state.followUpBehavior, awaitAdmission: true });
  if (!accepted) throw new Error("The follow-up was not accepted by the task.");
}

export function desktopPetActivity(state: AppStore, t: Translate): DesktopPetActivity {
  const statuses = new Map<string, DesktopPetTask["status"]>();
  // MiniCode's parentConversationId also describes user-created forks. The
  // exclusion here targets delegated/side chats, not those forks.
  const excluded = new Set(state.conversations.filter(conversation => conversation.archived || conversation.conversationType === "side_chat" || conversation.threadSource === "subagent").map(conversation => conversation.id));
  for (const id of Object.keys(state.sideChats)) excluded.add(id);
  for (const conversation of state.conversations) {
    if (conversation.archived) continue;
    if (conversation.sessionStatus === "running" || state.conversationStreaming[conversation.id]) statuses.set(conversation.id, "running");
    if (conversation.hasUnreadTurn && conversation.sessionStatus !== "running") statuses.set(conversation.id, "review");
    if (conversation.sessionStatus === "waiting") statuses.set(conversation.id, "waiting");
  }
  for (const [id, running] of Object.entries(state.conversationStreaming)) if (running) statuses.set(id, "running");
  if (state.isStreaming && state.conversationId) statuses.set(state.conversationId, "running");
  for (const conversation of state.conversations) {
    const messages = conversation.id === state.conversationId ? state.messages : state.conversationMessages[conversation.id] ?? [];
    const latest = [...messages].reverse().find(message => message.role === "assistant");
    if (latest?.terminalStatus === "failed" && !state.conversationStreaming[conversation.id] && !(conversation.id === state.conversationId && state.isStreaming)) statuses.set(conversation.id, "failed");
  }
  for (const request of diffRequests(state)) if (request.conversationId) statuses.set(request.conversationId, "waiting");
  for (const request of [...approvalRequests(state), ...askRequests(state)]) if (request.conversationId) statuses.set(request.conversationId, "waiting");
  const tasks: DesktopPetTask[] = [...statuses].filter(([id, status]) => {
    if (excluded.has(id)) return false;
    const conversation = state.conversations.find(conversation => conversation.id === id);
    if (conversation?.threadSource === "realtime_voice" && status !== "waiting" && status !== "failed") return false;
    if (status === "waiting" || status === "running") return true;
    const completion = petCompletionNotification(id);
    if (!completion) return true;
    const messages = id === state.conversationId ? state.messages : state.conversationMessages[id] ?? [];
    const latest = [...messages].reverse().find(message => message.role === "assistant");
    if (latest && (completion.identity.messageId && completion.identity.messageId !== latest.id
      || completion.identity.turnId && latest.turnId && completion.identity.turnId !== latest.turnId)) return true;
    return completion.notification.body !== null && !completion.notification.has_pending_continuation;
  }).map(([id, status]) => {
    const conversation = state.conversations.find(conversation => conversation.id === id);
    const messages = id === state.conversationId ? state.messages : state.conversationMessages[id] ?? [];
    const latest = [...messages].reverse().find(message => message.role === "assistant");
    const approval = approvalRequests(state).find(request => request.conversationId === id);
    const ask = askRequests(state).find(request => request.conversationId === id);
    const diff = diffRequests(state).find(request => request.conversationId === id);
    const request = approval || ask || diff;
    const updatedAt = latest?.completedAt ?? (conversation ? Date.parse(conversation.updatedAt) : latest?.timestamp);
    return { id,
    title: state.conversations.find(conversation => conversation.id === id)?.title || id, status,
    turnKey: request?.turnId || request?.requestId || latest?.turnId || latest?.id,
    updatedAt, sortAt: latest?.runStartedAt ?? updatedAt,
    detail: approval?.displayCommand || approval?.approvalReason || ask?.question || diff?.filePath,
    actions: petTaskActions(state, id, t).map(({ id, label, openInMainWindow }) => ({ id, label, ...(openInMainWindow ? { openInMainWindow } : {}) })),
    followUpAllowed: state.isConnected && status !== "waiting" && !diffRequests(state).some(request => request.conversationId === id),
  }; });
  return { tasks, running: tasks.some(task => task.status === "running"), waiting: tasks.some(task => task.status === "waiting"),
    review: tasks.some(task => task.status === "review"), failed: tasks.some(task => task.status === "failed") };
}
