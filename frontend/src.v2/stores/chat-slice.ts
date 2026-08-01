import type { StateCreator } from "zustand";
import type {
  AppStore,
  ChatSlice,
  ContentBlock,
  ConversationGoal,
  MessageContextRef,
  MessageAttachmentRef,
  SkillContextRef,
} from "./types";
import { canonicalWorkspacePath } from "../lib/workspace-display";
import { promptCacheEffectivePromptTokens } from "../chat/cacheUsage";
import { desktop } from "../desktop/runtime";
import { toBackendPermissionMode } from "../protocol/permissions";
import { sendClientCommand, sendConversationDeleteCommand } from "../protocol/ws-outbox";
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
  ensureCodePanelSlots,
  persistPanelSlots,
  LS,
  writeLS,
} from "./shared-helpers";

function stripDisplayContextSuffix(content: string, refs: MessageContextRef[]): string {
  const suffixItems = refs
    .filter((ref) => ref.kind !== "skill")
    .map((ref) => `@${ref.name}`)
    .filter((name) => name.length > 1);
  if (suffixItems.length === 0) return content;
  const suffix = ` ${suffixItems.join(" ")}`;
  return content.endsWith(suffix) ? content.slice(0, -suffix.length).trimEnd() : content;
}

type ProcessBlock = Extract<ContentBlock, { type: "process" }>;

function findAgentMessageBlockIndex(blocks: ContentBlock[], itemId: string): number {
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (block.type === "text" && block.itemId === itemId) return index;
  }
  return -1;
}

function normalizeProcessContent(content: string | undefined): string {
  return (content || "").trim().replace(/\s+/g, " ");
}

function mergeToolCallIds(left: string[] | undefined, right: string[] | undefined): string[] | undefined {
  const ids = [...(left || []), ...(right || [])].filter(Boolean);
  if (ids.length === 0) return undefined;
  return Array.from(new Set(ids));
}

function isDuplicateRuntimeAction(block: ContentBlock, item: ProcessBlock): block is ProcessBlock {
  return (
    block.type === "process" &&
    block.itemKind === "action_summary" &&
    item.itemKind === "action_summary" &&
    block.source === "runtime" &&
    item.source === "runtime" &&
    normalizeProcessContent(block.content) === normalizeProcessContent(item.content)
  );
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
      plan: state.plan?.status === "executing"
        ? { ...state.plan, status: "cancelled" as const }
        : state.plan,
      todos: state.todos.map((todo) =>
        todo.status === "in_progress"
          ? { ...todo, status: "blocked" as const, activeForm: todo.activeForm || unfinishedLabel }
          : todo,
      ),
    };
  }

  return {
    plan: state.plan?.status === "executing" || state.plan?.status === "accepted"
      ? {
          ...state.plan,
          status: "completed" as const,
          steps: state.plan.steps.map((step) =>
            step.status === "running" || step.status === "pending"
              ? { ...step, status: "done" as const }
              : step,
          ),
        }
      : state.plan,
    todos: state.todos.map((todo) =>
      todo.status === "in_progress" || todo.status === "pending"
        ? { ...todo, status: "completed" as const, activeForm: "" }
        : todo,
    ),
  };
}

function findStreamingIndexForMessage(messages: AppStore["messages"], messageId?: string): number {
  const targetId = messageId?.trim();
  if (!targetId) return findLastStreamingIndex(messages);
  return messages.findIndex((message) =>
    message.role === "assistant" &&
    Boolean(message.isStreaming) &&
    message.id === targetId,
  );
}

