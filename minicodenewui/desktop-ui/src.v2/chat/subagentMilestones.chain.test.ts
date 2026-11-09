/* @vitest-environment jsdom */
import { beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { handleRuntimeEvent } from "./runtimeEvents";
import type { ServerEvent } from "../protocol/events";

vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true) }));
beforeEach(() => useAppStore.setState({ conversationId: "milestone-owner", subagents: [], messages: [],
  conversationMessages: {}, conversationAgentStates: {}, inspectorEntries: [] }));
const event = (payload: Record<string, unknown>) => handleRuntimeEvent({ conversation_id: "milestone-owner", ...payload } as unknown as ServerEvent);

it("keeps execution milestones through status refresh and clears a previous incarnation's completion", () => {
  event({ type: "subagent.start", subagent_id: "child", role: "explore", record: {
    agent_path: "/root/child", mailbox_epoch: 1, display_name: "Review", accepted_at: 100, started_at: 120,
    completed_at: null, workspace_root: "C:/isolated/child",
  } });
  expect(useAppStore.getState().subagents[0]).toMatchObject({ displayName: "Review", acceptedAt: 100, startedAt: 120, workspaceRoot: "C:/isolated/child" });
  event({ type: "subagent.done", subagent_id: "child", status: "completed", record: {
    agent_path: "/root/child", mailbox_epoch: 1, completed_at: 500,
  }, result: { summary: "Reviewed" } });
  expect(useAppStore.getState().subagents[0].completedAt).toBe(500);
  event({ type: "subagent.done", subagent_id: "child", status: "completed", record: { agent_path: "/root/child", mailbox_epoch: 1 }, result: { summary: "Reviewed" } });
  expect(useAppStore.getState().subagents[0].completedAt).toBe(500);
  event({ type: "subagent.start", subagent_id: "child", role: "explore", record: {
    agent_path: "/root/child", mailbox_epoch: 2, accepted_at: 600, started_at: 620, completed_at: null,
  } });
  expect(useAppStore.getState().subagents[0]).toMatchObject({ mailboxEpoch: 2, acceptedAt: 600, startedAt: 620 });
  expect(useAppStore.getState().subagents[0].completedAt).toBeUndefined();
});
