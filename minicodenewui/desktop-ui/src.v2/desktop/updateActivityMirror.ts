import type { AppStore } from "../stores/types";
import { useAppStore } from "../stores";
import { desktop, type UpdateActivitySnapshot } from "./runtime";
import { dirtyEditorFiles } from "../stores/shared-helpers";
import { collectComposerDrafts, composerDraftAttachments, composerWindowOwnerId, selectComposerDraftInputs, composerDraftInputsEqual } from "../stores/composer-drafts";
import { hasPendingEditorDrafts } from "../stores/editor-drafts";

const sortedUnique = (values: Array<string | null | undefined>): string[] => (
  [...new Set(values.map((value) => value?.trim() || "").filter(Boolean))].sort()
);

export const buildUpdateActivitySnapshot = (state: AppStore): UpdateActivitySnapshot => {
  const sideChatIds = new Set(Object.keys(state.sideChats));
  const activeTurns = Object.entries(state.conversationStreaming)
    .filter(([, streaming]) => streaming)
    .map(([conversationId]) => conversationId);
  if (state.conversationId && state.isStreaming && !sideChatIds.has(state.conversationId)) {
    activeTurns.push(state.conversationId);
  }
  const runtimeStreamIds = state.runtimeSession?.active_stream_conversation_ids ?? [];
  activeTurns.push(...runtimeStreamIds);
  if (state.runtimeSession?.active_task_id && runtimeStreamIds.length === 0) {
    activeTurns.push(
      state.runtimeSession.active_conversation_id
      || state.runtimeSession.active_task_id,
    );
  }

  const promptIds = [
    state.pendingApproval?.requestId,
    ...state.approvalQueue.map((item) => item.requestId),
    state.pendingDiffReview?.requestId,
    ...state.diffReviewQueue.map((item) => item.requestId),
    state.pendingAskUser?.requestId,
    ...state.askUserQueue.map((item) => item.requestId),
    ...(state.runtimeSession?.pending_approvals ?? []).map((item) => item.request_id),
  ];
  const runtimePendingApprovalCount = Number(state.runtimeSession?.pending_approval_count || 0);
  if (runtimePendingApprovalCount > (state.runtimeSession?.pending_approvals?.length ?? 0)) {
    promptIds.push(`runtime-pending-approvals:${runtimePendingApprovalCount}`);
  }
  const allAttachments = [
    ...composerDraftAttachments(state),
    ...Object.values(state.sideChats).flatMap((thread) => thread.attachments ?? []),
    ...Object.values(state.conversationWorkbenchStates)
      .flatMap(composerDraftAttachments),
    ...Object.values(state.workspaceComposerDrafts).flatMap(composerDraftAttachments),
  ];
  const runtimeTaskIds = (state.runtimeSession?.running_tasks ?? []).map((item, index) => {
    if (typeof item === "string") return item;
    if (!item || typeof item !== "object") return `runtime-task:${index}`;
    const record = item as Record<string, unknown>;
    return [record.id, record.task_id, record.command_id]
      .find((value) => typeof value === "string" && value.trim()) as string | undefined
      ?? `runtime-task:${index}`;
  });

  return {
    runtimeReady: state.isConnected && state.runtimeSession !== null,
    activeTurns: sortedUnique(activeTurns),
    sideChatStreams: sortedUnique(
      [
        ...Object.values(state.sideChats)
          .filter((thread) => thread.isStreaming)
          .map((thread) => thread.id),
        ...Object.entries(state.conversationStreaming)
          .filter(([conversationId, streaming]) => streaming && sideChatIds.has(conversationId))
          .map(([conversationId]) => conversationId),
        ...runtimeStreamIds.filter((conversationId) => sideChatIds.has(conversationId)),
      ],
    ),
    pendingPrompts: sortedUnique(promptIds),
    uploadingAttachments: sortedUnique(
      allAttachments
        .filter((attachment) => attachment.status === "uploading")
        .map((attachment) => attachment.id),
    ),
    dirtyEditors: sortedUnique(dirtyEditorFiles(state)),
    backgroundTasks: sortedUnique(
      [
        ...state.backgroundTasks
          .filter((task) => task.status === "running" || task.status === "stalled" || task.cleanupPending)
          .map((task) => task.id),
        ...runtimeTaskIds,
        ...(state.runtimeSession?.active_subagents ?? []).map(agent => agent.id),
        ...(state.runtimeSession?.queued_user_messages ?? []).map(input => input.message_id),
        ...(state.runtimeSession?.pending_turn_inputs ?? []).map(input => input.message_id),
        ...[...state.subagents, ...Object.values(state.conversationAgentStates).flatMap(frame => frame.subagents)]
          .filter(agent => ["pending", "running", "blocked"].includes(agent.status)).map(agent => agent.id),
      ],
    ),
    pendingDrafts: [
      ...(!state.composerDraftsReady ? [`${composerWindowOwnerId()}:recovery`] : []),
      ...(state.composerDraftStoragePending ? [`${composerWindowOwnerId()}:commit`] : []),
      ...(state.composerDraftStorageError ? [`${composerWindowOwnerId()}:storage-error`] : []),
      ...(hasPendingEditorDrafts() ? [`${composerWindowOwnerId()}:editor-commit`] : []),
      ...(state.editorTabs.some(tab => tab.recoveryPending || tab.draftRestorePending) ? [`${composerWindowOwnerId()}:editor-recovery`] : []),
    ],
    draftStates: [
      `${composerWindowOwnerId()}:revision:${state.composerDraftRevision}`,
      ...collectComposerDrafts(state, state.composerDraftRevision).records.keys(),
    ].sort(),
  };
};

