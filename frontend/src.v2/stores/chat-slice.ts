import type { StateCreator } from "zustand";
import type {
  AppStore,
  ChatSlice,
  ContentBlock,
  ConversationGoal,
  ConnectionPhase,
  MessageContextRef,
  MessageAttachmentRef,
  ProgressContentBlock,
  SkillContextRef,
} from "./types";
import { canonicalWorkspacePath } from "../lib/workspace-display";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { promptCacheEffectivePromptTokens } from "../chat/cacheUsage";
import { desktop } from "../desktop/runtime";
import { toBackendPermissionMode } from "../protocol/permissions";
import {
  commandResultSucceeded,
  sendClientCommand,
  sendClientCommandAwaitResult,
  sendConversationDeleteCommand,
} from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import {
  getAnswerTextFromBlocks,
  getContentBlocks,
  getThinkingFromMessage,
  getToolCallsFromMessage,
  stripLegacyContentFields,
} from "../lib/content-blocks";
import {
  uniqueMessageId,
  newConversationId,
  isStructurallyEmptyAssistantMessage,
  cacheMessagesForConversation,
  updateMessagesForConversation,
  findLastStreamingIndex,
  computeToolCallCount,
  normalizeThinkingMetadata,
  thinkingMetadataMatches,
  mergeResumeToolCalls,
  conversationWorkspacePath,
  editorStateForWorkspace,
  conversationResetPayload,
  visibleDiffReviewForConversation,
  LS,
  writeLS,
} from "./shared-helpers";
import {
  isTerminalToolCallStatus,
  mergeToolCallResultRecord,
  type ToolCallRecord,
} from "../lib/tool-call-reducer";
import { providerProgressLifecycleRegressed } from "../lib/provider-progress";
import { isTransientProviderReasoning } from "../lib/provider-reasoning";

function stripDisplayContextSuffix(content: string, refs: MessageContextRef[]): string {
  const suffixItems = refs
    .filter((ref) => ref.kind !== "skill")
    .map((ref) => `@${ref.name}`)
    .filter((name) => name.length > 1);
  if (suffixItems.length === 0) return content;
  const suffix = ` ${suffixItems.join(" ")}`;
  return content.endsWith(suffix) ? content.slice(0, -suffix.length).trimEnd() : content;
}

function findAgentMessageBlockIndex(blocks: ContentBlock[], itemId: string): number {
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (block.type === "text" && block.itemId === itemId) return index;
  }
  return -1;
}

const settleThinkingBlocks = (blocks: ContentBlock[]): ContentBlock[] =>
  blocks.filter((block) =>
    block.type !== "thinking" || !isTransientProviderReasoning(block),
  );

function mergeToolCallIds(left: string[] | undefined, right: string[] | undefined): string[] | undefined {
  const ids = [...(left || []), ...(right || [])].filter(Boolean);
  if (ids.length === 0) return undefined;
  return Array.from(new Set(ids));
}

function applyRecallTruncation(
  conversationId: string,
  incoming: AppStore["messages"],
  state: AppStore,
): AppStore["messages"] {
  const truncation = (state.conversationRecallTruncations ?? {})[conversationId];
  if (!truncation || truncation.removedIds.length === 0) return incoming;
  const removed = new Set(truncation.removedIds);
  const firstRemovedIndex = incoming.findIndex((message) => removed.has(message.id));
  if (firstRemovedIndex < 0) return incoming;
  const truncated = incoming.slice(0, firstRemovedIndex);
  const cached = state.conversationMessages[conversationId] ?? (
    state.conversationId === conversationId ? state.messages : []
  );
  const cachedHasRemoved = cached.some((message) => removed.has(message.id));
  const cachedStartsWithTruncated = truncated.every((message, index) => cached[index]?.id === message.id);
  if (!cachedHasRemoved && cached.length > truncated.length && cachedStartsWithTruncated) {
    return cached;
  }
  return truncated;
}

function persistActiveConversationId(conversationId: string | null | undefined) {
  writeLS(LS.conversation.activeId, conversationId || "");
}

function completeRunWorkState(state: AppStore, terminalStatus: "completed" | "partial" | "failed" | "interrupted") {
  if (terminalStatus === "failed" || terminalStatus === "interrupted" || terminalStatus === "partial") {
    const unfinishedLabel = terminalStatus === "partial" ? "运行部分完成" : "运行已停止";
    return {
      plan: state.plan,
      todos: state.todos.map((todo) =>
        todo.status === "in_progress"
          ? { ...todo, status: "blocked" as const, activeForm: todo.activeForm || unfinishedLabel }
          : todo,
      ),
    };
  }

  // Turn plan updates are the canonical writer of checklist state. A finished
  // run must not silently mark unfinished work completed.
  return {
    plan: state.plan,
    todos: state.todos,
  };
}

const PROVIDER_PROGRESS_TERMINAL_STATUSES = new Set(["partial", "completed", "failed"]);

const maxProgressNumber = (left: number | undefined, right: number | undefined): number | undefined => {
  if (left === undefined) return right;
  if (right === undefined) return left;
  return Math.max(left, right);
};

const isProviderProgressId = (id: string): boolean => id.startsWith("provider:");

const isProviderRetryProgress = (
  progress: Pick<ProgressContentBlock, "id" | "retryAttempt" | "maxRetries" | "providerState">,
  previous?: Pick<ProgressContentBlock, "retryAttempt" | "maxRetries" | "providerState">,
): boolean => isProviderProgressId(progress.id) && (
  typeof progress.retryAttempt === "number"
  || typeof progress.maxRetries === "number"
  || Boolean(progress.providerState)
  || typeof previous?.retryAttempt === "number"
  || typeof previous?.maxRetries === "number"
  || Boolean(previous?.providerState)
);

const mergeProviderMessageProgress = (
  previous: ProgressContentBlock,
  incoming: ProgressContentBlock,
): ProgressContentBlock => {
  const lifecycleRegressed = providerProgressLifecycleRegressed(previous, incoming);
  const next: ProgressContentBlock = lifecycleRegressed
    ? { ...previous }
    : {
        ...previous,
        ...incoming,
        // Keep the provider retry ladder anchored to its first frame. The
        // block timestamp is used as its start time by timeline projections.
        timestamp: previous.timestamp,
      };

  const terminalFence = lifecycleRegressed
    && PROVIDER_PROGRESS_TERMINAL_STATUSES.has(previous.status);
  if (!terminalFence) {
    const retryAttempt = maxProgressNumber(previous.retryAttempt, incoming.retryAttempt);
    const maxRetries = maxProgressNumber(previous.maxRetries, incoming.maxRetries);
    const count = maxProgressNumber(previous.count, incoming.count);
    if (retryAttempt !== undefined) next.retryAttempt = retryAttempt;
    if (maxRetries !== undefined) next.maxRetries = maxRetries;
    if (count !== undefined) next.count = count;
  }

  if (!lifecycleRegressed) {
    const retryProgress = isProviderRetryProgress(incoming, previous);
    const detail = String(incoming.detail ?? "").trim();
    if (retryProgress) {
      if (detail) next.detail = detail;
      else delete next.detail;
    }
  }

  if (next.status !== "running") delete next.ephemeral;
  return next;
};

