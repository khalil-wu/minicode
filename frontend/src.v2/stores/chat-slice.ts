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

export const createChatSlice: StateCreator<AppStore, [], [], ChatSlice> = (set, get) => ({
  conversationId: null,
  conversations: [],
  activeGoal: null,
  messages: [],
  conversationMessages: {},
  conversationStreaming: {},
  isStreaming: false,
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
    const targetConversation = get().conversations.find((c) => c.id === id);
    const targetWorkspace = conversationWorkspacePath(targetConversation);
    set((s) => {
      const cachedCurrent = cacheMessagesForConversation(s, s.conversationId);
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
    const id = newConversationId();
    const shouldBindWorkspace = Boolean(options?.bindWorkspace || options?.workspaceRoot);
    const workspaceRoot = shouldBindWorkspace
      ? canonicalWorkspacePath(options?.workspaceRoot ?? state.workingDirectory)
      : "";
    const currentWorkspaceRoot = canonicalWorkspacePath(state.workingDirectory);
    const canUseCurrentWorkspaceGit = Boolean(workspaceRoot && workspaceRoot === currentWorkspaceRoot);
    sendClientCommand({
      type: "conversation.create",
      conversation_id: id,
      title: "New chat",
      workspace_root: workspaceRoot || undefined,
      permission_mode: toBackendPermissionMode(state.permissionMode),
    });
    set((s) => {
      const cachedCurrent = cacheMessagesForConversation(s, s.conversationId);
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
        appMode: "cowork" as const,
        workingDirectory: workspaceRoot,
        workspaceGit: workspaceRoot !== s.workingDirectory ? null : s.workspaceGit,
        ...(workspaceRoot !== s.workingDirectory ? editorStateForWorkspace(workspaceRoot) : {}),
        conversationMessages: {
          ...cachedCurrent.conversationMessages,
          [id]: [],
        },
        conversationStreaming: {
          ...cachedCurrent.conversationStreaming,
          [id]: false,
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
  },
  removeConversation: (id) => {
    sendClientCommand({ type: "conversation.delete", conversation_id: id });
    const state = get();
    const remaining = state.conversations.filter((conversation) => conversation.id !== id);
    const conversationMessages = Object.fromEntries(
      Object.entries(state.conversationMessages).filter(([key]) => key !== id),
    );
    const conversationStreaming = Object.fromEntries(
      Object.entries(state.conversationStreaming).filter(([key]) => key !== id),
    );

    if (state.conversationId !== id) {
      set({ conversations: remaining, conversationMessages, conversationStreaming });
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
      conversations: remaining,
      conversationMessages,
      conversationStreaming,
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
  appendTextChunk: (content, conversationId) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findLastStreamingIndex(messages);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks = msg.blocks ? msg.blocks.slice() : [];
        const last = blocks[blocks.length - 1];
        if (last && last.type === "text") {
          blocks[blocks.length - 1] = { ...last, content: last.content + content };
        } else {
          blocks.push({ type: "text", content });
        }
        next[idx] = { ...msg, content: msg.content + content, isThinkingStreaming: false, blocks };
        return next;
      });
    }),
  setFinalAnswerStreaming: (conversationId, isStreaming) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findLastStreamingIndex(messages);
        if (idx < 0) return null;
        const next = messages.slice();
        next[idx] = { ...next[idx], isStreaming };
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
  appendProgress: (progress, conversationId) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findLastStreamingIndex(messages);
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
          conversationMessages: { ...s.conversationMessages, [targetId]: nextMessages },
          conversationStreaming: { ...s.conversationStreaming, [targetId]: true },
        };
      }
      return {
        messages: nextMessages,
        isStreaming: true,
        toolCallCount: s.toolCallCount + (toolCallsPending?.length ?? 0),
        ...cacheMessagesForConversation(s, targetId, nextMessages, true),
      };
    }),
  replaceStreamingText: (conversationId, fullText) =>
    set((s) => {
      return updateMessagesForConversation(s, conversationId, (messages) => {
        const idx = findLastStreamingIndex(messages);
        if (idx < 0) return null;
        const next = messages.slice();
        const msg = next[idx];
        const blocks: ContentBlock[] = (msg.blocks ? msg.blocks.slice() : []).filter((block) => block.type !== "text");
        if (fullText) blocks.push({ type: "text", content: fullText });
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
