/* @vitest-environment jsdom */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import type { HeartbeatRendererStateCommand } from "../protocol/events";
import type { AppStore, ChatMessage } from "../stores/types";
import {
  createHeartbeatRendererObserver,
  heartbeatRendererFactsForConversation,
  projectHeartbeatRendererEligibility,
  type HeartbeatRendererFacts,
} from "./heartbeatRendererState";

const message = (patch: Partial<ChatMessage> = {}): ChatMessage => ({
  id: "answer", role: "assistant", content: "", artifacts: [], timestamp: 1,
  ...patch,
});
const facts = (patch: Partial<HeartbeatRendererFacts> = {}): HeartbeatRendererFacts => ({
  hasConversation: true, resumeState: "resumed", ...patch,
});
const snapshot = (patch: Partial<AppStore> = {}): AppStore => ({ ...useAppStore.getState(), ...patch });
const settle = async () => { await Promise.resolve(); };

beforeEach(() => {
  useAppStore.setState({
    conversationId: "a", messages: [], conversationMessages: {}, conversationHistoryPages: {},
    conversationHydration: {}, conversationStreaming: {}, isStreaming: false,
    pendingConversationSwitchId: null, pendingConversationCreateId: null,
    sideChats: {}, pendingAskUser: null, askUserQueue: [], pendingApproval: null, approvalQueue: [],
    pendingDiffReview: null, diffReviewQueue: [], runtimeSession: null,
  });
});

describe("Codex renderer eligibility precedence", () => {
  it.each([
    [{ hasConversation: false, supportedHost: false }, "missing_conversation"],
    [{ supportedHost: false, resumeState: "resuming" }, "unsupported_host"],
    [{ resumeState: "resuming", latestTurnId: "t", latestTurnStatus: "completed" }, "resuming"],
    [{ resumeState: "unobserved" }, "resuming"],
    [{ pendingRequestType: "userInput", latestTurnStatus: "inProgress" }, "waiting_on_user_input"],
    [{ pendingRequestType: "approval", latestTurnId: "t" }, "waiting_on_approval"],
    [{ pendingRequestType: "mcpServerElicitation" }, "waiting_on_approval"],
    [{ latestTurnId: "t" }, "missing_turn"],
    [{ latestTurnId: "t", latestTurnStatus: "inProgress" }, "turn_in_progress"],
    [{ pendingRequestType: "other", latestTurnStatus: "inProgress" }, "pending_request"],
  ] as Array<[Partial<HeartbeatRendererFacts>, string]>)("uses Source order for %o", (patch, reason) => {
    expect(projectHeartbeatRendererEligibility(facts(patch))).toEqual({ is_eligible: false, reason });
  });

  it.each(["completed", "failed", "interrupted", "partial"])("does not keep historical %s turns busy", status => {
    expect(projectHeartbeatRendererEligibility(facts({
      resumeState: "unobserved", latestTurnId: "t", latestTurnStatus: status,
    }))).toEqual({ is_eligible: true, reason: null });
  });

  it("permits an actually resumed empty conversation", () => {
    expect(projectHeartbeatRendererEligibility(facts())).toEqual({ is_eligible: true, reason: null });
  });
});