let stopUpdateActivityMirror: (() => void) | null = null;

/**
 * Track authoring and activity facts rather than side-chat text or subagent
 * progress. Those streams replace their owning objects without changing the
 * update/close blockers or stored drafts.
 */
const selectUpdateActivityInputs = (state: AppStore) => ({
  composerDrafts: selectComposerDraftInputs(state),
  isConnected: state.isConnected,
  runtimeSession: state.runtimeSession,
  conversationId: state.conversationId,
  isStreaming: state.isStreaming,
  conversationStreaming: state.conversationStreaming,
  sideChats: state.sideChats,
  pendingApproval: state.pendingApproval,
  approvalQueue: state.approvalQueue,
  pendingDiffReview: state.pendingDiffReview,
  diffReviewQueue: state.diffReviewQueue,
  pendingAskUser: state.pendingAskUser,
  askUserQueue: state.askUserQueue,
  editorTabs: state.editorTabs,
  backgroundTasks: state.backgroundTasks,
  subagents: state.subagents,
  conversationAgentStates: state.conversationAgentStates,
  composerDraftsReady: state.composerDraftsReady,
  composerDraftStoragePending: state.composerDraftStoragePending,
  composerDraftStorageError: state.composerDraftStorageError,
  composerDraftRevision: state.composerDraftRevision,
});

type UpdateActivityInputs = ReturnType<typeof selectUpdateActivityInputs>;

const updateActivityInputsEqual = (
  left: UpdateActivityInputs,
  right: UpdateActivityInputs,
): boolean => (Object.keys(left) as (keyof UpdateActivityInputs)[]).every(key => {
  if (key === "composerDrafts") return composerDraftInputsEqual(left.composerDrafts, right.composerDrafts);
  if (key === "sideChats") return left.sideChats === right.sideChats || Object.keys(left.sideChats).length === Object.keys(right.sideChats).length
    && Object.keys(left.sideChats).every(id => right.sideChats[id] && left.sideChats[id].id === right.sideChats[id].id
      && left.sideChats[id].isStreaming === right.sideChats[id].isStreaming);
  if (key === "subagents") return sameAgentActivity(left.subagents, right.subagents);
  if (key === "conversationAgentStates") return left.conversationAgentStates === right.conversationAgentStates
    || Object.keys(left.conversationAgentStates).length === Object.keys(right.conversationAgentStates).length
      && Object.keys(left.conversationAgentStates).every(id => right.conversationAgentStates[id]
        && sameAgentActivity(left.conversationAgentStates[id].subagents, right.conversationAgentStates[id].subagents));
  return left[key] === right[key];
});

