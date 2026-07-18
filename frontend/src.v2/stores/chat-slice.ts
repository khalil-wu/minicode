import type { StateCreator } from "zustand";
import type {
  AppStore,
  ChatSlice,
  ContentBlock,
  ConversationGoal,
  FileContextRef,
  MessageContextRef,
  MessageAttachmentRef,
  SkillContextRef,
} from "./types";
import { canonicalWorkspacePath } from "../lib/workspace-display";
import { promptCacheEffectivePromptTokens } from "../chat/cacheUsage";
import { desktop } from "../desktop/runtime";
import { toBackendPermissionMode } from "../protocol/permissions";
import { sendClientCommand } from "../protocol/ws-outbox";
import {
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
type TextBlock = Extract<ContentBlock, { type: "text" }>;

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

function textMetadataMatches(
  block: TextBlock,
  source: string,
  metadata?: Partial<Omit<TextBlock, "type" | "content" | "source">>,
): boolean {
  if (block.sealed) return false;
  const incomingSegmentId = metadata?.segmentId;
  if (incomingSegmentId || block.segmentId) {
    return (
      (block.source || "stream") === source &&
      block.segmentId === incomingSegmentId &&
      block.visibility === metadata?.visibility &&
      block.role === metadata?.role &&
      block.phase === metadata?.phase
    );
  }
  return (
    (block.source || "stream") === source &&
    block.visibility === metadata?.visibility &&
    block.role === metadata?.role &&
    block.phase === metadata?.phase
  );
}

function mergeProviderRaw(
  left: TextBlock["providerRaw"] | undefined,
  right: TextBlock["providerRaw"] | undefined,
): TextBlock["providerRaw"] | undefined {
  if (!left) return right;
  if (!right) return left;
  return { ...left, ...right };
}

function isTimelineTextSource(source: string | undefined): boolean {
  return source === "model_preamble" || source === "post_tool" || source === "runtime";
}

function isFinalTextPhase(phase: string | undefined): boolean {
  return phase === "final" || phase === "final_answer";
}

function isCommentaryTextPhase(phase: string | undefined): boolean {
  return phase === "commentary";
}

function isFinalTextMetadata(
  metadata?: Partial<Omit<TextBlock, "type" | "content" | "source">>,
): boolean {
  return metadata?.visibility === "final" || isFinalTextPhase(metadata?.phase);
}

function isTimelineTextMetadata(
  source: string | undefined,
  metadata?: Partial<Omit<TextBlock, "type" | "content" | "source">>,
): boolean {
  return (
    !isFinalTextMetadata(metadata) &&
    (
    metadata?.visibility === "timeline" ||
    metadata?.visibility === "debug" ||
    metadata?.role === "runtime" ||
    isCommentaryTextPhase(metadata?.phase) ||
    isTimelineTextSource(source)
    )
  );
}

function isUnsealedTextMetadata(
  metadata?: Partial<Omit<TextBlock, "type" | "content" | "source">>,
): boolean {
  return metadata?.visibility === "unsealed" || metadata?.visibility === "draft";
}

function isReplaceableTextBlock(block: TextBlock): boolean {
  return !block.sealed && !isTimelineTextMetadata(block.source, block);
}

function isNarrationTextBlock(block: ContentBlock): block is TextBlock {
  return (
    block.type === "text" &&
    block.source === "model_narration" &&
    block.sealed !== true
  );
}

function findTargetNarrationBlockIndex(
  blocks: ContentBlock[],
  metadata?: Partial<Omit<TextBlock, "type" | "content" | "source">>,
): number {
  const segmentId = metadata?.segmentId;
  for (let i = blocks.length - 1; i >= 0; i -= 1) {
    const block = blocks[i];
    if (!isNarrationTextBlock(block)) continue;
    if (segmentId && block.segmentId !== segmentId) continue;
    if (!segmentId && block.visibility !== "timeline") continue;
    return i;
  }
  return -1;
}

function findFinalizableTextBlockIndex(
  blocks: ContentBlock[],
  source?: string,
  metadata?: Partial<Omit<TextBlock, "type" | "content" | "source">>,
): number {
  const narrationIndex = findTargetNarrationBlockIndex(blocks, metadata);
  if (narrationIndex >= 0) return narrationIndex;
  for (let i = blocks.length - 1; i >= 0; i -= 1) {
    const block = blocks[i];
    if (block.type !== "text" || !isReplaceableTextBlock(block)) continue;
    if (source === "model_narration" && block.source !== "model_narration") continue;
    return i;
  }
  return -1;
}

function isExplicitFinalTextBlock(block: ContentBlock): block is TextBlock {
  return (
    block.type === "text" &&
    Boolean(block.content.trim()) &&
    (
      block.visibility === "final" ||
      isFinalTextPhase(block.phase) ||
      block.source === "reply" ||
      block.source === "model_final" ||
      block.source === "fallback" ||
      block.source === "partial"
    )
  );
}

function findDuplicateFinalTextBlockIndex(
  blocks: ContentBlock[],
  content: string,
): number {
  const normalized = content.trim();
  if (!normalized) return -1;
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (!isExplicitFinalTextBlock(block)) continue;
    if (block.content.trim() === normalized) return index;
  }
  return -1;
}