const pendingProviderProgressKey = (conversationId: string, messageId: string): string =>
  `${conversationId}\u0000${messageId}`;

const assistantIsTerminal = (message: AppStore["messages"][number]): boolean =>
  Boolean(message.terminalStatus)
  || Boolean(message.completedAt)
  || (!message.isStreaming && !message.isThinkingStreaming);

/** Merge one already-normalized progress block into its exact assistant. */
const mergeProgressBlockIntoAssistant = (
  message: AppStore["messages"][number],
  incoming: ProgressContentBlock,
): AppStore["messages"][number] => {
  const provider = isProviderProgressId(incoming.id);
  if (provider && assistantIsTerminal(message)) return message;

  const blocks = getContentBlocks(message).slice();
  const existingIdx = blocks.findIndex((block) =>
    block.type === "progress" && block.id === incoming.id,
  );
  if (existingIdx >= 0) {
    const existing = blocks[existingIdx];
    if (existing.type === "progress") {
      if (provider) {
        blocks[existingIdx] = mergeProviderMessageProgress(existing, incoming);
      } else {
        const terminal = new Set(["completed", "failed", "partial"]);
        if (terminal.has(existing.status) && incoming.status === "running") return message;
        blocks[existingIdx] = { ...existing, ...incoming };
      }
    } else {
      blocks[existingIdx] = incoming;
    }
  } else {
    blocks.push(incoming);
  }
  return { ...message, blocks };
};

/**
 * Answer text that arrived with no live assistant record to attach it to is
 * real model output. Returning `null` from the reducer keeps the store
 * consistent, but dropping the text with no trace makes the loss
 * unverifiable — a whole final answer can disappear from the transcript with
 * nothing anywhere to explain it. Record every drop in the Inspector, keyed per
 * (conversation, item) so a long run of dropped deltas collapses into one
 * running total instead of flooding the list.
 */
function recordDroppedAnswerText(
  store: AppStore,
  reason: "agent_message_delta" | "agent_message_completed",
  text: string,
  itemId: string,
  conversationId?: string,
  messageId?: string,
) {
  const owner = conversationId?.trim() || store.conversationId || "session";
  const targetId = `dropped-answer:${owner}:${itemId || "item"}`;
  const previous = store.inspectorEntries.find((entry) =>
    entry.targetKind === "message" && entry.targetId === targetId,
  );
  const previousEvents = typeof previous?.payload.dropped_events === "number"
    ? previous.payload.dropped_events
    : 0;
  const previousCharacters = typeof previous?.payload.dropped_characters === "number"
    ? previous.payload.dropped_characters
    : 0;
  store.addInspectorEntry({
    targetKind: "message",
    targetId,
    payload: {
      dropped: true,
      reason,
      detail: "没有正在流式输出的助手消息，文本无法归属",
      conversation_id: conversationId?.trim() || store.conversationId || undefined,
      message_id: messageId?.trim() || undefined,
      item_id: itemId || undefined,
      dropped_events: previousEvents + 1,
      dropped_characters: previousCharacters + text.length,
      text_preview: text.slice(0, 2000),
    },
    timestamp: Date.now(),
  });
}

function findStreamingIndexForMessage(messages: AppStore["messages"], messageId?: string): number {
  const targetId = messageId?.trim();
  if (!targetId) return findLastStreamingIndex(messages);
  // Live assistant records are appended near the tail. Searching from the
  // beginning turned every text/thinking delta into a full-history walk on a
  // long task, even though the target is normally the final message.
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (
      message.role === "assistant"
      && Boolean(message.isStreaming)
      && message.id === targetId
    ) return index;
  }
  return -1;
}

