import { pendingPromptTargetsConversation } from "../lib/pending-prompts";
import type { HeartbeatRendererStateCommand } from "../protocol/events";
import type { AppStore, ChatMessage } from "../stores/types";

export interface HeartbeatRendererFacts {
  hasConversation: boolean;
  /** The execution host owns this fact; an omitted value is checked there. */
  supportedHost?: boolean;
  resumeState: "resuming" | "resumed" | "unobserved";
  latestTurnId?: string;
  latestTurnStatus?: string;
  pendingRequestType?: "userInput" | "approval" | "mcpServerElicitation" | "other";
}

type Eligibility = Pick<HeartbeatRendererStateCommand, "is_eligible" | "reason">;

/** Installed Codex pqo precedence. Host support remains a backend fact. */
export const projectHeartbeatRendererEligibility = (facts: HeartbeatRendererFacts): Eligibility => {
  const blocked = (reason: string): Eligibility => ({ is_eligible: false, reason });
  if (!facts.hasConversation) return blocked("missing_conversation");
  if (facts.supportedHost === false) return blocked("unsupported_host");
  if (facts.resumeState === "resuming"
    || (facts.resumeState !== "resumed" && !facts.latestTurnId)) return blocked("resuming");
  if (facts.pendingRequestType === "userInput") return blocked("waiting_on_user_input");
  if (facts.pendingRequestType === "approval" || facts.pendingRequestType === "mcpServerElicitation") {
    return blocked("waiting_on_approval");
  }
  if (!facts.pendingRequestType) {
    if (facts.latestTurnId && !facts.latestTurnStatus) return blocked("missing_turn");
    if (facts.latestTurnStatus === "inProgress") return blocked("turn_in_progress");
    return { is_eligible: true, reason: null };
  }
  return blocked("pending_request");
};

const latestTurnMessage = (messages: ChatMessage[]): ChatMessage | undefined => {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === "assistant" && message.turnId && !message.queueState) return message;
  }
  return undefined;
};

const pendingRequestForConversation = (
  state: AppStore,
  conversationId: string,
): HeartbeatRendererFacts["pendingRequestType"] => {
  const asks = [state.pendingAskUser, ...state.askUserQueue]
    .filter(prompt => pendingPromptTargetsConversation(prompt, conversationId));
  const runtime = (state.runtimeSession?.pending_approvals ?? [])
    .filter(prompt => prompt.conversation_id === conversationId);
  if (asks.some(prompt => !prompt?.elicitation)
    || runtime.some(prompt => prompt.subtype === "request_user_input" || prompt.type === "ask_user")) {
    return "userInput";
  }
  if (asks.some(prompt => prompt?.elicitation)
    || runtime.some(prompt => prompt.subtype === "elicitation")) return "mcpServerElicitation";
  if ([state.pendingApproval, ...state.approvalQueue, state.pendingDiffReview, ...state.diffReviewQueue]
    .some(prompt => pendingPromptTargetsConversation(prompt, conversationId))
    || runtime.some(prompt => ["can_use_tool", "request_permissions"].includes(prompt.subtype ?? "")
      || prompt.type === "approval_request")) return "approval";
  return runtime.length ? "other" : undefined;
};