function isActivityContentBlock(block: ContentBlock): boolean {
  return block.type === "tool_call" || block.type === "progress" || block.type === "thinking" || block.type === "process";
}

function lastAutoFinalizableUnsealedIndex(blocks: ContentBlock[]): number {
  if (blocks.some(isExplicitFinalTextBlock)) return -1;
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (block.type !== "text" || !block.content.trim()) continue;
    if (!isUnsealedTextMetadata(block) || !isReplaceableTextBlock(block)) return -1;
    const hasActivityAfter = blocks.slice(index + 1).some(isActivityContentBlock);
    return hasActivityAfter ? -1 : index;
  }
  return -1;
}

function contributesToAnswerText(
  source: string | undefined,
  metadata?: Partial<Omit<TextBlock, "type" | "content" | "source">>,
): boolean {
  if (isFinalTextMetadata(metadata)) return true;
  if (isCommentaryTextPhase(metadata?.phase)) return false;
  return !isTimelineTextMetadata(source, metadata) && !isUnsealedTextMetadata(metadata);
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
  if (terminalStatus === "failed" || terminalStatus === "interrupted") {
    return {
      plan: state.plan?.status === "executing"
        ? { ...state.plan, status: "cancelled" as const }
        : state.plan,
      todos: state.todos.map((todo) =>
        todo.status === "in_progress"
          ? { ...todo, status: "blocked" as const, activeForm: todo.activeForm || "运行已停止" }
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
      const fileRefs: FileContextRef[] = contextRefs.flatMap((ref) =>
        ref.kind === "skill"
          ? []
          : [{ path: ref.path, name: ref.name, kind: ref.kind }],
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
        indexedChunks: attachment.indexedChunks,
        inputSource: attachment.inputSource,
        sourceCharCount: attachment.sourceCharCount,
        attachment: {
          id: attachment.id,
          kind: attachment.kind,
          file_name: attachment.name,
          media_type: attachment.mediaType,
          artifact_id: attachment.artifactId,
          doc_id: attachment.docId,
          indexed_chunks: attachment.indexedChunks ?? 0,
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
        selectedMentions: fileRefs,
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
  pauseStreaming: () => {
    // Streaming pause is intentionally unavailable until the server exposes
    // a real backpressure/resume contract.
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
    sendClientCommand({ type: "conversation.delete", conversation_id: id });
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
  appendTextChunk: (content, conversationId, source, metadata, messageId) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findStreamingIndexForMessage(messages, messageId);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks = msg.blocks ? msg.blocks.slice() : [];
        const last = blocks[blocks.length - 1];
        const nextSource = source || "stream";
        const contributesToAnswer = contributesToAnswerText(nextSource, metadata);
        const streamingTextMetadata = nextSource === "model_narration" && metadata?.visibility === "timeline"
          ? { isStreaming: true }
          : {};
        if (contributesToAnswer && isFinalTextMetadata(metadata)) {
          const duplicateFinalIndex = findDuplicateFinalTextBlockIndex(blocks, content);
          if (duplicateFinalIndex >= 0) {
            const duplicate = blocks[duplicateFinalIndex];
            if (duplicate.type === "text") {
              blocks[duplicateFinalIndex] = {
                ...duplicate,
                source: nextSource || duplicate.source,
                ...metadata,
                providerRaw: mergeProviderRaw(duplicate.providerRaw, metadata?.providerRaw),
                finishReason: metadata?.finishReason ?? duplicate.finishReason,
              };
            }
            next[idx] = {
              ...msg,
              content: msg.content || content,
              isThinkingStreaming: false,
              blocks,
            };
            return next;
          }
        }
        if (last && last.type === "text" && textMetadataMatches(last, nextSource, metadata)) {
          // Preserve the existing attribution when appending to an open text
          // block — only freshly opened blocks carry an explicit source (e.g.
          // the "reply" BriefTool reply), so a streaming reply is not
          // relabeled by a later chunk.
          blocks[blocks.length - 1] = {
            ...last,
            content: last.content + content,
            providerRaw: mergeProviderRaw(last.providerRaw, metadata?.providerRaw),
            finishReason: metadata?.finishReason ?? last.finishReason,
            ...streamingTextMetadata,
          };
        } else {
          blocks.push({ type: "text", content, source: nextSource, ...metadata, ...streamingTextMetadata });
        }
        next[idx] = {
          ...msg,
          content: contributesToAnswer ? msg.content + content : msg.content,
          isThinkingStreaming: false,
          blocks,
        };
        return next;
      });
    }),
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
  finalizeStreamingText: (conversationId, source, metadata, messageId) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findStreamingIndexForMessage(messages, messageId);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks: ContentBlock[] = msg.blocks ? msg.blocks.slice() : [];
        const targetIndex = findFinalizableTextBlockIndex(blocks, source, metadata);
        if (targetIndex < 0) return null;
        const block = blocks[targetIndex];
        if (block.type !== "text") return null;
        const nextVisibility = metadata?.visibility || "final";
        const promotedToFinal = nextVisibility === "final";
        if (promotedToFinal && metadata?.promoteAllUnsealedNarration) {
          const narrationIndexes = blocks.flatMap((candidate, index) =>
            candidate.type === "text" &&
            candidate.source === "model_narration" &&
            candidate.visibility === "timeline" &&
            candidate.sealed !== true
              ? [index]
              : [],
          );
          if (narrationIndexes.length > 0) {
            const firstIndex = narrationIndexes[0];
            const narrationContent = narrationIndexes
              .map((index) => {
                const candidate = blocks[index];
                return candidate.type === "text" ? candidate.content : "";
              })
              .join("");
            const firstBlock = blocks[firstIndex];
            const rewritten = blocks.filter((_, index) => !narrationIndexes.includes(index) || index === firstIndex);
            const rewrittenIndex = rewritten.findIndex((candidate) => candidate === firstBlock);
            if (firstBlock.type === "text" && rewrittenIndex >= 0) {
              rewritten[rewrittenIndex] = {
                ...firstBlock,
                source: source || "model_final",
                ...metadata,
                visibility: "final",
                phase: metadata?.phase || "final",
                content: narrationContent,
                sealed: true,
                isStreaming: false,
              } as ContentBlock;
              next[idx] = {
                ...msg,
                content: msg.content && msg.content !== narrationContent ? `${narrationContent}${msg.content}` : narrationContent,
                blocks: rewritten,
              };
              return next;
            }
          }
        }
        blocks[targetIndex] = {
          ...block,
          source: source || block.source || (promotedToFinal ? "model_final" : "stream"),
          ...metadata,
          visibility: nextVisibility,
          phase: metadata?.phase || (promotedToFinal ? "final" : block.phase),
          sealed: true,
          isStreaming: false,
        } as ContentBlock;
        next[idx] = {
          ...msg,
          content: promotedToFinal
            ? (msg.content && msg.content !== block.content ? `${block.content}${msg.content}` : block.content)
            : msg.content,
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
          isThinkingStreaming: item.status === "running" || msg.isThinkingStreaming,
          blocks,
        };
        return next;
      });
    }),
  appendProgress: (progress, conversationId, messageId) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findStreamingIndexForMessage(messages, messageId);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks = msg.blocks ? msg.blocks.slice() : [];
        const progressBlock = {
          ...progress,
          type: "progress" as const,
          timestamp: Date.now(),
        };
        const existingIdx = blocks.findIndex((block) =>
          block.type === "progress" && block.id === progress.id,
        );
        if (existingIdx >= 0) {
          blocks[existingIdx] = progressBlock;
        } else {
          blocks.push(progressBlock);
        }
        next[idx] = { ...msg, blocks };
        return next;
      });
    }),
  replaceEphemeralProgress: (progress, conversationId, messageId) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findStreamingIndexForMessage(messages, messageId);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks = msg.blocks ? msg.blocks.slice() : [];
        const progressBlock = {
          ...progress,
          type: "progress" as const,
          timestamp: Date.now(),
        };
        // Replace the last ephemeral progress block in the same group
        const groupId = progress.groupId;
        if (groupId) {
          for (let i = blocks.length - 1; i >= 0; i--) {
            const block = blocks[i];
            if (
              block.type === "progress" &&
              block.groupId === groupId &&
              (block as unknown as Record<string, unknown>).ephemeral === true
            ) {
              blocks[i] = progressBlock;
              next[idx] = { ...msg, blocks };
              return next;
            }
          }
        }
        // No previous ephemeral found — fall back to ID-based upsert, then append
        const existingIdx = blocks.findIndex((block) =>
          block.type === "progress" && block.id === progress.id,
        );
        if (existingIdx >= 0) {
          blocks[existingIdx] = progressBlock;
        } else {
          blocks.push(progressBlock);
        }
        next[idx] = { ...msg, blocks };
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
        next[idx] = { ...baseMsg, blocks };
        return next;
      });
      const targetId = conversationId || s.conversationId;
      if (result.messages && (!targetId || targetId === s.conversationId)) {
        return { ...result, toolCallCount: s.toolCallCount + 1 };
      }
      return result;
    }),
  updateToolCall: (id, patch, conversationId, scope) =>
    set((s) => {
      const matchesScope = (record: ReturnType<typeof getToolCallsFromMessage>[number]) => {
        if (record.id !== id) return false;
        if (scope?.turnId && record.turnId && record.turnId !== scope.turnId) return false;
        if (scope?.iterationId && record.iterationId && record.iterationId !== scope.iterationId) return false;
        if (scope?.stepId && record.stepId && record.stepId !== scope.stepId) return false;
        return true;
      };
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = messages.findIndex((m) =>
          getToolCallsFromMessage(m).some(matchesScope),
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
  finishStreaming: (conversationId, usage, terminalStatus = "completed", messageId, failureMessage) =>
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
            let blocks = getContentBlocks(m);
            let content = baseMessage.content;
            const unsealedIndex = lastAutoFinalizableUnsealedIndex(blocks);
            if (unsealedIndex >= 0) {
              const unsealedBlock = blocks[unsealedIndex];
              if (unsealedBlock.type === "text") {
                content = unsealedBlock.content;
                blocks = blocks.map((block, index) =>
                  index === unsealedIndex && block.type === "text"
                    ? {
                        ...block,
                        source: terminalStatus === "completed" ? "model_final" : "partial",
                        visibility: "final",
                        phase: "final",
                      }
                    : block,
                );
              }
            }
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
              blocks: blocks.map((block) => {
                if (block.type === "tool_call" && (block.record.status === "running" || block.record.status === "pending")) {
                  // A tool call still running/pending at turn end never received
                  // its result (lost tool_result / missed terminal event). Do not
                  // fabricate "success". User interruption is partial rather
                  // than failed; other terminal paths remain failures.
                  return {
                    ...block,
                    record: {
                      ...block.record,
                      status: terminalStatus === "interrupted" ? "partial" as const : "failed" as const,
                      finishedAt,
                    },
                  };
                }
                if (block.type === "progress" && block.status === "running") {
                  const progressStatus = terminalStatus === "failed" ? "failed" as const : "completed" as const;
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
                    status: terminalStatus === "failed" ? "failed" as const : "completed" as const,
                    timestamp: finishedAt,
                  };
                }
                return block;
              }),
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
  resumeStreaming: (conversationId, toolCallsPending, messageId, turnId) =>
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

      let nextMessages: typeof sourceMessages;
      if (targetMessage) {
        nextMessages = sourceMessages.slice();
        const baseMessage = stripLegacyContentFields(targetMessage);
        nextMessages[targetIndex] = {
          ...baseMessage,
          content: "",
          ...(turnId ? { turnId } : {}),
          isStreaming: true,
          resumeState: "resumed",
          blocks: mergeResumeToolCalls(getContentBlocks(targetMessage), toolCallsPending),
        };
      } else {
        nextMessages = [
          ...sourceMessages,
          {
            id: targetMessageId || uniqueMessageId("resume"),
            ...(turnId ? { turnId } : {}),
            role: "assistant" as const,
            content: "",
            blocks: mergeResumeToolCalls([], toolCallsPending),
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
        toolCallCount: s.toolCallCount + (toolCallsPending?.length ?? 0),
        ...cacheMessagesForConversation(s, targetId, nextMessages, true),
      };
    }),
  replaceStreamingText: (conversationId, fullText, source, metadata, messageId) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findStreamingIndexForMessage(messages, messageId);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        if (source === "model_narration") {
          const targetSegmentId = metadata?.segmentId;
          const blocks: ContentBlock[] = (msg.blocks ? msg.blocks.slice() : []).filter((block) =>
            !(
              block.type === "text"
              && block.source === "model_narration"
              && block.sealed !== true
              && (!targetSegmentId || block.segmentId === targetSegmentId)
            ),
          );
          if (fullText) {
            blocks.push({ type: "text", content: fullText, source, ...metadata });
          }
          next[idx] = { ...msg, blocks };
          return next;
        }
        const blocks: ContentBlock[] = (msg.blocks ? msg.blocks.slice() : []).filter((block) =>
          block.type !== "text" || !isReplaceableTextBlock(block),
        );
        if (fullText) blocks.push({ type: "text", content: fullText, source: source || "stream", ...metadata });
        next[idx] = { ...msg, content: fullText, blocks };
        return next;
      });
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
  startSideChatMessage: (id, content) =>
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
                id: uniqueMessageId("su"),
                role: "user",
                content,
                artifacts: [],
                timestamp: t,
              },
              {
                id: uniqueMessageId("sa"),
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