export const createChatSlice: StateCreator<AppStore, [], [], ChatSlice> = (set, get) => ({
  conversationId: null,
  conversations: [],
  activeGoal: null,
  messages: [],
  conversationMessages: {},
  conversationStreaming: {},
  conversationRecallTruncations: {},
  isStreaming: false,
  isPaused: false,
  isConnected: false,
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
  deleteMessage: (id) =>
    set((s) => {
      const nextMessages = s.messages.filter((message) => message.id !== id);
      const nextStreaming = s.messages.some((message) => message.id === id && message.isStreaming) ? false : s.isStreaming;
      return {
        messages: nextMessages,
        isStreaming: nextStreaming,
        toolCallCount: s.toolCallCount - computeToolCallCount(s.messages) + computeToolCallCount(nextMessages),
        ...cacheMessagesForConversation(s, s.conversationId, nextMessages, nextStreaming),
      };
    }),
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
  recallMessage: (id) => {
    let truncateCommand: Parameters<typeof sendClientCommand>[0] | null = null;
    set((s) => {
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
      const restoredAttachments = attachmentRefs.map((attachment) => ({
        id: `att-recall-${attachment.artifactId || attachment.id}-${Date.now().toString(36)}`,
        name: attachment.name,
        type: attachment.mediaType || "application/octet-stream",
        size: attachment.sizeBytes || 0,
        status: "ready" as const,
        artifactId: attachment.artifactId,
        docId: attachment.docId,
        inputSource: attachment.inputSource,
        sourceCharCount: attachment.sourceCharCount,
        attachment: {
          id: attachment.id,
          kind: attachment.kind,
          file_name: attachment.name,
          media_type: attachment.mediaType,
          artifact_id: attachment.artifactId,
          doc_id: attachment.docId,
          size_bytes: attachment.sizeBytes ?? 0,
          ...(attachment.inputSource ? { input_source: attachment.inputSource } : {}),
          ...(attachment.sourceCharCount ? { source_char_count: attachment.sourceCharCount } : {}),
        },
      }));
      const removedMessages = s.messages.slice(turnStart);
      const nextStreaming = removedMessages.some((message) => message.isStreaming) ? false : s.isStreaming;
      const restoredDraft = recalled.role === "user"
        ? stripDisplayContextSuffix(recalled.content, contextRefs)
        : s.draft;
      const conversationId = s.conversationId || "";
      const removedIds = removedMessages.map((message) => message.id).filter(Boolean);
      const existingTruncation = conversationId ? s.conversationRecallTruncations[conversationId] : undefined;
      if (conversationId && recalled.id) {
        truncateCommand = {
          type: "conversation.truncate",
          conversation_id: conversationId,
          truncate_before_message_id: recalled.id,
          retained_message_ids: nextMessages.map((message) => message.id),
        };
      }
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
        conversationRecallTruncations: conversationId && removedIds.length > 0
          ? {
              ...s.conversationRecallTruncations,
              [conversationId]: {
                removedIds: Array.from(new Set([...(existingTruncation?.removedIds ?? []), ...removedIds])),
                retainedIds: nextMessages.map((message) => message.id),
                updatedAt: Date.now(),
              },
            }
          : s.conversationRecallTruncations,
        ...cacheMessagesForConversation(s, s.conversationId, nextMessages, nextStreaming),
      };
    });
    if (truncateCommand) {
      sendClientCommand(truncateCommand);
    }
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
    // Optimistically switch the UI from local cache; the canonical
    // conversation.switched event re-applies and hydrates the transcript.
    if (get().conversations.some((conversation) => conversation.id === targetId)) {
      get().applyConversationSwitched({ conversationId: targetId });
    }
    sendClientCommand({ type: "conversation.switch", conversation_id: targetId });
  },
  applyConversationSwitched: ({ conversationId }) => {
    const id = conversationId.trim();
    if (!id) return;
    const currentConversationId = get().conversationId;
    if (currentConversationId) {
      get().snapshotAgentState(currentConversationId);
      get().snapshotWorkbenchState(currentConversationId);
    }
    const targetConversation = get().conversations.find((c) => c.id === id);
    const targetWorkspace = conversationWorkspacePath(targetConversation);
    set((s) => {
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
              workspaceGit: targetWorkspace !== s.workingDirectory ? null : s.workspaceGit,
              ...(targetWorkspace !== s.workingDirectory ? editorStateForWorkspace(targetWorkspace) : {}),
            }
          : {}),
      };
    });
    persistActiveConversationId(id);
    get().restoreAgentState(id);
    get().restoreWorkbenchState(id);
    if (targetWorkspace) {
      const rt = desktop();
      if (rt?.trustWorkspace) rt.trustWorkspace(targetWorkspace);
    }
  },
  switchConversation: (id) => {
    get().applyConversationSwitched({ conversationId: id });
  },
  createConversation: (options) => {
    const state = get();
    if (state.conversationId) {
      get().snapshotAgentState(state.conversationId);
      get().snapshotWorkbenchState(state.conversationId);
    }
    const id = newConversationId();
    const shouldBindWorkspace = Boolean(options?.bindWorkspace || options?.workspaceRoot);
    const workspaceRoot = shouldBindWorkspace
      ? canonicalWorkspacePath(options?.workspaceRoot ?? state.workingDirectory)
      : "";
    const currentWorkspaceRoot = canonicalWorkspacePath(state.workingDirectory);
    const canUseCurrentWorkspaceGit = Boolean(workspaceRoot && workspaceRoot === currentWorkspaceRoot);
    const nextAppMode = options?.appMode ?? "cowork";
    sendClientCommand({
      type: "conversation.create",
      conversation_id: id,
      title: "New chat",
      workspace_root: workspaceRoot || undefined,
      permission_mode: toBackendPermissionMode(state.permissionMode),
    });
    set((s) => {
      const cachedCurrent = cacheMessagesForConversation(s, s.conversationId);
      const panelSlots = nextAppMode === "code" ? ensureCodePanelSlots(s.panelSlots) : s.panelSlots;
      if (nextAppMode === "code") persistPanelSlots(panelSlots);
      return {
        ...conversationResetPayload(),
        conversationId: id,
        conversations: [
          {
            id,
            title: "New chat",
            updatedAt: new Date().toISOString(),
            workspaceRoot: workspaceRoot || undefined,
            gitBranch: canUseCurrentWorkspaceGit ? state.workspaceGit?.branch || undefined : undefined,
            worktreePath: canUseCurrentWorkspaceGit ? state.workspaceGit?.currentPath || undefined : undefined,
            gitIsolated: canUseCurrentWorkspaceGit ? state.workspaceGit?.isWorktree : undefined,
            goal: null,
          },
          ...s.conversations,
        ],
        activeGoal: null,
        messages: [],
        isStreaming: false,
        toolCallCount: 0,
        appMode: nextAppMode,
        workingDirectory: workspaceRoot,
        workspaceGit: workspaceRoot !== s.workingDirectory ? null : s.workspaceGit,
        ...(workspaceRoot !== s.workingDirectory ? editorStateForWorkspace(workspaceRoot) : {}),
        ...(nextAppMode === "code" ? { panelSlots } : {}),
        conversationMessages: {
          ...cachedCurrent.conversationMessages,
          [id]: [],
        },
        conversationStreaming: {
          ...cachedCurrent.conversationStreaming,
          [id]: false,
        },
        conversationAgentStates: {
          ...(s.conversationAgentStates ?? {}),
          [id]: { plan: null, todos: [], subagents: [], agentProgress: [] },
        },
        conversationWorkbenchStates: {
          ...(s.conversationWorkbenchStates ?? {}),
          [id]: {
            diffReview: null,
            previewArtifact: null,
            livePreviewUrl: null,
            activeTerminalSessionId: null,
            rightStackTab: "tasks" as const,
            rightPanelOpen: false,
            rightStackTabLocked: false,
            draft: "",
            attachments: [],
            quotedMessage: null,
            selectedMentions: [],
            selectedSkills: [],
            allowedRemoteImageDomains: [],
          },
        },
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
      };
    });
    persistActiveConversationId(id);
    get().restoreWorkbenchState(id);
  },
  removeConversation: (id) => {
    let state = get();
    if (state.conversationId && state.conversationId !== id) {
      get().snapshotAgentState(state.conversationId);
      get().snapshotWorkbenchState(state.conversationId);
      state = get();
    }
    void sendConversationDeleteCommand({ type: "conversation.delete", conversation_id: id });
    const remaining = state.conversations.filter((conversation) => conversation.id !== id);
    const nextActive = remaining.find((conversation) => !conversation.archived);
    const conversationMessages = Object.fromEntries(
      Object.entries(state.conversationMessages).filter(([key]) => key !== id),
    );
    const conversationStreaming = Object.fromEntries(
      Object.entries(state.conversationStreaming).filter(([key]) => key !== id),
    );
    const conversationAgentStates = { ...(state.conversationAgentStates ?? {}) };
    delete conversationAgentStates[id];
    const conversationWorkbenchStates = { ...(state.conversationWorkbenchStates ?? {}) };
    delete conversationWorkbenchStates[id];
    const conversationRecallTruncations = { ...(state.conversationRecallTruncations ?? {}) };
    delete conversationRecallTruncations[id];

    if (state.conversationId !== id) {
      set({
        conversations: remaining,
        conversationMessages,
        conversationStreaming,
        conversationAgentStates,
        conversationWorkbenchStates,
        conversationRecallTruncations,
      });
      return;
    }

    if (nextActive) {
      const targetWorkspace = conversationWorkspacePath(nextActive);
      const targetMessages = conversationMessages[nextActive.id] ?? [];
      set((s) => ({
        ...conversationResetPayload(),
        conversations: remaining,
        conversationMessages,
        conversationStreaming,
        conversationAgentStates,
        conversationWorkbenchStates,
        conversationRecallTruncations,
        conversationId: nextActive.id,
        activeGoal: nextActive.goal ?? null,
        messages: targetMessages,
        isStreaming: conversationStreaming[nextActive.id] ?? false,
        toolCallCount: computeToolCallCount(targetMessages),
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
        workingDirectory: targetWorkspace,
        workspaceGit: targetWorkspace !== s.workingDirectory ? null : s.workspaceGit,
        ...(targetWorkspace !== s.workingDirectory ? editorStateForWorkspace(targetWorkspace) : {}),
      }));
      persistActiveConversationId(nextActive.id);
      get().restoreAgentState(nextActive.id);
      get().restoreWorkbenchState(nextActive.id);
      if (targetWorkspace) {
        const rt = typeof window !== "undefined"
          ? (window as any).__MINICODE_RUNTIME__?.desktop
          : undefined;
        if (rt?.trustWorkspace) rt.trustWorkspace(targetWorkspace);
      }
      return;
    }

    const hasCodeContext = Boolean(
      state.workingDirectory ||
      state.editorTabs.length > 0 ||
      state.activeTabPath ||
      state.activeEditorPath,
    );
    const panelSlots = hasCodeContext ? ensureCodePanelSlots(state.panelSlots) : state.panelSlots;
    if (hasCodeContext) persistPanelSlots(panelSlots);

    set({
      ...conversationResetPayload(),
      conversations: remaining,
      conversationMessages,
      conversationStreaming,
      conversationAgentStates,
      conversationWorkbenchStates,
      conversationRecallTruncations,
      conversationId: null,
      activeGoal: null,
      messages: [],
      isStreaming: false,
      toolCallCount: 0,
      draft: "",
      attachments: [],
      quotedMessage: null,
      selectedMentions: [],
      selectedSkills: [],
      allowedRemoteImageDomains: [],
      actionChip: null,
      mentionResults: [],
      slashPanelOpen: false,
      mentionPanelOpen: false,
      prMonitor: null,
      ...(hasCodeContext ? { appMode: "code" as const, panelSlots } : {}),
    });
    persistActiveConversationId(null);
  },
  getVisibleMessages: (conversationId) => {
    const state = get();
    const targetId = conversationId || state.conversationId;
    if (!targetId || targetId === state.conversationId) return state.messages;
    return state.sideChats[targetId]?.messages ?? state.conversationMessages[targetId] ?? [];
  },
  setActiveGoal: (goal: ConversationGoal | null, conversationId?: string) =>
    set((s) => {
      const targetId = conversationId || s.conversationId || undefined;
      const isActive = !targetId || targetId === s.conversationId;
      return {
        ...(isActive ? { activeGoal: goal } : {}),
        conversations: targetId
          ? s.conversations.map((conversation) =>
              conversation.id === targetId ? { ...conversation, goal } : conversation,
            )
          : s.conversations,
      };
    }),
  hydrateConversationMessages: (id, messages, options) =>
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
    }),
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
  startAgentMessage: (itemId, conversationId, messageId) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findStreamingIndexForMessage(messages, messageId);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks = (msg.blocks ? msg.blocks.slice() : []).filter((block) =>
          block.type !== "text" || block.itemId !== itemId,
        );
        blocks.push({ type: "text", itemId, content: "", status: "in_progress", isStreaming: true });
        next[idx] = {
          ...msg,
          isThinkingStreaming: false,
          blocks,
        };
        return next;
      });
    }),
  appendAgentMessageDelta: (itemId, delta, conversationId, messageId) =>
    set((s) => updateMessagesForConversation(s, conversationId, (messages) => {
      const idx = findStreamingIndexForMessage(messages, messageId);
      if (idx < 0 || !delta) return null;
      const next = messages.slice();
      const msg = next[idx];
      const blocks = msg.blocks ? msg.blocks.slice() : [];
      const blockIndex = findAgentMessageBlockIndex(blocks, itemId);
      if (blockIndex >= 0) {
        const block = blocks[blockIndex];
        if (block.type === "text") {
          blocks[blockIndex] = { ...block, content: block.content + delta, status: "in_progress", isStreaming: true };
        }
      } else {
        blocks.push({ type: "text", itemId, content: delta, status: "in_progress", isStreaming: true });
      }
      next[idx] = { ...msg, isThinkingStreaming: false, blocks };
      return next;
    })),
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
  completeAgentMessage: (item, conversationId, metadata, messageId) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findStreamingIndexForMessage(messages, messageId);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks: ContentBlock[] = msg.blocks ? msg.blocks.slice() : [];
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
    }),
  appendThinkingChunk: (content, conversationId, metadata, messageId) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findStreamingIndexForMessage(messages, messageId);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks = msg.blocks ? msg.blocks.slice() : [];
        const last = blocks[blocks.length - 1];
        const thinkingMetadata = normalizeThinkingMetadata(metadata);
        if (last && last.type === "thinking" && thinkingMetadataMatches(last, thinkingMetadata)) {
          blocks[blocks.length - 1] = { ...last, content: last.content + content };
        } else {
          blocks.push({ type: "thinking", content, ...thinkingMetadata });
        }
        next[idx] = { ...msg, isThinkingStreaming: true, blocks };
        return next;
      });
    }),
  appendProcessItem: (item, conversationId, messageId) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findStreamingIndexForMessage(messages, messageId);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks = msg.blocks ? msg.blocks.slice() : [];
        const processBlock = {
          ...item,
          type: "process" as const,
          timestamp: item.timestamp ?? Date.now(),
        };
        const existingIdx = blocks.findIndex((block) =>
          block.type === "process" && block.id === item.id,
        );
        const duplicateRuntimeActionIdx = existingIdx >= 0
          ? existingIdx
          : blocks.findIndex((block) => isDuplicateRuntimeAction(block, processBlock));
        if (duplicateRuntimeActionIdx >= 0) {
          const existingBlock = blocks[duplicateRuntimeActionIdx];
          blocks[duplicateRuntimeActionIdx] = existingBlock.type === "process"
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
  appendToolCallBlock: (tc, conversationId, messageId) =>
    set((s) => {
      const result = updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findStreamingIndexForMessage(messages, messageId);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks = msg.blocks ? msg.blocks.slice() : [];
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
        // messageId already scopes this reducer to one assistant turn. The
        // provider tool-use ID is authoritative within that turn.
        if (messageId) return true;
        if (scope?.turnId && record.turnId && record.turnId !== scope.turnId) return false;
        if (scope?.iterationId && record.iterationId && record.iterationId !== scope.iterationId) return false;
        if (scope?.stepId && record.stepId && record.stepId !== scope.stepId) return false;
        return true;
      };
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = messages.findIndex((m) =>
          (!messageId || m.id === messageId)
          && getToolCallsFromMessage(m).some(matchesScope),
        );
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const baseMsg = stripLegacyContentFields(msg);
        next[idx] = {
          ...baseMsg,
          blocks: getContentBlocks(msg).map((block) =>
            block.type === "tool_call" && matchesScope(block.record)
              ? { ...block, record: { ...block.record, ...patch } }
              : block,
          ),
        };
        return next;
      });
    }),
  finishStreaming: (conversationId, usage, terminalStatus = "completed", messageId, failureMessage, durationMs) =>
    set((s) => {
      const finishedAt = Date.now();
      const normalizedFailureMessage = terminalStatus === "failed" ? String(failureMessage || "").trim() : "";
      const targetId = conversationId || s.conversationId || undefined;
      const isActiveTarget = !targetId || targetId === s.conversationId;
      let matchedStreamingMessage = false;
      const result = updateMessagesForConversation(
        s,
        conversationId,
        (messages) => {
          const targetMessageId = messageId?.trim();
          const nextMessages = messages.map((m) => {
            if (!m.isStreaming && !m.isThinkingStreaming) return m;
            if (targetMessageId && m.id !== targetMessageId) return m;
            matchedStreamingMessage = true;
            const baseMessage = stripLegacyContentFields(m);
            const blocks = getContentBlocks(m);
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
                // fabricate "success". User interruption is partial rather
                // than failed; other terminal paths remain failures.
                return {
                  ...block,
                  record: {
                    ...block.record,
                    status: terminalStatus === "interrupted" || terminalStatus === "partial"
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
              resumeState: undefined,
              terminalStatus,
              failureMessage: normalizedFailureMessage || undefined,
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
      if (!isActiveTarget || result === s) return result;
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
    }),
  resumeStreaming: (conversationId, toolCallsPending, messageId, turnId, snapshotBlocks) =>
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
          resumeState: "resumed",
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
            resumeState: "resumed",
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
    }),
  setConnected: (c) => set({ isConnected: c }),
  setLastUsage: (u) =>
    set((s) => ({
      lastUsage: u,
      usageTotals: u
        ? {
            input: s.usageTotals.input + Math.max(0, u.input || 0),
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
        sideChats: {
          ...s.sideChats,
          [id]: { id, messages: [], isStreaming: false, draft: "", inheritedContext },
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