const activeAgent = (status: string) => status === "pending" || status === "running" || status === "blocked";
const sameAgentActivity = (left: AppStore["subagents"], right: AppStore["subagents"]) => left === right
  || left.length === right.length && left.every((agent, index) => agent.id === right[index].id && activeAgent(agent.status) === activeAgent(right[index].status));

export const startUpdateActivityMirror = (): (() => void) => {
  if (stopUpdateActivityMirror) return stopUpdateActivityMirror;
  const updates = desktop()?.updates;
  if (!updates?.reportActivity) return () => {};

  let desiredSnapshot = buildUpdateActivitySnapshot(useAppStore.getState());
  let desiredSerialized = JSON.stringify(desiredSnapshot);
  let lastAcknowledgedSnapshot = "";
  const pendingRequestIds = new Set<string>();
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  let heartbeatPending = true;
  let sending = false;
  let active = true;
  const scheduleRetry = (flush: () => void) => {
    if (!active || retryTimer) return;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      flush();
    }, 1000);
  };
  const flush = () => {
    if (!active || sending) return;
    const requestIds = [...pendingRequestIds].slice(0, 20);
    const shouldSend = heartbeatPending
      || requestIds.length > 0
      || desiredSerialized !== lastAcknowledgedSnapshot;
    if (!shouldSend) return;
    for (const requestId of requestIds) pendingRequestIds.delete(requestId);
    const snapshot = desiredSnapshot;
    const serialized = desiredSerialized;
    heartbeatPending = false;
    sending = true;
    let failed = false;
    void updates.reportActivity(snapshot, requestIds)
      .then((result) => {
        if (result.accepted) {
          lastAcknowledgedSnapshot = serialized;
        } else {
          failed = true;
          for (const requestId of requestIds) pendingRequestIds.add(requestId);
          heartbeatPending = true;
          scheduleRetry(flush);
        }
      })
      .catch(() => {
        failed = true;
        for (const requestId of requestIds) pendingRequestIds.add(requestId);
        heartbeatPending = true;
        scheduleRetry(flush);
      })
      .finally(() => {
        sending = false;
        if (failed) return;
        if (
          desiredSerialized !== lastAcknowledgedSnapshot
          || pendingRequestIds.size > 0
          || heartbeatPending
        ) {
          flush();
        }
      });
  };
  const publish = (state: AppStore, heartbeat = false) => {
    desiredSnapshot = buildUpdateActivitySnapshot(state);
    desiredSerialized = JSON.stringify(desiredSnapshot);
    if (heartbeat) heartbeatPending = true;
    flush();
  };

  publish(useAppStore.getState(), true);
  const unsubscribe = useAppStore.subscribe(
    selectUpdateActivityInputs,
    () => publish(useAppStore.getState()),
    { equalityFn: updateActivityInputsEqual },
  );
  const unsubscribeRequest = updates.onActivityRequest?.((payload) => {
    const requestId = payload?.requestId?.trim();
    if (!requestId) return;
    pendingRequestIds.add(requestId);
    publish(useAppStore.getState());
  });
  heartbeatTimer = setInterval(() => publish(useAppStore.getState(), true), 5000);
  stopUpdateActivityMirror = () => {
    active = false;
    if (retryTimer) clearTimeout(retryTimer);
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    unsubscribeRequest?.();
    unsubscribe();
    stopUpdateActivityMirror = null;
  };
  return stopUpdateActivityMirror;
};
