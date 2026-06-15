import { useAppStore } from "../stores";
import type {
  ConversationListEvent,
  ConversationRecordPayload,
  ConversationSummaryPayload,
  ConversationSwitchedEvent,
  GoalInfo,
  GoalUpdatedEvent,
  LlmModelUpdatedEvent,
  RuntimeSessionSnapshot,
  ServerEvent,
  SessionRestoredEvent,
  SessionSyncedEvent,
} from "../protocol/events";
import type { StreamBuffer } from "../lib/stream-buffer";
import { pushToast } from "../overlays/ToastContainer";
import { hydrateMessages, type BackendTranscriptMessage } from "./transcriptHydration";
import { clearStreamingState } from "./streamingState";
import type { ChatMessage } from "../stores/types";
import { toConversationGoal } from "../stores/types";
import { fromBackendPermissionMode } from "../protocol/permissions";
import { conversationResetPayload } from "../stores/shared-helpers";

type ConversationSummary = ConversationSummaryPayload;
type ConversationPayload = ConversationRecordPayload;

const maybeString = (value: string | null | undefined): string | undefined =>
  typeof value === "string" && value ? value : undefined;

const stringValue = (value: string | null | undefined): string =>
  maybeString(value) ?? "";

const toConversationMeta = (conversation: ConversationSummary) => ({
  id: conversation.id,
  title: maybeString(conversation.title) ?? "Untitled",
  updatedAt: maybeString(conversation.updated_at) ?? new Date().toISOString(),
  archived: conversation.archived,
  workspaceRoot: maybeString(conversation.workspace_root),
  gitBranch: maybeString(conversation.git_branch),
  worktreePath: maybeString(conversation.worktree_path),
  gitIsolated: conversation.git_isolated,
  goal: toConversationGoal(conversation.goal),
});

const upsertConversationMeta = (conversation: ConversationSummary) => {
  useAppStore.setState((state) => {
    const meta = toConversationMeta(conversation);
    return {
      conversations: [
        meta,
        ...state.conversations.filter((item) => item.id !== meta.id),
      ],
    };
  });
};

const hydrateActiveConversation = (
  conversation: ConversationPayload | null | undefined,
  activeConversationId?: string,
  fallbackMessages?: BackendTranscriptMessage[],
  options: { upsertMeta?: boolean; preserveStreamingDraft?: boolean } = {},
) => {
  const conversationId = maybeString(activeConversationId) || conversation?.id || "";
  if (!conversationId) return;

  if (conversation && options.upsertMeta !== false) {
    upsertConversationMeta(conversation);
  }
  useAppStore.getState().applyConversationSwitched({ conversationId });
  if (conversation) {
    useAppStore.getState().setActiveGoal(toConversationGoal(conversation.goal), conversationId);
  }

  const transcript = conversation?.messages ?? conversation?.transcript ?? fallbackMessages;
  if (transcript) {
    const hydrated = hydrateMessages(transcript);
    const messages = options.preserveStreamingDraft
      ? mergeHydratedWithStreamingDrafts(conversationId, hydrated)
      : hydrated;
    useAppStore.getState().hydrateConversationMessages(
      conversationId,
      messages,
      { activate: true, isStreaming: messages.some((message) => Boolean(message.isStreaming)) },
    );
  }

  const workspaceRoot = maybeString(conversation?.worktree_path) || maybeString(conversation?.workspace_root);
  if (conversation) {
    useAppStore.getState().setWorkingDirectory(workspaceRoot || "");
  }
};

const mergeHydratedWithStreamingDrafts = (
  conversationId: string,
  hydratedMessages: ChatMessage[],
): ChatMessage[] => {
  const state = useAppStore.getState();
  const cachedMessages = state.conversationMessages[conversationId]
    ?? (state.conversationId === conversationId ? state.messages : []);
  const streamingDrafts = cachedMessages.filter(
    (message) => message.role === "assistant" && Boolean(message.isStreaming),
  );
  if (!streamingDrafts.length) return hydratedMessages;

  const hydratedIds = new Set(hydratedMessages.map((message) => message.id));
  const preservedDrafts = streamingDrafts.filter((message) => !hydratedIds.has(message.id));
  if (!preservedDrafts.length) return hydratedMessages;
  return [...hydratedMessages, ...preservedDrafts];
};

const activeConversationWorkspace = (): string => {
  const state = useAppStore.getState();
  const active = state.conversations.find((conversation) => conversation.id === state.conversationId);
  return active?.worktreePath || active?.workspaceRoot || "";
};

const clearActiveConversationView = () => {
  useAppStore.getState().setWorkingDirectory("");
  useAppStore.setState({
    ...conversationResetPayload(),
    conversationId: null,
    activeGoal: null,
    messages: [],
    isStreaming: false,
    toolCallCount: 0,
  });
};

const applyRuntimeSessionSnapshot = (session: RuntimeSessionSnapshot | undefined | null) => {
  if (!session) return;
  useAppStore.getState().setRuntimeSession(session);
  if (session.permission_mode) {
    useAppStore.setState({ permissionMode: fromBackendPermissionMode(session.permission_mode) });
  }
};