export const heartbeatRendererFactsForConversation = (
  state: AppStore,
  conversationId: string,
  isRestoring: boolean,
  observed: boolean,
): HeartbeatRendererFacts | null => {
  const active = state.conversationId === conversationId;
  const hydration = state.conversationHydration[conversationId];
  const messages = active ? state.messages
    : state.sideChats[conversationId]?.messages ?? state.conversationMessages[conversationId];
  const latest = latestTurnMessage(messages ?? []);
  const pendingRequestType = pendingRequestForConversation(state, conversationId);
  const streaming = active ? state.isStreaming
    : state.sideChats[conversationId]?.isStreaming ?? state.conversationStreaming[conversationId];
  // A terminal receipt clears this projection before the following runtime
  // snapshot arrives. An older snapshot must not revive that finished turn.
  const live = state.runtimeSession?.active_stream_conversation_ids?.includes(conversationId) === true
    && streaming !== false;
  // Inventories and empty message caches do not establish a loaded thread.
  // A request may authorize observation, but it does not hydrate its target.
  const hasTranscript = messages !== undefined && (observed || hydration !== undefined
    || state.conversationHistoryPages[conversationId] !== undefined);
  if (!active && !hasTranscript && !latest && !pendingRequestType && !live
    && hydration?.isHydrating !== true) return null;
  const resuming = isRestoring || hydration?.isHydrating === true
    || state.pendingConversationSwitchId === conversationId
    || state.pendingConversationCreateId === conversationId;
  const resumed = messages !== undefined && (observed || hydration?.isHydrating === false);
  return {
    hasConversation: true,
    resumeState: resuming ? "resuming" : resumed ? "resumed" : "unobserved",
    latestTurnId: latest?.turnId,
    latestTurnStatus: live ? "inProgress" : latest?.terminalStatus
      ?? (latest?.isStreaming ? "inProgress" : undefined),
    pendingRequestType,
  };
};

interface RendererStore {
  getState: () => AppStore;
  subscribe: (listener: (state: AppStore, previous: AppStore) => void) => () => void;
}

export const createHeartbeatRendererObserver = (
  store: RendererStore,
  isRestoring: () => boolean,
  send: (command: HeartbeatRendererStateCommand) => boolean,
) => {
  const targets = new Set<string>();
  const observed = new Set<string>();
  const sent = new Map<string, string>();
  let started = false;
  let refreshQueued = false;

  const refresh = (forceTarget?: string): void => {
    if (!started) return;
    const state = store.getState();
    if (state.conversationId) targets.add(state.conversationId);
    for (const conversationId of targets) {
      const facts = heartbeatRendererFactsForConversation(state, conversationId, isRestoring(), observed.has(conversationId));
      if (!facts) {
        sent.delete(conversationId);
        continue;
      }
      const eligibility = projectHeartbeatRendererEligibility(facts);
      const serialized = JSON.stringify(eligibility);
      if (conversationId !== forceTarget && sent.get(conversationId) === serialized) continue;
      if (send({ type: "scheduler.heartbeat.renderer_state", conversation_id: conversationId, ...eligibility })) {
        sent.set(conversationId, serialized);
      }
    }
  };

  const unsubscribe = store.subscribe((state, previous) => {
    if (state.conversationId === previous.conversationId
      && state.pendingConversationSwitchId === previous.pendingConversationSwitchId
      && state.pendingConversationCreateId === previous.pendingConversationCreateId
      && state.conversationHydration === previous.conversationHydration
      && state.messages === previous.messages
      && state.conversationMessages === previous.conversationMessages
      && state.isStreaming === previous.isStreaming
      && state.conversationStreaming === previous.conversationStreaming
      && state.sideChats === previous.sideChats
      && state.pendingAskUser === previous.pendingAskUser && state.askUserQueue === previous.askUserQueue
      && state.pendingApproval === previous.pendingApproval && state.approvalQueue === previous.approvalQueue
      && state.pendingDiffReview === previous.pendingDiffReview && state.diffReviewQueue === previous.diffReviewQueue
      && state.runtimeSession === previous.runtimeSession) return;
    if (!refreshQueued) {
      refreshQueued = true;
      // A canonical event can update several slices. Publish its completed
      // projection, rather than an intermediate snapshot between reducers.
      queueMicrotask(() => { refreshQueued = false; refresh(); });
    }
  });

  return {
    start: () => { started = true; refresh(); },
    refresh,
    observe: (conversationId: string) => {
      targets.add(conversationId);
      observed.add(conversationId);
      refresh();
    },
    request: (conversationId: string) => {
      targets.add(conversationId);
      refresh(conversationId);
    },
    dispose: () => { started = false; unsubscribe(); },
  };
};
