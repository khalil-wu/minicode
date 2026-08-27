import type { StateCreator } from "zustand";
import type {
  AppStore,
  CheckpointProjectionRecord,
  ControlPlaneSlice,
} from "./types";

const MAX_CHECKPOINTS_PER_CONVERSATION = 50;

const checkpointTimestamp = (checkpoint: CheckpointProjectionRecord): number => {
  const parsed = Date.parse(checkpoint.createdAt);
  return Number.isFinite(parsed) ? parsed : 0;
};

export const createControlPlaneSlice: StateCreator<AppStore, [], [], ControlPlaneSlice> = (set) => ({
  conversationHydration: {},
  permissionRulesByConversation: {},
  checkpointsByConversation: {},
  runCheckpointsByConversation: {},
  checkpointResumeByConversation: {},
  guidelineReloadsByConversation: {},
  providerOAuthFlowsByConversation: {},
  recentWorkspaces: [],
  setConversationHydration: (conversationId, isHydrating, updatedAt = Date.now()) => {
    const owner = conversationId.trim();
    if (!owner) return;
    set((state) => ({
      conversationHydration: {
        ...state.conversationHydration,
        [owner]: { isHydrating, updatedAt },
      },
    }));
  },
  setPermissionRulesProjection: (projection) => {
    const owner = projection.conversationId.trim();
    if (!owner) return;
    set((state) => ({
      permissionRulesByConversation: {
        ...state.permissionRulesByConversation,
        [owner]: { ...projection, conversationId: owner },
      },
    }));
  },
  recordCheckpointProjection: (checkpoint, updatedAt = Date.now()) => {
    const owner = checkpoint.conversationId.trim();
    if (!owner || !checkpoint.id.trim()) return;
    set((state) => {
      const existing = state.checkpointsByConversation[owner];
      const checkpoints = [
        checkpoint,
        ...(existing?.checkpoints ?? []).filter((candidate) => candidate.id !== checkpoint.id),
      ]
        .sort((left, right) => checkpointTimestamp(right) - checkpointTimestamp(left))
        .slice(0, MAX_CHECKPOINTS_PER_CONVERSATION);
      return {
        checkpointsByConversation: {
          ...state.checkpointsByConversation,
          [owner]: {
            conversationId: owner,
            workspaceRoot: checkpoint.workspaceRoot || existing?.workspaceRoot || "",
            checkpoints,
            updatedAt,
          },
        },
      };
    });
  },
  setCheckpointCollectionProjection: (projection) => {
    const owner = projection.conversationId.trim();
    if (!owner) return;
    set((state) => ({
      checkpointsByConversation: {
        ...state.checkpointsByConversation,
        [owner]: {
          ...projection,
          conversationId: owner,
          checkpoints: projection.checkpoints
            .slice()
            .sort((left, right) => checkpointTimestamp(right) - checkpointTimestamp(left))
            .slice(0, MAX_CHECKPOINTS_PER_CONVERSATION),
        },
      },
    }));
  },
  setRunCheckpointCollectionProjection: (projection) => {
    const owner = projection.conversationId.trim();
    if (!owner) return;
    set((state) => ({
      runCheckpointsByConversation: {
        ...state.runCheckpointsByConversation,
        [owner]: { ...projection, conversationId: owner },
      },
    }));
  },
  setCheckpointResumeProjection: (projection) => {
    const owner = projection.conversationId.trim();
    if (!owner) return;
    set((state) => ({
      checkpointResumeByConversation: {
        ...state.checkpointResumeByConversation,
        [owner]: { ...projection, conversationId: owner },
      },
    }));
  },
  setGuidelinesReloadProjection: (projection) => {
    const owner = projection.conversationId.trim();
    if (!owner) return;
    set((state) => ({
      guidelineReloadsByConversation: {
        ...state.guidelineReloadsByConversation,
        [owner]: { ...projection, conversationId: owner },
      },
    }));
  },
  setProviderOAuthFlow: (projection) => {
    const owner = projection.conversationId.trim();
    const provider = projection.provider.trim();
    if (!owner || !provider) return;
    set((state) => {
      const existing = state.providerOAuthFlowsByConversation[owner]?.[provider];
      if (
        existing?.eventSeq !== undefined
        && projection.eventSeq !== undefined
        && projection.eventSeq < existing.eventSeq
      ) return state;
      if (
        existing
        && (
          existing.eventSeq === undefined
          || projection.eventSeq === undefined
          || projection.eventSeq === existing.eventSeq
        )
        && projection.updatedAt < existing.updatedAt
      ) return state;
      return {
        providerOAuthFlowsByConversation: {
          ...state.providerOAuthFlowsByConversation,
          [owner]: {
            ...(state.providerOAuthFlowsByConversation[owner] ?? {}),
            [provider]: {
              ...projection,
              conversationId: owner,
              provider,
            },
          },
        },
      };
    });
  },
  clearProviderOAuthFlow: (conversationId, provider) => {
    const owner = conversationId.trim();
    const normalizedProvider = provider?.trim();
    if (!owner) return;
    set((state) => {
      if (!state.providerOAuthFlowsByConversation[owner]) return state;
      const providerOAuthFlowsByConversation = { ...state.providerOAuthFlowsByConversation };
      if (!normalizedProvider) {
        delete providerOAuthFlowsByConversation[owner];
      } else {
        const flows = { ...providerOAuthFlowsByConversation[owner] };
        delete flows[normalizedProvider];
        if (Object.keys(flows).length > 0) providerOAuthFlowsByConversation[owner] = flows;
        else delete providerOAuthFlowsByConversation[owner];
      }
      return { providerOAuthFlowsByConversation };
    });
  },
  setRecentWorkspaces: (workspaces) => set({
    recentWorkspaces: workspaces
      .filter((workspace) => Boolean(workspace.path.trim()))
      .sort((left, right) => right.lastOpened - left.lastOpened)
      .slice(0, 20),
  }),
  clearConversationControlPlaneState: (conversationId) => {
    const owner = conversationId.trim();
    if (!owner) return;
    set((state) => {
      const conversationHydration = { ...state.conversationHydration };
      const permissionRulesByConversation = { ...state.permissionRulesByConversation };
      const checkpointsByConversation = { ...state.checkpointsByConversation };
      const runCheckpointsByConversation = { ...state.runCheckpointsByConversation };
      const checkpointResumeByConversation = { ...state.checkpointResumeByConversation };
      const guidelineReloadsByConversation = { ...state.guidelineReloadsByConversation };
      const providerOAuthFlowsByConversation = { ...state.providerOAuthFlowsByConversation };
      delete conversationHydration[owner];
      delete permissionRulesByConversation[owner];
      delete checkpointsByConversation[owner];
      delete runCheckpointsByConversation[owner];
      delete checkpointResumeByConversation[owner];
      delete guidelineReloadsByConversation[owner];
      delete providerOAuthFlowsByConversation[owner];
      return {
        conversationHydration,
        permissionRulesByConversation,
        checkpointsByConversation,
        runCheckpointsByConversation,
        checkpointResumeByConversation,
        guidelineReloadsByConversation,
        providerOAuthFlowsByConversation,
      };
    });
  },
});
