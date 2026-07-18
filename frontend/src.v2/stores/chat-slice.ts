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
  findStreamingTargetIndex,
  computeToolCallCount,
  normalizeThinkingMetadata,
  thinkingMetadataMatches,
  mergeResumeToolCalls,
  conversationWorkspacePath,
  editorStateForWorkspace,
  conversationResetPayload,
  ensureCodePanelSlots,
  persistPanelSlots,
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
  return (
    (block.source || "stream") === source &&
    block.visibility === metadata?.visibility &&
    block.role === metadata?.role &&
    block.phase === metadata?.phase
  );
}

function isTimelineTextSource(source: string | undefined): boolean {
  return source === "model_preamble" || source === "post_tool" || source === "runtime";
}

function isTimelineTextMetadata(
  source: string | undefined,
  metadata?: Partial<Omit<TextBlock, "type" | "content" | "source">>,
): boolean {
  return (
    metadata?.visibility === "timeline" ||
    metadata?.visibility === "debug" ||
    metadata?.role === "runtime" ||
    isTimelineTextSource(source)
  );
}

function isReplaceableTextBlock(block: TextBlock): boolean {
  return !isTimelineTextMetadata(block.source, block);
}

export const createChatSlice: StateCreator<AppStore, [], [], ChatSlice> = (set, get) => ({
  conversationId: null,
  conversations: [],
  activeGoal: null,
  messages: [],
  conversationMessages: {},
  conversationStreaming: {},
  isStreaming: false,
  isPaused: false,
  isConnected: false,
  lastUsage: null,
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
  recallMessage: (id) =>
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
        attachment: {
          id: attachment.id,
          kind: attachment.kind,
          file_name: attachment.name,
          media_type: attachment.mediaType,
          artifact_id: attachment.artifactId,
          doc_id: attachment.docId,
          indexed_chunks: attachment.indexedChunks ?? 0,
          size_bytes: attachment.sizeBytes ?? 0,
        },
      }));
      const removedMessages = s.messages.slice(turnStart);
      const nextStreaming = removedMessages.some((message) => message.isStreaming) ? false : s.isStreaming;
      const restoredDraft = recalled.role === "user"
        ? stripDisplayContextSuffix(recalled.content, contextRefs)
        : s.draft;
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
        ...cacheMessagesForConversation(s, s.conversationId, nextMessages, nextStreaming),
      };
    }),
  removeEmptyStreamingAssistant: (conversationId) =>
    set((s) => {
      if (conversationId && s.sideChats[conversationId]) {
        const thread = s.sideChats[conversationId];
        return {
          sideChats: {
            ...s.sideChats,
            [conversationId]: {
              ...thread,
              messages: thread.messages.filter((m) => !isStructurallyEmptyAssistantMessage(m)),
            },
          },
        };
      }
      const targetId = conversationId || s.conversationId || undefined;
      const isActive = !targetId || targetId === s.conversationId;
      const sourceMessages = targetId && !isActive
        ? s.conversationMessages[targetId] ?? []
        : s.messages;
      const nextMessages = sourceMessages.filter((m) => !isStructurallyEmptyAssistantMessage(m));
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
    set({ isPaused: true });
    sendClientCommand({ type: "pause_streaming" });
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
    get().restoreAgentState(id);
    get().restoreWorkbenchState(id);
    if (targetWorkspace) {
      const rt = typeof window !== "undefined"
        ? (window as any).__MINICODE_RUNTIME__?.desktop
        : undefined;
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
          },
        },
        runtimeSession: null,
        draft: "",
        attachments: [],
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

    if (state.conversationId !== id) {
      set({
        conversations: remaining,
        conversationMessages,
        conversationStreaming,
        conversationAgentStates,
        conversationWorkbenchStates,
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
        conversationId: nextActive.id,
        activeGoal: nextActive.goal ?? null,
        messages: targetMessages,
        isStreaming: conversationStreaming[nextActive.id] ?? false,
        toolCallCount: computeToolCallCount(targetMessages),
        runtimeSession: null,
        draft: "",
        attachments: [],
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
      conversationId: null,
      activeGoal: null,
      messages: [],
      isStreaming: false,
      toolCallCount: 0,
      ...(hasCodeContext ? { appMode: "code" as const, panelSlots } : {}),
    });
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
      const nextStreaming = options?.isStreaming ?? messages.some((message) => message.isStreaming);
      const cachedCurrent = activate
        ? cacheMessagesForConversation(s, s.conversationId)
        : {
            conversationMessages: s.conversationMessages,
            conversationStreaming: s.conversationStreaming,
          };
      return {
        ...(activate ? { conversationId: id, messages, isStreaming: nextStreaming, toolCallCount: computeToolCallCount(messages) } : {}),
        conversationMessages: {
          ...cachedCurrent.conversationMessages,
          [id]: messages,
        },
        conversationStreaming: {
          ...cachedCurrent.conversationStreaming,
          [id]: nextStreaming,
        },
      };
    }),
  appendTextChunk: (content, conversationId, source, metadata) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findLastStreamingIndex(messages);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks = msg.blocks ? msg.blocks.slice() : [];
        const last = blocks[blocks.length - 1];
        const nextSource = source || "stream";
        const contributesToAnswer = !isTimelineTextMetadata(nextSource, metadata);
        if (last && last.type === "text" && textMetadataMatches(last, nextSource, metadata)) {
          // Preserve the existing attribution when appending to an open text
          // block — only freshly opened blocks carry an explicit source (e.g.
          // the "send_message" BriefTool reply), so a streaming reply is not
          // relabeled by a later chunk.
          blocks[blocks.length - 1] = { ...last, content: last.content + content };
        } else {
          blocks.push({ type: "text", content, source: nextSource, ...metadata });
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
  setFinalAnswerAttachments: (conversationId, attachments) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findLastStreamingIndex(messages);
        if (idx < 0) return null;
        const next = messages.slice();
        next[idx] = { ...next[idx], replyAttachments: attachments };
        return next;
      });
    }),
  finalizeStreamingText: (conversationId, source, metadata) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findLastStreamingIndex(messages);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks: ContentBlock[] = msg.blocks ? msg.blocks.slice() : [];
        // Re-tag the last text block as the final answer in place — the content
        // was already streamed live; this only updates attribution so the
        // projection routes it as the committed final answer.
        let tagged = false;
        for (let i = blocks.length - 1; i >= 0; i -= 1) {
          const block = blocks[i];
          if (block.type === "text" && isReplaceableTextBlock(block)) {
            blocks[i] = {
              ...block,
              source: source || "model_final",
              visibility: metadata?.visibility || "final",
              phase: metadata?.phase || "final",
              ...(metadata?.role ? { role: metadata.role } : {}),
            } as ContentBlock;
            tagged = true;
            break;
          }
        }
        if (!tagged) return null;
        next[idx] = { ...msg, blocks };
        return next;
      });
    }),
  appendThinkingChunk: (content, conversationId, metadata) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findLastStreamingIndex(messages);
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
        const idx = findStreamingTargetIndex(messages, messageId);
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
        next[idx] = { ...msg, isThinkingStreaming: item.status === "running", blocks };
        return next;
      });
    }),
  appendProgress: (progress, conversationId, messageId) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findStreamingTargetIndex(messages, messageId);
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
        const idx = findStreamingTargetIndex(messages, messageId);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks = msg.blocks ? msg.blocks.slice() : [];
        const progressBlock = {
          ...progress,
          type: "progress" as const,
          timestamp: Date.now(),
        };
        // Ephemeral progress with the same groupId is a rolling status line —
        // replace in place instead of accumulating history.
        const existingIdx = blocks.findIndex((block) =>
          block.type === "progress" &&
          (block.id === progress.id || (Boolean(progress.groupId) && block.ephemeral === true && block.groupId === progress.groupId)),
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
  appendToolCallBlock: (tc, conversationId) =>
    set((s) => {
      const result = updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findLastStreamingIndex(messages);
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
  finishStreaming: (conversationId, usage, terminalStatus = "completed") =>
    set((s) => {
      const finishedAt = Date.now();
      return updateMessagesForConversation(
        s,
        conversationId,
        (messages) => messages.map((m) => {
          if (!m.isStreaming) return m;
          const baseMessage = stripLegacyContentFields(m);
          return {
            ...baseMessage,
            isStreaming: false,
            isThinkingStreaming: false,
            resumeState: undefined,
            terminalStatus,
            usage,
            completedAt: finishedAt,
            blocks: getContentBlocks(m).map((block) => {
              if (block.type === "tool_call" && (block.record.status === "running" || block.record.status === "pending")) {
                return {
                  ...block,
                  record: {
                    ...block.record,
                    status: terminalStatus === "failed" ? "failed" as const : "success" as const,
                    finishedAt,
                  },
                };
              }
              if (block.type === "progress" && block.status === "running") {
                return {
                  ...block,
                  status: terminalStatus === "failed" ? "failed" as const : "completed" as const,
                  label: terminalStatus === "interrupted" ? "\u5DF2\u4E2D\u65AD" : block.label,
                  summary: terminalStatus === "interrupted" ? "\u7528\u6237\u5DF2\u4E2D\u65AD" : block.summary,
                  timestamp: finishedAt,
                };
              }
              return block;
            }),
          };
        }),
        false,
      );
    }),
  resumeStreaming: (conversationId, toolCallsPending) =>
    set((s) => {
      const targetId = conversationId || s.conversationId;
      const sourceMessages = (targetId && s.conversationMessages[targetId]) || s.messages;
      const lastIdx = sourceMessages.length - 1;
      const lastMsg = lastIdx >= 0 ? sourceMessages[lastIdx] : null;

      let nextMessages: typeof sourceMessages;
      if (lastMsg && lastMsg.role === "assistant") {
        nextMessages = sourceMessages.slice();
        const baseLastMsg = stripLegacyContentFields(lastMsg);
        nextMessages[lastIdx] = {
          ...baseLastMsg,
          content: "",
          isStreaming: true,
          resumeState: "resumed",
          blocks: mergeResumeToolCalls(getContentBlocks(lastMsg), toolCallsPending),
        };
      } else {
        nextMessages = [
          ...sourceMessages,
          {
            id: uniqueMessageId("resume"),
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
  replaceStreamingText: (conversationId, fullText, source, metadata) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findLastStreamingIndex(messages);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks: ContentBlock[] = (msg.blocks ? msg.blocks.slice() : []).filter((block) =>
          block.type !== "text" || !isReplaceableTextBlock(block),
        );
        if (fullText) blocks.push({ type: "text", content: fullText, source: source || "stream", ...metadata });
        next[idx] = { ...msg, content: fullText, blocks };
        return next;
      });
    }),
  setConnected: (c) => set({ isConnected: c }),
  setLastUsage: (u) => set({ lastUsage: u }),
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
