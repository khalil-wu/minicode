import { useAppStore } from "../stores";
import { composerWorkspaceKey } from "../stores/composer-drafts";
import { getDiffCommentScope, transferDiffComments } from "../lib/diff-comment-storage";

/** Bind the optimistic first turn to the conversation created by the backend. */
export const adoptGeneratedConversation = (conversationId?: string, messageId?: string, admittedUserMessageId?: string) => {
  const targetId = conversationId?.trim();
  const intentId = messageId?.trim();
  if (!targetId || !intentId) return;
  let adoptedWorkspace: string | undefined;
  useAppStore.setState((state) => {
    if (state.conversationId || state.sideChats[targetId]) return state;
    // A nil foreground owner is not authority to adopt an old owner's event.
    // The backend echoes the first send's optimistic assistant id. A restore
    // may have cleared its streaming flag before the admitted run starts.
    const intent = state.messages.find((message) => message.role === "assistant" && message.id === intentId);
    const admittedInput = admittedUserMessageId && state.messages.some(message => message.role === "user" && message.id === admittedUserMessageId);
    if (!admittedInput && (!intent || intent.terminalStatus || intent.completedAt != null || intent.queueState === "queued")) return state;
    adoptedWorkspace = state.workingDirectory;
    const messages = state.messages;
    const streaming = admittedInput ? messages.some(message => message.isStreaming || message.isThinkingStreaming) : true;
    const conversations = state.conversations.some((conversation) => conversation.id === targetId)
      ? state.conversations
      : [{ id: targetId, title: "新会话", updatedAt: new Date().toISOString() }, ...state.conversations];
    const workspaceComposerDrafts = { ...state.workspaceComposerDrafts };
    delete workspaceComposerDrafts[composerWorkspaceKey(state.workingDirectory)];
    return {
      workspaceComposerDrafts,
      conversationId: targetId,
      conversations,
      conversationMessages: { ...state.conversationMessages, [targetId]: messages },
      conversationStreaming: { ...state.conversationStreaming, [targetId]: streaming },
      isStreaming: streaming,
    };
  });
  if (adoptedWorkspace !== undefined) transferDiffComments(getDiffCommentScope(null, adoptedWorkspace), getDiffCommentScope(targetId, adoptedWorkspace));
};
