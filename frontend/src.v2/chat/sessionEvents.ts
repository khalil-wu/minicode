import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";
import type { StreamBuffer } from "../lib/stream-buffer";
import { pushToast } from "../overlays/ToastContainer";
import { hydrateMessages, type BackendTranscriptMessage } from "./transcriptHydration";
import { clearStreamingState } from "./streamingState";

type ConversationSummary = {
  id: string;
  title?: string;
  updated_at?: string;
  archived?: boolean;
  workspace_root?: string;
  git_branch?: string;
  worktree_path?: string;
  git_isolated?: boolean;
};

type ConversationPayload = ConversationSummary & {
  messages?: BackendTranscriptMessage[];
  transcript?: BackendTranscriptMessage[];
};

const toConversationMeta = (conversation: ConversationSummary) => ({
  id: conversation.id,
  title: conversation.title ?? "Untitled",
  updatedAt: conversation.updated_at ?? new Date().toISOString(),
  archived: conversation.archived,
  workspaceRoot: conversation.workspace_root,
  gitBranch: conversation.git_branch,
  worktreePath: conversation.worktree_path,
  gitIsolated: conversation.git_isolated,
});

export const handleSessionEvent = (
  e: ServerEvent,
  buffers: { textStreamBuffer: StreamBuffer; thinkingStreamBuffer: StreamBuffer },
): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "llm.model.updated": {
      const ev = e as unknown as {
        model?: string;
        available_models?: string[];
        current_model?: string;
        provider?: string;
        working_directory?: string;
      };
      const model = ev.current_model || ev.model || "";
      if (model) s.setCurrentModel(model);
      if (ev.provider) s.setCurrentProvider(ev.provider);
      if (ev.available_models) s.setAvailableModels(ev.available_models);
      if (ev.working_directory) s.setWorkingDirectory(ev.working_directory);
      return true;
    }
    case "session.restored":
    case "session.synced": {
      clearStreamingState(buffers);
      const ev = e as unknown as {
        model?: string;
        provider?: string;
        working_directory?: string;
        available_models?: string[];
        error?: string;
      };
      if (ev.model) s.setCurrentModel(ev.model);
      if (ev.provider) s.setCurrentProvider(ev.provider);
      if (ev.working_directory) s.setWorkingDirectory(ev.working_directory);
      if (ev.available_models) s.setAvailableModels(ev.available_models);
      if (ev.error) pushToast(`Session restore warning: ${ev.error}`, "warning", 5000);
      return true;
    }
    case "conversation.list": {
      const ev = e as unknown as {
        conversations?: ConversationSummary[];
        active_conversation_id?: string;
        active_conversation?: ConversationPayload | null;
      };
      if (ev.conversations) {
        const activeConversation = ev.active_conversation ?? null;
        const activeTranscript = activeConversation?.messages ?? activeConversation?.transcript;
        useAppStore.setState({ conversations: ev.conversations.map(toConversationMeta) });
        if (ev.active_conversation_id) {
          useAppStore.getState().switchConversation(ev.active_conversation_id);
        }
        if (activeConversation && activeTranscript && activeConversation.id === ev.active_conversation_id) {
          useAppStore.getState().hydrateConversationMessages(
            activeConversation.id,
            hydrateMessages(activeTranscript),
            { activate: true, isStreaming: false },
          );
          if (activeConversation.workspace_root) {
            useAppStore.getState().setWorkingDirectory(activeConversation.workspace_root);
          }
        }
      }
      return true;
    }
    case "conversation.switched": {
      const ev = e as unknown as {
        conversation_id?: string;
        conversation?: ConversationPayload;
      };
      if (ev.conversation) {
        const nextConversationId = ev.conversation_id ?? ev.conversation.id;
        const meta = toConversationMeta(ev.conversation);
        const transcript = ev.conversation.messages ?? ev.conversation.transcript;
        const nextWorkspace = ev.conversation.workspace_root;
        if (nextWorkspace) {
          useAppStore.getState().setWorkingDirectory(nextWorkspace);
        }
        if (transcript) {
          useAppStore.setState((state) => ({
            conversations: [
              meta,
              ...state.conversations.filter((c) => c.id !== meta.id),
            ],
          }));
          useAppStore.getState().hydrateConversationMessages(nextConversationId, hydrateMessages(transcript), {
            activate: true,
            isStreaming: false,
          });
        } else {
          useAppStore.setState((state) => ({
            conversationId: nextConversationId,
            messages: state.conversationMessages[nextConversationId] ?? [],
            isStreaming: state.conversationStreaming[nextConversationId] ?? false,
            conversations: [
              meta,
              ...state.conversations.filter((c) => c.id !== meta.id),
            ],
          }));
        }
      }
      return true;
    }
    default:
      return false;
  }
};
