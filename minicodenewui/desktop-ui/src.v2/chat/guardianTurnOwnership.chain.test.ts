// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "../stores";
import { handleRuntimeEvent } from "./runtimeEvents";
import { handleGuardianEvent } from "./guardianEvents";
import type { GuardianAssessmentEvent } from "../protocol/guardian-types";
import type { ServerEvent } from "../protocol/events";

beforeEach(() => useAppStore.setState({ conversationId: "thread", messages: [
  { id: "assistant-projection", role: "assistant", content: "", timestamp: 1, isStreaming: true, blocks: [] },
], conversationMessages: {}, subagents: [], agentProgress: [], conversationAgentStates: {} }));

describe("issuing turn ownership from run start through Guardian live projection", () => {
  it.each(["runtime-turn", "explicit-host-turn"])("retains review on the exact issuing %s", turnId => {
    handleRuntimeEvent({ type: "agent.run.started", run_id: "runtime-turn", turn_id: turnId,
      conversation_id: "thread", message_id: "assistant-projection", role: "main", task_id: "task" } as unknown as ServerEvent, "thread");
    expect(useAppStore.getState().messages[0].turnId).toBe(turnId);
    const review: GuardianAssessmentEvent = { type: "guardian_assessment", id: "review", conversation_id: "thread",
      message_id: "assistant-projection", turn_id: turnId, started_at_ms: 1000, status: "in_progress",
      target_item_id: "patch", action: { type: "apply_patch", cwd: "/workspace", files: ["/workspace/actual.txt"] } };
    handleGuardianEvent(review);
    expect(useAppStore.getState().messages[0].blocks).toHaveLength(1);
    handleGuardianEvent({ ...review, id: "wrong-turn", turn_id: "assistant-projection" });
    expect(useAppStore.getState().messages[0].blocks).toHaveLength(1);
    handleGuardianEvent({ ...review, status: "denied", completed_at_ms: 2000, rationale: "Original review" });
    expect(useAppStore.getState().messages[0].blocks?.[0]).toMatchObject({ type: "guardian_assessment", review: { id: "review", status: "denied", turn_id: turnId } });
  });
});
