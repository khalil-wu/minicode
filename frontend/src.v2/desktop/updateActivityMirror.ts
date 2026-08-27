import type { AppStore } from "../stores/types";
import { useAppStore } from "../stores";
import { desktop, type UpdateActivitySnapshot } from "./runtime";

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
    ...state.attachments,
    ...Object.values(state.conversationWorkbenchStates)
      .flatMap((workbench) => workbench.attachments ?? []),
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
    dirtyEditors: sortedUnique(
      state.editorTabs
        .filter((tab) => !tab.readOnly && tab.content !== tab.original)
        .map((tab) => tab.path),
    ),
    backgroundTasks: sortedUnique(
      [
        ...state.backgroundTasks
          .filter((task) => task.status === "running" || task.status === "stalled")
          .map((task) => task.id),
        ...runtimeTaskIds,
      ],
    ),
  };
};

let stopUpdateActivityMirror: (() => void) | null = null;

/**
 * Exactly the slices `buildUpdateActivitySnapshot` reads. Subscribing to the
 * whole store ran the snapshot build (flat-mapping every workbench's
 * attachments, string-comparing every open editor tab) plus a JSON.stringify on
 * every streaming token. All of these are replaced immutably, so reference
 * equality is a sound and very cheap gate.
 */
const selectUpdateActivityInputs = (state: AppStore) => ({
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
  attachments: state.attachments,
  conversationWorkbenchStates: state.conversationWorkbenchStates,
  editorTabs: state.editorTabs,
  backgroundTasks: state.backgroundTasks,
});

type UpdateActivityInputs = ReturnType<typeof selectUpdateActivityInputs>;

const updateActivityInputsEqual = (
  left: UpdateActivityInputs,
  right: UpdateActivityInputs,
): boolean => (Object.keys(left) as (keyof UpdateActivityInputs)[]).every(
  (key) => left[key] === right[key],
);

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
