import type { AppStore, ConversationWorkbenchState } from "../stores/types";
import { normalizeArtifactContentState } from "./artifact-projection";

type PreviewFields = Pick<
  ConversationWorkbenchState,
  | "previewArtifact"
  | "livePreviewUrl"
  | "previewServers"
  | "previewLaunchConfigs"
  | "previewLaunchProcesses"
  | "previewVerification"
>;

export type PreviewProjection = PreviewFields & {
  conversationId?: string;
};

const emptyPreviewFields = (): PreviewFields => ({
  previewArtifact: null,
  livePreviewUrl: null,
  previewServers: [],
  previewLaunchConfigs: [],
  previewLaunchProcesses: [],
  previewVerification: null,
});

const normalizedConversationId = (value: unknown): string =>
  typeof value === "string" ? value.trim() : "";

const previewFieldsFromWorkbench = (state: ConversationWorkbenchState): PreviewFields => ({
  // A stored null is meaningful. It prevents a conversation from inheriting a
  // previous conversation's open file while the right panel is still mounted.
  previewArtifact: state.previewArtifact ? normalizeArtifactContentState(state.previewArtifact) : null,
  livePreviewUrl: state.livePreviewUrl ?? null,
  previewServers: state.previewServers ?? [],
  previewLaunchConfigs: state.previewLaunchConfigs ?? [],
  previewLaunchProcesses: state.previewLaunchProcesses ?? [],
  previewVerification: state.previewVerification ?? null,
});

const previewFieldsFromActiveStore = (state: AppStore): PreviewFields => ({
  previewArtifact: state.previewArtifact ? normalizeArtifactContentState(state.previewArtifact) : null,
  livePreviewUrl: state.livePreviewUrl,
  previewServers: state.previewServers,
  previewLaunchConfigs: state.previewLaunchConfigs,
  previewLaunchProcesses: state.previewLaunchProcesses,
  previewVerification: state.previewVerification,
});

/**
 * Select preview state for one concrete conversation without crossing an
 * owner boundary. The legacy global fields remain the active-conversation
 * mirror until that conversation has its first scoped snapshot.
 */
export const selectPreviewForConversation = (
  state: AppStore,
  conversationId?: string | null,
): PreviewProjection => {
  const targetId = normalizedConversationId(conversationId);
  const stored = targetId
    ? state.conversationWorkbenchStates?.[targetId]
    : undefined;
  if (stored) {
    return {
      conversationId: targetId || undefined,
      ...previewFieldsFromWorkbench(stored),
    };
  }
  if (!targetId || targetId === normalizedConversationId(state.conversationId)) {
    return {
      conversationId: targetId || undefined,
      ...previewFieldsFromActiveStore(state),
    };
  }
  return {
    conversationId: targetId || undefined,
    ...emptyPreviewFields(),
  };
};

/** The preview surface can intentionally belong to a side-chat conversation. */
export const selectPreviewSurface = (state: AppStore): PreviewProjection => {
  const ownerId = normalizedConversationId(
    state.previewOwnerConversationId || state.conversationId,
  );
  return selectPreviewForConversation(state, ownerId || undefined);
};

/** Activity and artifacts are always scoped to the primary active conversation. */
export const selectActiveConversationPreview = (state: AppStore): PreviewProjection =>
  selectPreviewForConversation(state, state.conversationId);