export const handleSessionEvent = (
  e: ServerEvent,
  buffers: { textStreamBuffer: StreamBuffer; thinkingStreamBuffer: StreamBuffer },
): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "llm.model.updated": {
      const ev = e as LlmModelUpdatedEvent;
      const model = stringValue(ev.current_model) || stringValue(ev.model);
      if (model) s.setCurrentModel(model);
      if (ev.provider) s.setCurrentProvider(ev.provider);
      if (ev.available_models) s.setAvailableModels(ev.available_models);
      const workingDirectory = maybeString(ev.working_directory);
      if (workingDirectory && !activeConversationWorkspace()) {
        s.setWorkingDirectory(workingDirectory);
      }
      return true;
    }
    case "session.restored":
    case "session.synced": {
      const ev = e as SessionRestoredEvent | SessionSyncedEvent;
      const model = stringValue(ev.current_model)
        || stringValue(ev.model)
        || stringValue(ev.session?.selected_model);
      const workspaceRoot = stringValue(ev.working_directory)
        || stringValue(ev.workspace_root)
        || stringValue(ev.workspace?.root_path)
        || stringValue(ev.session?.workspace_root);
      const restoredConversation = ev.type === "session.restored" ? ev.conversation : null;
      const fallbackMessages = ev.type === "session.restored" ? ev.messages : undefined;
      const activeConversation = ev.active_conversation ?? ev.session?.active_conversation ?? restoredConversation ?? null;
      const activeConversationId = stringValue(ev.active_conversation_id)
        || stringValue(ev.session?.active_conversation_id)
        || activeConversation?.id
        || "";
      const switchEventWillHydrate = ev.type === "session.restored" && ev.conversation_switched_follows === true;
      if (switchEventWillHydrate) {
        buffers.textStreamBuffer.destroy();
        buffers.thinkingStreamBuffer.destroy();
      } else {
        clearStreamingState(buffers, { conversationId: activeConversationId || s.conversationId });
      }

      if (model) s.setCurrentModel(model);
      const provider = maybeString(ev.provider);
      if (provider) s.setCurrentProvider(provider);
      if (workspaceRoot && !switchEventWillHydrate) s.setWorkingDirectory(workspaceRoot);
      if (ev.available_models) s.setAvailableModels(ev.available_models);
      if (switchEventWillHydrate) {
        // The backend will immediately emit the canonical conversation.switched
        // event. Keep conversation activation on that single path so restore and
        // manual switching cannot diverge.
      } else if (activeConversationId) {
        hydrateActiveConversation(activeConversation, activeConversationId, fallbackMessages);
      } else {
        clearActiveConversationView();
      }
      applyRuntimeSessionSnapshot(ev.session);
      if (ev.type === "session.restored" && ev.error) {
        pushToast(`Session restore warning: ${ev.error}`, "warning", 5000);
      }
      return true;
    }
    case "conversation.list": {
      const ev = e as ConversationListEvent;
      if (ev.conversations) {
        const conversationMetas = ev.conversations.map(toConversationMeta);
        const activeConversationId = maybeString(ev.active_conversation_id);
        const storeState = useAppStore.getState();
        const pendingLocalActive = !activeConversationId && storeState.conversationId
          ? storeState.conversations.find((conversation) =>
              conversation.id === storeState.conversationId &&
              conversation.title === "New chat" &&
              !conversationMetas.some((item) => item.id === conversation.id) &&
              storeState.messages.length === 0
            )
          : undefined;
        const nextConversationMetas = pendingLocalActive
          ? [pendingLocalActive, ...conversationMetas]
          : conversationMetas;
        const knownConversationIds = new Set(nextConversationMetas.map((conversation) => conversation.id));
        useAppStore.setState((state) => ({
          conversations: nextConversationMetas,
          conversationMessages: Object.fromEntries(
            Object.entries(state.conversationMessages).filter(([id]) => knownConversationIds.has(id)),
          ),
          conversationStreaming: Object.fromEntries(
            Object.entries(state.conversationStreaming).filter(([id]) => knownConversationIds.has(id)),
          ),
        }));
        const activeConversation = ev.active_conversation?.id === activeConversationId
          ? ev.active_conversation
          : null;
        if (activeConversationId) {
          hydrateActiveConversation(activeConversation, activeConversationId, undefined, { upsertMeta: false });
        } else if (pendingLocalActive) {
          // A freshly-created optimistic conversation can coexist with a stale
          // list response that was already in flight. Keep the blank local
          // conversation visible until the create response reconciles it.
        } else {
          clearActiveConversationView();
        }
        applyRuntimeSessionSnapshot(ev.session);
      }
      return true;
    }
    case "goal.updated": {
      const ev = e as GoalUpdatedEvent & { goal?: GoalInfo | null };
      useAppStore.getState().setActiveGoal(toConversationGoal(ev.goal), maybeString(ev.conversation_id));
      return true;
    }
    case "conversation.switched": {
      const ev = e as ConversationSwitchedEvent;
      if (ev.conversation) {
        const nextConversationId = maybeString(ev.conversation_id) ?? ev.conversation.id;
        hydrateActiveConversation(ev.conversation, nextConversationId, undefined, { preserveStreamingDraft: true });
      }
      applyRuntimeSessionSnapshot(ev.session);
      return true;
    }
    default:
      return false;
  }
};