describe("per-conversation renderer facts", () => {
  it("does not turn an inventory, empty cache, or an unowned prompt into loaded facts", () => {
    const state = snapshot({
      conversations: [{ id: "b", title: "B", updatedAt: "2026-10-15" }],
      conversationMessages: { b: [] },
      pendingAskUser: { requestId: "unowned", question: "Question" },
      runtimeSession: { pending_approval_count: 12, pending_approvals: [] },
    });
    expect(heartbeatRendererFactsForConversation(state, "b", false, false)).toBeNull();
    expect(heartbeatRendererFactsForConversation(state, "missing", false, false)).toBeNull();
  });

  it("keeps prompts with their exact conversation and classifies MCP elicitation as approval", () => {
    const state = snapshot({
      conversationMessages: { b: [] },
      conversationHydration: { b: { isHydrating: false, updatedAt: 1 } },
      pendingAskUser: { requestId: "mcp", conversationId: "b", question: "Consent", elicitation: { mode: "url" } },
      askUserQueue: [{ requestId: "input", conversationId: "c", question: "Question" }],
    });
    expect(heartbeatRendererFactsForConversation(state, "a", false, true)?.pendingRequestType).toBeUndefined();
    expect(heartbeatRendererFactsForConversation(state, "b", false, false)?.pendingRequestType).toBe("mcpServerElicitation");
    expect(heartbeatRendererFactsForConversation(state, "c", false, false)?.pendingRequestType).toBe("userInput");
  });

  it("uses only explicitly owned runtime pending lanes and preserves unknown requests", () => {
    const state = snapshot({ runtimeSession: { pending_approval_count: 9, pending_approvals: [
      { request_id: "a-input", type: "control_request", subtype: "request_user_input", conversation_id: "a" },
      { request_id: "b-elicitation", type: "control_request", subtype: "elicitation", conversation_id: "b" },
      { request_id: "c-approval", type: "control_request", subtype: "can_use_tool", conversation_id: "c" },
      { request_id: "d-other", type: "control_request", subtype: "provider_auth", conversation_id: "d" },
      { request_id: "unowned", type: "approval_pending" },
    ] } });
    expect(heartbeatRendererFactsForConversation(state, "a", false, true)?.pendingRequestType).toBe("userInput");
    expect(heartbeatRendererFactsForConversation(state, "b", false, false)?.pendingRequestType).toBe("mcpServerElicitation");
    expect(heartbeatRendererFactsForConversation(state, "c", false, false)?.pendingRequestType).toBe("approval");
    expect(heartbeatRendererFactsForConversation(state, "d", false, false)?.pendingRequestType).toBe("other");
    expect(heartbeatRendererFactsForConversation(state, "e", false, false)).toBeNull();
  });

  it("does not invent a turn id or whole-turn completion from a completed text block", () => {
    const optimistic = snapshot({ messages: [message({ isStreaming: true })], isStreaming: true });
    expect(heartbeatRendererFactsForConversation(optimistic, "a", false, true)?.latestTurnId).toBeUndefined();
    const running = snapshot({ messages: [message({ turnId: "real-turn", isStreaming: true, blocks: [
      { type: "text", content: "Answer", status: "completed", isStreaming: false },
    ] })] });
    expect(heartbeatRendererFactsForConversation(running, "a", false, true)?.latestTurnStatus).toBe("inProgress");
    const missing = snapshot({ messages: [message({ turnId: "real-turn", isStreaming: false })] });
    expect(projectHeartbeatRendererEligibility(heartbeatRendererFactsForConversation(missing, "a", false, true)!))
      .toEqual({ is_eligible: false, reason: "missing_turn" });
  });

  it("does not revive a terminal receipt from an older runtime active-stream snapshot", () => {
    const state = snapshot({
      messages: [message({ turnId: "t", terminalStatus: "failed", isStreaming: false })],
      runtimeSession: { active_stream_conversation_ids: ["a"] }, isStreaming: false,
    });
    expect(heartbeatRendererFactsForConversation(state, "a", false, true)?.latestTurnStatus).toBe("failed");
  });

  it("leaves evicted inactive transcripts unknown", () => {
    const state = snapshot({ conversationHydration: { b: { isHydrating: false, updatedAt: 1 } } });
    expect(heartbeatRendererFactsForConversation(state, "b", false, true)).toBeNull();
  });
});

describe("ephemeral observer lifecycle", () => {
  it("coalesces a canonical projection and sends changes or explicit requests only", async () => {
    const send = vi.fn((_command: HeartbeatRendererStateCommand) => true);
    let restoring = true;
    const observer = createHeartbeatRendererObserver(useAppStore, () => restoring, send);
    observer.start();
    expect(send.mock.calls.at(-1)?.[0]).toEqual({
      type: "scheduler.heartbeat.renderer_state", conversation_id: "a", is_eligible: false, reason: "resuming",
    });
    restoring = false;
    observer.observe("a");
    send.mockClear();
    useAppStore.setState({ messages: [message({ turnId: "t", isStreaming: true })] });
    useAppStore.setState({ messages: [message({ turnId: "t", isStreaming: false, terminalStatus: "completed" })] });
    await settle();
    expect(send).not.toHaveBeenCalled();
    useAppStore.setState({ pendingAskUser: { requestId: "q", conversationId: "a", question: "Question" } });
    await settle();
    expect(send.mock.calls.at(-1)?.[0].reason).toBe("waiting_on_user_input");
    useAppStore.setState({ draft: "unsent", messages: [message({ turnId: "t", terminalStatus: "completed", content: "more text" })] });
    await settle();
    expect(send).toHaveBeenCalledTimes(1);
    observer.request("a");
    expect(send).toHaveBeenCalledTimes(2);
    observer.dispose();
    useAppStore.setState({ pendingAskUser: null });
    await settle();
    expect(send).toHaveBeenCalledTimes(2);
  });

  it("does not hydrate unknown requested targets or replay facts rejected by a closed socket", async () => {
    let connected = false;
    const send = vi.fn((_command: HeartbeatRendererStateCommand) => connected);
    const observer = createHeartbeatRendererObserver(useAppStore, () => false, send);
    observer.observe("a");
    observer.start();
    observer.request("unloaded");
    expect(send.mock.calls.every(([command]) => command.conversation_id === "a")).toBe(true);
    connected = true;
    useAppStore.setState({ pendingApproval: { requestId: "p", toolName: "exec", args: {}, conversationId: "a" } });
    await settle();
    expect(send.mock.calls.at(-1)?.[0].reason).toBe("waiting_on_approval");
    expect(useAppStore.getState().pendingConversationSwitchId).toBeNull();
    observer.dispose();
  });
});