export const createChatSlice: StateCreator<AppStore, [], [], ChatSlice> = (set, get) => ({
  conversationId: null,
  conversations: [],
  conversationInventoryInstanceId: null,
  conversationInventoryRevision: 0,
  activeGoal: null,
  messages: [],
  conversationMessages: {},
  conversationStreaming: {},
  pendingProviderProgress: {},
  conversationRecallTruncations: {},
  isStreaming: false,
  isPaused: false,
  isConnected: false,
  connectionPhase: "connecting",
  reconnectAttempt: 0,
  reconnectMaxAttempts: null,
  connectionError: null,
  lastUsage: null,
  usageTotals: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, reasoning: 0, turns: 0 },
  sideChats: {},
  toolCallCount: 0,
  sendMessage: (content: string, options?: { assistant?: boolean; contextRefs?: MessageContextRef[]; attachmentRefs?: MessageAttachmentRef[] }) => {
    const id = uniqueMessageId();
    const includeAssistant = options?.assistant !== false;
    set((s) => {
      const nextMessages = [
        ...s.messages,
        {
          id,
          role: "user" as const,
          content,
          contextRefs: options?.contextRefs ?? [],
          attachmentRefs: options?.attachmentRefs ?? [],
          artifacts: [],
          timestamp: Date.now(),
        },
        ...(includeAssistant
          ? [{
              id: uniqueMessageId("a"),
              role: "assistant" as const,
              content: "",
              blocks: [],
              artifacts: [],
              timestamp: Date.now(),
              isStreaming: true,
            }]
          : []),
      ];
      const nextStreaming = includeAssistant || s.isStreaming;
      return {
        messages: nextMessages,
        isStreaming: nextStreaming,
        ...cacheMessagesForConversation(s, s.conversationId, nextMessages, nextStreaming),
      };
    });
  },
  upsertSystemMessage: (id, content, options) =>
    set((s) => {
      const targetId = options?.conversationId || s.conversationId || undefined;
      const isActive = !targetId || targetId === s.conversationId;
      const sourceMessages = targetId && !isActive
        ? s.conversationMessages[targetId] ?? []
        : s.messages;
      const existingIdx = sourceMessages.findIndex((message) =>
        message.id === id ||
        (options?.replacePrefix && message.role === "system" && message.content.startsWith(options.replacePrefix))
      );
      const nextMessages = sourceMessages.slice();
      const nextMessage = {
        id,
        role: "system" as const,
        content,
        artifacts: [],
        timestamp: Date.now(),
      };
      if (existingIdx >= 0) {
        nextMessages[existingIdx] = { ...nextMessages[existingIdx], ...nextMessage };
      } else {
        nextMessages.push(nextMessage);
      }
      if (targetId && !isActive) {
        return {
          conversationMessages: {
            ...s.conversationMessages,
            [targetId]: nextMessages,
          },
        };
      }
      return {
        messages: nextMessages,
        ...cacheMessagesForConversation(s, s.conversationId, nextMessages, s.isStreaming),
      };
    }),
  recallMessage: async (id) => {
    const current = get();
    const currentIndex = current.messages.findIndex((message) => message.id === id);
    if (currentIndex < 0) return false;
    const currentTarget = current.messages[currentIndex];
    const currentTurnStart = currentTarget.role === "user"
      ? currentIndex
      : (() => {
          for (let i = currentIndex; i >= 0; i -= 1) {
            if (current.messages[i]?.role === "user") return i;
          }
          return currentIndex;
        })();
    const currentRecalled = current.messages[currentTurnStart] ?? currentTarget;
    const conversationId = current.conversationId || "";
    if (conversationId && currentRecalled.id) {
      try {
        const result = await sendClientCommandAwaitResult({
          type: "conversation.truncate",
          conversation_id: conversationId,
          truncate_before_message_id: currentRecalled.id,
          retained_message_ids: current.messages.slice(0, currentTurnStart).map((message) => message.id),
        }, "conversation.truncate");
        if (!commandResultSucceeded(result)) {
          pushToast(result.message || "Unable to recall this turn.", "error", 4000);
          return false;
        }
      } catch {
        return false;
      }
    }
    let restored = false;
    set((s) => {
      // The truncate response may arrive after the user switches chats. Never
      // restore a durable attachment handle into a different conversation.
      if (String(s.conversationId || "") !== conversationId) return s;
      const index = s.messages.findIndex((message) => message.id === id);
      if (index < 0) return s;
      const target = s.messages[index];
      const turnStart =
        target.role === "user"
          ? index
          : (() => {
              for (let i = index; i >= 0; i -= 1) {
                if (s.messages[i]?.role === "user") return i;
              }
              return index;
            })();
      const recalled = s.messages[turnStart] ?? target;
      const nextMessages = s.messages.slice(0, turnStart);
      const contextRefs = recalled.contextRefs ?? target.contextRefs ?? [];
      const mentionRefs = contextRefs.filter(
        (ref): ref is Exclude<MessageContextRef, SkillContextRef> => ref.kind !== "skill",
      );
      const skillRefs: SkillContextRef[] = contextRefs.filter((ref): ref is SkillContextRef => ref.kind === "skill");
      const attachmentRefs = recalled.attachmentRefs ?? target.attachmentRefs ?? [];
      const restoredAt = Date.now().toString(36);
      const restoredAttachments = attachmentRefs.map((attachment, attachmentIndex) => {
        const artifactId = String(attachment.artifactId || "").trim();
        const canReuseDurableAttachment = Boolean(conversationId && artifactId);
        return {
          id: `att-recall-${artifactId || attachment.id}-${restoredAt}-${attachmentIndex}`,
          name: attachment.name,
          type: attachment.mediaType || "application/octet-stream",
          size: attachment.sizeBytes || 0,
          status: canReuseDurableAttachment ? "ready" as const : "error" as const,
          ...(conversationId ? { conversationId } : {}),
          ...(artifactId ? { artifactId } : {}),
          docId: attachment.docId,
          inputSource: attachment.inputSource,
          sourceCharCount: attachment.sourceCharCount,
          ...(!canReuseDurableAttachment
            ? { error: "原附件缺少可验证的持久化引用，请重新上传。" }
            : {
                attachment: {
                  id: attachment.id,
                  kind: attachment.kind,
                  file_name: attachment.name,
                  media_type: attachment.mediaType,
                  artifact_id: artifactId,
                  doc_id: attachment.docId,
                  size_bytes: attachment.sizeBytes ?? 0,
                  ...(attachment.inputSource ? { input_source: attachment.inputSource } : {}),
                  ...(attachment.sourceCharCount ? { source_char_count: attachment.sourceCharCount } : {}),
                },
              }),
        };
      });
      const removedMessages = s.messages.slice(turnStart);
      const nextStreaming = removedMessages.some((message) => message.isStreaming) ? false : s.isStreaming;
      const restoredDraft = recalled.role === "user"
        ? stripDisplayContextSuffix(recalled.content, contextRefs)
        : s.draft;
      const activeConversationId = conversationId;
      const removedIds = removedMessages.map((message) => message.id).filter(Boolean);
      const existingTruncation = activeConversationId ? s.conversationRecallTruncations[activeConversationId] : undefined;
      restored = true;
      return {
        messages: nextMessages,
        draft: restoredDraft,
        selectedMentions: mentionRefs,
        selectedSkills: skillRefs,
        attachments: restoredAttachments,
        actionChip: null,
        mentionResults: [],
        slashPanelOpen: false,
        mentionPanelOpen: false,
        isStreaming: nextStreaming,
        toolCallCount: s.toolCallCount - computeToolCallCount(s.messages) + computeToolCallCount(nextMessages),
        conversationRecallTruncations: activeConversationId && removedIds.length > 0
          ? {
              ...s.conversationRecallTruncations,
              [activeConversationId]: {
                removedIds: Array.from(new Set([...(existingTruncation?.removedIds ?? []), ...removedIds])),
                retainedIds: nextMessages.map((message) => message.id),
                updatedAt: Date.now(),
              },
            }
          : s.conversationRecallTruncations,
        ...cacheMessagesForConversation(s, s.conversationId, nextMessages, nextStreaming),
      };
    });
    if (restored && conversationId) {
      sendClientCommand({
        type: "session.usage.inspect",
        conversation_id: conversationId,
        source: "conversation_recall",
        silent: true,
      }, { silent: true });
    }
    return restored;
  },
  removeEmptyStreamingAssistant: (conversationId, messageId) =>
    set((s) => {
      const targetMessageId = messageId?.trim();
      const shouldRemoveAssistant = (message: AppStore["messages"][number]) =>
        (isStructurallyEmptyAssistantMessage(message) || (
          Boolean(targetMessageId)
          && message.role === "assistant"
          && message.queueState === "queued"
          && !message.content
        )) &&
        (!targetMessageId || message.id === targetMessageId);
      const removeMessages = (messages: AppStore["messages"]) => {
        if (!targetMessageId) {
          return messages.filter((message) => !shouldRemoveAssistant(message));
        }
        const assistantIndex = messages.findIndex(shouldRemoveAssistant);
        if (assistantIndex < 0) return messages;
        const removeIndexes = new Set([assistantIndex]);
        if (messages[assistantIndex - 1]?.role === "user") {
          removeIndexes.add(assistantIndex - 1);
        }
        return messages.filter((_message, index) => !removeIndexes.has(index));
      };
      if (conversationId && s.sideChats[conversationId]) {
        const thread = s.sideChats[conversationId];
        return {
          sideChats: {
            ...s.sideChats,
            [conversationId]: {
              ...thread,
              messages: removeMessages(thread.messages),
            },
          },
        };
      }
      const targetId = conversationId || s.conversationId || undefined;
      const isActive = !targetId || targetId === s.conversationId;
      const sourceMessages = targetId && !isActive
        ? s.conversationMessages[targetId] ?? []
        : s.messages;
      const nextMessages = removeMessages(sourceMessages);
      if (targetId && !isActive) {
        return {
          conversationMessages: {
            ...s.conversationMessages,
            [targetId]: nextMessages,
          },
        };
      }
      return {
        messages: nextMessages,
        ...cacheMessagesForConversation(s, s.conversationId, nextMessages, s.isStreaming),
      };
    }),
  interrupt: () => {
    get().finishStreaming(undefined, undefined, "interrupted");
  },
  requestConversationSwitch: (id) => {
    const targetId = id.trim();
    if (!targetId) return;
    // `conversation.switched` is the causal event and the only authority on
    // which conversation is active — the backend refuses a stale or archived
    // target rather than switching to it. Applying the switch locally first
    // left the renderer showing a conversation the backend never activated, and
    // every later command then carried a conversation_id its scope check
    // rejects. Wait for applyConversationSwitched.
    sendClientCommand({ type: "conversation.switch", conversation_id: targetId });
  },
  applyConversationSwitched: ({ conversationId }) => {
    const id = conversationId.trim();
    if (!id) return;
    const currentConversationId = get().conversationId;
    if (currentConversationId && get().conversations.some((conversation) => conversation.id === currentConversationId)) {
      get().snapshotAgentState(currentConversationId);
      get().snapshotWorkbenchState(currentConversationId);
    }
    const targetConversation = get().conversations.find((c) => c.id === id);
    const targetWorkspace = conversationWorkspacePath(targetConversation);
    set((s) => {
      const sameConversation = s.conversationId === id;
      const workspaceChanged = !workspaceRootsEqual(targetWorkspace, s.workingDirectory);
      const currentStillKnown = Boolean(
        s.conversationId &&
        s.conversations.some((conversation) => conversation.id === s.conversationId),
      );
      const cachedCurrent = currentStillKnown
        ? cacheMessagesForConversation(s, s.conversationId)
        : {
            conversationMessages: s.conversationMessages,
            conversationStreaming: s.conversationStreaming,
          };
      return {
        ...conversationResetPayload(),
        ...(sameConversation
          ? {
              contextUsage: s.contextUsage,
              budgetBuckets: s.budgetBuckets,
              totalBudgetPercent: s.totalBudgetPercent,
              lastUsage: s.lastUsage,
              usageTotals: s.usageTotals,
            }
          : {}),
        conversationId: id,
        activeGoal: targetConversation?.goal ?? null,
        messages: cachedCurrent.conversationMessages[id] ?? [],
        isStreaming: cachedCurrent.conversationStreaming[id] ?? false,
        toolCallCount: computeToolCallCount(cachedCurrent.conversationMessages[id] ?? []),
        conversationMessages: cachedCurrent.conversationMessages,
        conversationStreaming: cachedCurrent.conversationStreaming,
        runtimeSession: null,
        draft: "",
        attachments: [],
        quotedMessage: null,
        selectedMentions: [],
        selectedSkills: [],
        actionChip: null,
        mentionResults: [],
        slashPanelOpen: false,
        mentionPanelOpen: false,
        prMonitor: null,
        // --- End cross-slice reset ---
        ...(targetConversation
          ? {
              workingDirectory: targetWorkspace,
              workspaceGit: workspaceChanged ? null : s.workspaceGit,
              ...(workspaceChanged
                ? editorStateForWorkspace(targetWorkspace)
                : {}),
            }
          : {}),
      };
    });
    persistActiveConversationId(id);
    get().restoreAgentState(id);
    get().restoreWorkbenchState(id);
    set((s) => ({
      diffReview: visibleDiffReviewForConversation(id, s.pendingDiffReview, s.diffReviewQueue)
        ?? s.diffReview,
    }));
    if (targetWorkspace) {
      const rt = desktop();
      if (rt?.trustWorkspace) rt.trustWorkspace(targetWorkspace);
    }
  },
  switchConversation: (id) => {
    get().applyConversationSwitched({ conversationId: id });
  },
  createConversation: async (options) => {
    const state = get();
    const id = newConversationId();
    const nextAppMode = options?.appMode ?? state.appMode;
    const requestedWorkspace = canonicalWorkspacePath(
      options?.workspaceRoot ?? state.workingDirectory,
    );
    const shouldBindWorkspace = Boolean(options?.workspaceRoot)
      || options?.bindWorkspace === true
      || (
        options?.bindWorkspace === undefined
        && nextAppMode === "code"
        && Boolean(state.workingDirectory)
      );
    if (shouldBindWorkspace && !requestedWorkspace) {
      pushToast("Select a workspace before creating a workspace-bound conversation.", "error", 4000);
      return false;
    }
    const workspaceRoot = shouldBindWorkspace
      ? requestedWorkspace
      : "";
    try {
      const result = await sendClientCommandAwaitResult({
        type: "conversation.create",
        conversation_id: id,
        title: "New chat",
        conversation_type: "main",
        workspace_root: workspaceRoot || undefined,
        permission_mode: toBackendPermissionMode(state.permissionMode),
      }, "conversation.create");
      if (!commandResultSucceeded(result)) {
        pushToast(result.message || "Unable to create the conversation.", "error", 4000);
        return false;
      }
      get().setAppMode(nextAppMode);
      return true;
    } catch (error) {
      const message = error instanceof Error && error.message.trim()
        ? error.message
        : "Unable to create the conversation.";
      pushToast(message, "error", 4000);
      return false;
    }
  },
  removeConversation: async (id) => sendConversationDeleteCommand({
    type: "conversation.delete",
    conversation_id: id,
  }),
  getVisibleMessages: (conversationId) => {
    const state = get();
    const targetId = conversationId || state.conversationId;
    if (!targetId || targetId === state.conversationId) return state.messages;
    return state.sideChats[targetId]?.messages ?? state.conversationMessages[targetId] ?? [];
  },
  setActiveGoal: (goal: ConversationGoal | null, conversationId?: string, revision?: number) =>
    set((s) => {
      const targetId = conversationId || s.conversationId || undefined;
      const isActive = !targetId || targetId === s.conversationId;
      const normalizedRevision = Number.isSafeInteger(revision) && Number(revision) >= 0
        ? Number(revision)
        : undefined;
      return {
        ...(isActive ? { activeGoal: goal } : {}),
        conversations: targetId
          ? s.conversations.map((conversation) =>
              conversation.id === targetId
                ? {
                    ...conversation,
                    goal,
                    ...(normalizedRevision !== undefined ? { revision: normalizedRevision } : {}),
                  }
                : conversation,
            )
          : s.conversations,
      };
    }),
  hydrateConversationMessages: (id, messages, options) => {
    set((s) => {
      const activate = options?.activate ?? id === s.conversationId;
      const guardedMessages = applyRecallTruncation(id, messages, s);
      const nextStreaming = options?.isStreaming ?? guardedMessages.some((message) => message.isStreaming);
      const cachedCurrent = activate
        ? cacheMessagesForConversation(s, s.conversationId)
        : {
            conversationMessages: s.conversationMessages,
            conversationStreaming: s.conversationStreaming,
          };
      return {
        ...(activate ? { conversationId: id, messages: guardedMessages, isStreaming: nextStreaming, toolCallCount: computeToolCallCount(guardedMessages) } : {}),
        conversationMessages: {
          ...cachedCurrent.conversationMessages,
          [id]: guardedMessages,
        },
        conversationStreaming: {
          ...cachedCurrent.conversationStreaming,
          [id]: nextStreaming,
        },
      };
    });
    const hydrated = get().getVisibleMessages(id);
    for (const message of hydrated) {
      if (message.role === "assistant") {
        get().flushPendingProviderProgress(id, message.id);
      }
    }
  },
  bindStreamingTurn: (conversationId, messageId, turnId) => {
    const normalizedTurnId = turnId?.trim();
    if (!normalizedTurnId) return;
    set((s) => updateMessagesForConversation(s, conversationId, (messages) => {
      const index = findStreamingIndexForMessage(messages, messageId);
      if (index < 0) return null;
      const message = messages[index];
      if (message.turnId && message.turnId !== normalizedTurnId) return null;
      if (message.turnId === normalizedTurnId) return null;
      const next = messages.slice();
      next[index] = { ...message, turnId: normalizedTurnId };
      return next;
    }));
  },
  startAgentMessage: (itemId, conversationId, messageId, source) => {
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findStreamingIndexForMessage(messages, messageId);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks = settleThinkingBlocks(msg.blocks ? msg.blocks.slice() : []).filter(
          (block) => block.type !== "text" || block.itemId !== itemId,
        );
        blocks.push({
          type: "text",
          itemId,
          content: "",
          ...(source ? { source } : {}),
          status: "in_progress",
          isStreaming: true,
        });
        next[idx] = {
          ...msg,
          isThinkingStreaming: false,
          blocks,
        };
        return next;
      });
    });
    if (conversationId?.trim() && messageId?.trim()) {
      get().flushPendingProviderProgress(conversationId, messageId);
    }
  },
  appendAgentMessageDelta: (itemId, delta, conversationId, messageId, source) => {
    let droppedDelta = false;
    set((s) => updateMessagesForConversation(s, conversationId, (messages) => {
      const idx = findStreamingIndexForMessage(messages, messageId);
      if (idx < 0 || !delta) {
        // An empty delta loses nothing; a non-empty one with no owner does.
        droppedDelta = idx < 0 && Boolean(delta);
        return null;
      }
      const next = messages.slice();
      const msg = next[idx];
      const blocks = settleThinkingBlocks(msg.blocks ? msg.blocks.slice() : []);
      const blockIndex = findAgentMessageBlockIndex(blocks, itemId);
      if (blockIndex >= 0) {
        const block = blocks[blockIndex];
        if (block.type === "text") {
          blocks[blockIndex] = {
            ...block,
            content: block.content + delta,
            ...(source ? { source } : {}),
            status: "in_progress",
            isStreaming: true,
          };
        }
      } else {
        blocks.push({
          type: "text",
          itemId,
          content: delta,
          ...(source ? { source } : {}),
          status: "in_progress",
          isStreaming: true,
        });
      }
      next[idx] = { ...msg, isThinkingStreaming: false, blocks };
      return next;
    }));
    if (droppedDelta) {
      recordDroppedAnswerText(get(), "agent_message_delta", delta, itemId, conversationId, messageId);
    }
  },
  setFinalAnswerAttachments: (conversationId, attachments, messageId) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findStreamingIndexForMessage(messages, messageId);
        if (idx < 0) return null;
        const next = messages.slice();
        next[idx] = { ...next[idx], replyAttachments: attachments };
        return next;
      });
    }),
  completeAgentMessage: (item, conversationId, metadata, messageId) => {
    let droppedAnswer = false;
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findStreamingIndexForMessage(messages, messageId);
        if (idx < 0) {
          droppedAnswer = true;
          return null;
        }
        const next = messages.slice();
        const msg = next[idx];
        const blocks = settleThinkingBlocks(msg.blocks ? msg.blocks.slice() : []);
        const targetIndex = findAgentMessageBlockIndex(blocks, item.id);
        const completed: ContentBlock = {
          type: "text",
          itemId: item.id,
          content: item.text,
          source: item.source || "model_final",
          status: item.status || "completed",
          isStreaming: false,
          ...metadata,
        };
        if (targetIndex >= 0) blocks[targetIndex] = completed;
        else blocks.push(completed);
        next[idx] = {
          ...msg,
          content: getAnswerTextFromBlocks(blocks),
          isThinkingStreaming: false,
          blocks,
        };
        return next;
      });
    });
    if (droppedAnswer) {
      recordDroppedAnswerText(
        get(),
        "agent_message_completed",
        item.text || "",
        item.id,
        conversationId,
        messageId,
      );
      // A whole final answer vanishing is the worst case of this class of drop:
      // the transcript looks finished and simply has no answer in it. Say so.
      if (String(item.text || "").trim()) {
        pushToast("收到的最终答复无法归属到任何进行中的回答，已记录在检查器中。", "warning", 6000);
      }
    }
  },
  appendThinkingChunk: (content, conversationId, metadata, messageId) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findStreamingIndexForMessage(messages, messageId);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks = msg.blocks ? msg.blocks.slice() : [];
        const thinkingMetadata = normalizeThinkingMetadata(metadata);
        const identity = thinkingMetadata.item_id;
        const matchingIndex = identity
          ? blocks.findIndex((block) => block.type === "thinking" && block.item_id === identity)
          : -1;
        const lastIndex = blocks.length - 1;
        const fallbackIndex = matchingIndex >= 0
          ? matchingIndex
          : lastIndex >= 0
            && blocks[lastIndex].type === "thinking"
            && thinkingMetadataMatches(blocks[lastIndex], thinkingMetadata)
            ? lastIndex
            : -1;
        if (fallbackIndex >= 0) {
          const existing = blocks[fallbackIndex];
          if (existing.type === "thinking") {
            blocks[fallbackIndex] = {
              ...existing,
              ...thinkingMetadata,
              content: existing.content + content,
            };
          }
        } else {
          blocks.push({ type: "thinking", content, ...thinkingMetadata });
        }
        next[idx] = { ...msg, isThinkingStreaming: true, blocks };
        return next;
      });
    }),
  settleThinking: (conversationId, messageId) =>
    set((s) => updateMessagesForConversation(s, conversationId, (messages) => {
      const idx = findStreamingIndexForMessage(messages, messageId);
      if (idx < 0) return null;
      const message = messages[idx];
      const currentBlocks = message.blocks ?? [];
      const blocks = settleThinkingBlocks(currentBlocks);
      if (!message.isThinkingStreaming && blocks.length === currentBlocks.length) return null;
      const next = messages.slice();
      next[idx] = { ...message, isThinkingStreaming: false, blocks };
      return next;
    })),
  appendProcessItem: (item, conversationId, messageId) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findStreamingIndexForMessage(messages, messageId);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks = settleThinkingBlocks(msg.blocks ? msg.blocks.slice() : []);
        const processBlock = {
          ...item,
          type: "process" as const,
          timestamp: item.timestamp ?? Date.now(),
        };
        const existingIdx = blocks.findIndex((block) =>
          block.type === "process" && block.id === item.id,
        );
        if (existingIdx >= 0) {
          const existingBlock = blocks[existingIdx];
          blocks[existingIdx] = existingBlock.type === "process"
            ? {
                ...existingBlock,
                ...processBlock,
                toolCallIds: mergeToolCallIds(existingBlock.toolCallIds, processBlock.toolCallIds),
              }
            : processBlock;
        } else {
          blocks.push(processBlock);
        }
        next[idx] = {
          ...msg,
          isThinkingStreaming: false,
          blocks,
        };
        return next;
      });
    }),
  upsertMessageProgress: (progress, conversationId, messageId) =>
    set((s) => {
      const targetConversationId = conversationId?.trim() || s.conversationId?.trim();
      const targetMessageId = messageId?.trim();
      const incoming: ProgressContentBlock = {
        ...progress,
        type: "progress",
        timestamp: Date.now(),
      };
      const provider = isProviderProgressId(incoming.id);
      let pendingKey: string | undefined;
      let pendingProgress: ProgressContentBlock | undefined;
      const result = updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = targetMessageId
          ? messages.findIndex((message) => message.role === "assistant" && message.id === targetMessageId)
          : findLastStreamingIndex(messages);
        if (idx < 0) {
          if (provider && targetConversationId && targetMessageId) {
            pendingKey = pendingProviderProgressKey(targetConversationId, targetMessageId);
            pendingProgress = incoming;
          }
          return null;
        }
        const message = messages[idx];
        const next = messages.slice();
        next[idx] = mergeProgressBlockIntoAssistant(message, incoming);
        return next;
      });

      if (!pendingKey || !pendingProgress) return result;
      const current = s.pendingProviderProgress[pendingKey] ?? [];
      const existingIdx = current.findIndex((block) => block.id === pendingProgress!.id);
      const nextPending = current.slice();
      if (existingIdx >= 0) {
        nextPending[existingIdx] = mergeProviderMessageProgress(
          nextPending[existingIdx],
          pendingProgress,
        );
      } else {
        nextPending.push(pendingProgress);
      }
      return {
        ...result,
        pendingProviderProgress: {
          ...s.pendingProviderProgress,
          [pendingKey]: nextPending,
        },
      };
    }),
  flushPendingProviderProgress: (conversationId, messageId) =>
    set((s) => {
      const owner = conversationId.trim();
      const targetMessageId = messageId.trim();
      const key = pendingProviderProgressKey(owner, targetMessageId);
      const pending = s.pendingProviderProgress[key];
      if (!pending || pending.length === 0) return s;

      let found = false;
      const result = updateMessagesForConversation(s, owner, (messages) => {
        const idx = messages.findIndex((message) =>
          message.role === "assistant" && message.id === targetMessageId,
        );
        if (idx < 0) return null;
        found = true;
        const message = messages[idx];
        if (assistantIsTerminal(message)) return messages;
        const next = messages.slice();
        next[idx] = pending.reduce(
          (current, progress) => mergeProgressBlockIntoAssistant(current, progress),
          message,
        );
        return next;
      });
      if (!found) return s;
      const pendingProviderProgress = { ...s.pendingProviderProgress };
      delete pendingProviderProgress[key];
      return { ...result, pendingProviderProgress };
    }),
  clearPendingProviderProgress: (conversationId, messageId) =>
    set((s) => {
      const owner = conversationId?.trim();
      if (!owner) return s;
      const targetMessageId = messageId?.trim();
      const prefix = `${owner}\u0000`;
      const pendingProviderProgress = Object.fromEntries(
        Object.entries(s.pendingProviderProgress).filter(([key]) =>
          targetMessageId
            ? key !== pendingProviderProgressKey(owner, targetMessageId)
            : !key.startsWith(prefix),
        ),
      );
      if (Object.keys(pendingProviderProgress).length === Object.keys(s.pendingProviderProgress).length) {
        return s;
      }
      return { pendingProviderProgress };
    }),
  removeProcessItem: (itemId, conversationId, messageId) =>
    set((s) => {
      const targetItemId = itemId.trim();
      if (!targetItemId) return {};
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const targetMessageId = messageId?.trim();
        const idx = targetMessageId
          ? messages.findIndex((message) =>
              message.role === "assistant" && message.id === targetMessageId,
            )
          : findLastStreamingIndex(messages);
        if (idx < 0) return null;
        const message = messages[idx];
        const blocks = message.blocks ?? [];
        const filtered = blocks.filter((block) =>
          !(block.type === "process" && block.id === targetItemId),
        );
        if (filtered.length === blocks.length) return null;
        const next = messages.slice();
        next[idx] = { ...message, blocks: filtered };
        return next;
      });
    }),
  appendToolCallBlock: (tc, conversationId, messageId) =>
    set((s) => {
      const result = updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findStreamingIndexForMessage(messages, messageId);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks = settleThinkingBlocks(msg.blocks ? msg.blocks.slice() : []);
        blocks.push({ type: "tool_call", record: tc });
        const baseMsg = stripLegacyContentFields(msg);
        next[idx] = { ...baseMsg, isThinkingStreaming: false, blocks };
        return next;
      });
      const targetId = conversationId || s.conversationId;
      if (result.messages && (!targetId || targetId === s.conversationId)) {
        return { ...result, toolCallCount: s.toolCallCount + 1 };
      }
      return result;
    }),
  updateToolCall: (id, patch, conversationId, scope, messageId) =>
    set((s) => {
      const matchesScope = (record: ReturnType<typeof getToolCallsFromMessage>[number]) => {
        if (record.id !== id) return false;
        // A supplied scope field is an assertion. The caller may pass the
        // record's previous scope for the one explicit lifecycle migration;
        // otherwise missing candidate fields are not wildcards.
        if (scope?.turnId && record.turnId !== scope.turnId) return false;
        if (scope?.iterationId && record.iterationId !== scope.iterationId) return false;
        if (scope?.stepId && record.stepId !== scope.stepId) return false;
        return true;
      };
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const candidates = messages.flatMap((message, messageIndex) => {
          if (messageId && message.id !== messageId) return [];
          return getContentBlocks(message).flatMap((block, blockIndex) =>
            block.type === "tool_call" && matchesScope(block.record)
              ? [{ messageIndex, blockIndex, record: block.record }]
              : [],
          );
        });
        // A lifecycle id is only safe when it resolves to exactly one block.
        // Counting messages alone allowed duplicate ids inside one message to
        // be updated together, which corrupted parallel/legacy tool records.
        if (candidates.length !== 1) return null;
        const [{ messageIndex: idx, blockIndex }] = candidates;
        const next = messages.slice();
        const msg = next[idx];
        const baseMsg = stripLegacyContentFields(msg);
        const blocks = getContentBlocks(msg).slice();
        const block = blocks[blockIndex];
        if (!block || block.type !== "tool_call") return null;
        const record = block.record;
        const incomingSeq = typeof patch.seq === "number" && Number.isSafeInteger(patch.seq)
          ? patch.seq
          : undefined;
        const existingSeq = typeof record.seq === "number" && Number.isSafeInteger(record.seq)
          ? record.seq
          : undefined;
        if (incomingSeq !== undefined && existingSeq !== undefined && incomingSeq <= existingSeq) {
          return null;
        }
        const incoming = { ...record, ...patch } as ToolCallRecord;
        const nextRecord = patch.status && isTerminalToolCallStatus(record.status)
          ? mergeToolCallResultRecord(record, incoming)
          : patch.status === "running" || patch.status === "pending"
            ? isTerminalToolCallStatus(record.status) ? record : incoming
            : incoming;
        blocks[blockIndex] = { ...block, record: nextRecord };
        next[idx] = {
          ...baseMsg,
          blocks,
        };
        return next;
      });
    }),
  finishStreaming: (conversationId, usage, terminalStatus = "completed", messageId, failureMessage, failureRecoverable, durationMs, terminationReason) => {
    set((s) => {
      const finishedAt = Date.now();
      const normalizedFailureMessage = terminalStatus === "failed" ? String(failureMessage || "").trim() : "";
      const normalizedFailureRecoverable = terminalStatus === "failed" && typeof failureRecoverable === "boolean"
        ? failureRecoverable
        : undefined;
      const targetId = conversationId || s.conversationId || undefined;
      const isActiveTarget = !targetId || targetId === s.conversationId;
      let matchedStreamingMessage = false;
      let sawOtherLiveAssistant = false;
      const result = updateMessagesForConversation(
        s,
        conversationId,
        (messages) => {
          const targetMessageId = messageId?.trim();
          const nextMessages = messages.map((m) => {
            if (!m.isStreaming && !m.isThinkingStreaming) return m;
            if (targetMessageId && m.id !== targetMessageId) {
              if (m.role === "assistant") sawOtherLiveAssistant = true;
              return m;
            }
            matchedStreamingMessage = true;
            const baseMessage = stripLegacyContentFields(m);
            const blocks = settleThinkingBlocks(getContentBlocks(m));
            const terminalBlocks = blocks.map((block) => {
              if (block.type === "text" && block.isStreaming === true) {
                const hasContent = Boolean(block.content.trim());
                return {
                  ...block,
                  source: hasContent ? "partial" : "cancelled",
                  status: hasContent ? "partial" : "cancelled",
                  isStreaming: false,
                };
              }
              if (block.type === "tool_call" && (block.record.status === "running" || block.record.status === "pending")) {
                // A tool call still running/pending at turn end never received
                // its result (lost tool_result / missed terminal event). Do not
                // fabricate "success". User interruption cancels the open
                // item; max-output/partial completion remains partial, and
                // other terminal paths remain failures.
                return {
                  ...block,
                  record: {
                    ...block.record,
                    status: terminalStatus === "interrupted"
                      ? "cancelled" as const
                      : terminalStatus === "partial"
                        ? "partial" as const
                        : "failed" as const,
                    finishedAt,
                  },
                };
              }
              if (block.type === "progress" && block.status === "running") {
                const progressStatus = terminalStatus === "failed"
                  ? "failed" as const
                  : terminalStatus === "partial" || terminalStatus === "interrupted"
                    ? "partial" as const
                    : "completed" as const;
                return {
                  ...block,
                  status: progressStatus,
                  label: terminalStatus === "interrupted" ? "\u5DF2\u4E2D\u65AD" : block.label,
                  summary: terminalStatus === "interrupted" ? "\u7528\u6237\u5DF2\u4E2D\u65AD" : block.summary,
                  timestamp: finishedAt,
                };
              }
              if (block.type === "process" && block.status === "running") {
                return {
                  ...block,
                  status: terminalStatus === "failed"
                    ? "failed" as const
                    : terminalStatus === "partial" || terminalStatus === "interrupted"
                      ? "partial" as const
                      : "completed" as const,
                  timestamp: finishedAt,
                };
              }
              return block;
            });
            const content = getAnswerTextFromBlocks(terminalBlocks);
            return {
              ...baseMessage,
              content,
              isStreaming: false,
              isThinkingStreaming: false,
              terminalStatus,
              terminationReason: String(terminationReason || "").trim() || undefined,
              failureMessage: normalizedFailureMessage || undefined,
              failureRecoverable: normalizedFailureRecoverable,
              usage,
              completedAt: finishedAt,
              durationMs: Number.isFinite(durationMs) ? Math.max(0, Number(durationMs)) : undefined,
              blocks: terminalBlocks,
            };
          });
          return targetMessageId && !matchedStreamingMessage ? null : nextMessages;
        },
        false,
      );
      if (result === s) {
        // The terminal event named a message that is no longer streaming (it
        // was already sealed, or the id belongs to a turn this client never
        // rendered). Leaving the flags set strands the spinner forever, because
        // `done` is the only fence that clears it. Nothing else is live, so the
        // conversation is idle regardless of which message the id pointed at.
        if (sawOtherLiveAssistant) return result;
        if (targetId && s.sideChats[targetId]) {
          return {
            sideChats: {
              ...s.sideChats,
              [targetId]: { ...s.sideChats[targetId], isStreaming: false },
            },
          };
        }
        return {
          ...(isActiveTarget ? { isStreaming: false } : {}),
          ...(targetId
            ? { conversationStreaming: { ...s.conversationStreaming, [targetId]: false } }
            : {}),
        };
      }
      if (!isActiveTarget) return result;
      const workState = completeRunWorkState(s, terminalStatus);
      const nextConversationAgentStates = targetId
        ? {
            ...(s.conversationAgentStates ?? {}),
            [targetId]: {
              ...((s.conversationAgentStates ?? {})[targetId] ?? {
                plan: s.plan,
                todos: s.todos,
                subagents: s.subagents,
                agentProgress: s.agentProgress,
              }),
              plan: workState.plan,
              todos: workState.todos.slice(),
            },
          }
        : s.conversationAgentStates;
      return {
        ...result,
        ...workState,
        ...(targetId ? { conversationAgentStates: nextConversationAgentStates } : {}),
      };
    });
    get().clearPendingProviderProgress(conversationId, messageId);
  },
  resumeStreaming: (conversationId, toolCallsPending, messageId, turnId, snapshotBlocks) => {
    set((s) => {
      const targetId = conversationId || s.conversationId;
      const sourceMessages = targetId && targetId !== s.conversationId
        ? s.conversationMessages[targetId] ?? []
        : s.messages;
      const targetMessageId = messageId?.trim();
      const targetIndex = targetMessageId
        ? sourceMessages.findIndex((message) => message.role === "assistant" && message.id === targetMessageId)
        : sourceMessages.length - 1;
      const targetMessage = targetIndex >= 0 && sourceMessages[targetIndex]?.role === "assistant"
        ? sourceMessages[targetIndex]
        : null;
      const hasStructuredSnapshot = snapshotBlocks !== undefined;
      const resumedBlocks = (existingBlocks: ContentBlock[]) => mergeResumeToolCalls(
        snapshotBlocks ?? existingBlocks,
        toolCallsPending,
        hasStructuredSnapshot,
      );

      let nextMessages: typeof sourceMessages;
      if (targetMessage) {
        nextMessages = sourceMessages.slice();
        const baseMessage = stripLegacyContentFields(targetMessage);
        const blocks = resumedBlocks(getContentBlocks(targetMessage));
        nextMessages[targetIndex] = {
          ...baseMessage,
          content: hasStructuredSnapshot ? getAnswerTextFromBlocks(blocks) : "",
          ...(turnId ? { turnId } : {}),
          isStreaming: true,
          blocks,
        };
      } else {
        const blocks = resumedBlocks([]);
        nextMessages = [
          ...sourceMessages,
          {
            id: targetMessageId || uniqueMessageId("resume"),
            ...(turnId ? { turnId } : {}),
            role: "assistant" as const,
            content: hasStructuredSnapshot ? getAnswerTextFromBlocks(blocks) : "",
            blocks,
            artifacts: [],
            timestamp: Date.now(),
            isStreaming: true,
          },
        ];
      }

      if (targetId && targetId !== s.conversationId) {
        return {
          isPaused: false,
          conversationMessages: { ...s.conversationMessages, [targetId]: nextMessages },
          conversationStreaming: { ...s.conversationStreaming, [targetId]: true },
        };
      }
      return {
        messages: nextMessages,
        isPaused: false,
        isStreaming: true,
        toolCallCount: computeToolCallCount(nextMessages),
        ...cacheMessagesForConversation(s, targetId, nextMessages, true),
      };
    });
    const owner = conversationId?.trim() || get().conversationId?.trim();
    const targetMessageId = messageId?.trim();
    if (owner && targetMessageId) {
      get().flushPendingProviderProgress(owner, targetMessageId);
    }
  },
  setConnected: (c) => set((s) => c
    ? {
        isConnected: true,
        connectionPhase: "connected" as ConnectionPhase,
        reconnectAttempt: 0,
        reconnectMaxAttempts: null,
        connectionError: null,
      }
    : {
        isConnected: false,
        runtimeSession: null,
        // A direct disconnect from a protocol/runtime error is terminal. The
        // transport close handler immediately replaces this with reconnecting
        // for ordinary network failures.
        connectionPhase: s.connectionPhase === "reconnecting"
          ? s.connectionPhase
          : "failed" as ConnectionPhase,
      }),
  setConnectionState: (phase, details) => set((s) => ({
    isConnected: phase === "connected",
    connectionPhase: phase,
    runtimeSession: phase === "connected" ? s.runtimeSession : null,
    reconnectAttempt: details?.attempt ?? (phase === "connected" ? 0 : s.reconnectAttempt),
    reconnectMaxAttempts: details?.maxAttempts === undefined
      ? (phase === "connected" ? null : s.reconnectMaxAttempts)
      : details.maxAttempts,
    connectionError: details?.error === undefined
      ? (phase === "connected" ? null : s.connectionError)
      : details.error,
  })),
  setLastUsage: (u) =>
    set((s) => ({
      lastUsage: u,
      usageTotals: u
        ? {
            input: s.usageTotals.input + Math.max(0, u.input || 0),
            ordinaryInput: Math.max(0, s.usageTotals.ordinaryInput || 0) + Math.max(0, u.ordinaryInput || 0),
            output: s.usageTotals.output + Math.max(0, u.output || 0),
            cacheRead: s.usageTotals.cacheRead + Math.max(0, u.cacheRead || 0),
            cacheWrite: s.usageTotals.cacheWrite + Math.max(0, u.cacheWrite || 0),
            promptCacheTotal: Math.max(0, s.usageTotals.promptCacheTotal || 0) + promptCacheEffectivePromptTokens(u),
            reasoning: Math.max(0, s.usageTotals.reasoning || 0) + Math.max(0, u.reasoning || 0),
            turns: s.usageTotals.turns + 1,
          }
        : s.usageTotals,
    })),
  ensureSideChat: (id) =>
    set((s) => {
      if (s.sideChats[id]) return s;
      const selectedContext = s.sideChatPendingContext ?? undefined;
      const recentMessages = s.messages
        .filter((message) => message.role === "user" || message.role === "assistant")
        .slice(-6)
        .map((message) => {
          const role = message.role === "assistant" ? "Assistant" : "User";
          const content = (message.content || getThinkingFromMessage(message) || "").trim().replace(/\s+/g, " ");
          return content ? `${role}: ${content.slice(0, 280)}` : "";
        })
        .filter(Boolean);
      const inheritedContext = recentMessages.length > 0
        ? `Main conversation context:\n${recentMessages.join("\n")}`
        : "";
      return {
        sideChatPendingContext: null,
        sideChats: {
          ...s.sideChats,
          [id]: { id, messages: [], isStreaming: false, draft: "", inheritedContext, selectedContext },
        },
      };
    }),
  removeSideChat: (id) =>
    set((s) => {
      if (!s.sideChats[id]) return s;
      const next = { ...s.sideChats };
      delete next[id];
      return { sideChats: next };
    }),
  setSideChatDraft: (id, draft) =>
    set((s) => {
      const thread = s.sideChats[id];
      if (!thread) return s;
      return { sideChats: { ...s.sideChats, [id]: { ...thread, draft } } };
    }),
  startSideChatMessage: (id, content, messageIds) =>
    set((s) => {
      const thread = s.sideChats[id];
      if (!thread) return s;
      const t = Date.now();
      return {
        sideChats: {
          ...s.sideChats,
          [id]: {
            ...thread,
            isStreaming: true,
            draft: "",
            messages: [
              ...thread.messages,
              {
                id: messageIds.userMessageId,
                role: "user",
                content,
                artifacts: [],
                timestamp: t,
              },
              {
                id: messageIds.assistantMessageId,
                role: "assistant",
                content: "",
                blocks: [],
                artifacts: [],
                timestamp: t,
                isStreaming: true,
              },
            ],
          },
        },
      };
    }),
});
