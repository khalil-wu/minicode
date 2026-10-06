import { beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";
import { handleRuntimeEvent } from "./runtimeEvents";
import { subagentModelPatch } from "../lib/subagent-model";

vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

beforeEach(() => useAppStore.setState({
  conversationId: "model-owner", currentModel: "unrelated-parent-model", currentProvider: "custom",
  subagents: [], conversationAgentStates: {}, inspectorEntries: [], messages: [],
}));
const receive = (payload: Record<string, unknown>) => handleRuntimeEvent({
  conversation_id: "model-owner", subagent_id: "child-model", agent_path: "/root/review", mailbox_epoch: 1, ...payload,
} as unknown as ServerEvent, "model-owner");
const child = () => useAppStore.getState().subagents.find((agent) => agent.id === "child-model")!;

it("uses the actual child metadata through live events and a terminal status refresh", () => {
  receive({ type: "subagent.start", role: "reviewer", prompt: "Review", model: "actual-child", provider: "child-service", reasoning_effort: "high" });
  expect(child()).toMatchObject({ model: "actual-child", provider: "child-service", reasoningEffort: "high" });
  receive({ type: "subagent.progress", detail: "working" });
  expect(child().model).toBe("actual-child");
  receive({ type: "subagent.progress", model: "next-step-model", provider: "child-service", reasoning_effort: "off" });
  receive({ type: "subagent.done", status: "completed", summary: "done", result: { content: "report" } });
  expect(child()).toMatchObject({ status: "done", model: "next-step-model", reasoningEffort: "off" });
  receive({ type: "subagent.done", status: "completed", summary: "done", result: { content: "report" },
    snapshot: { model: "confirmed-model", provider: "confirmed-service", reasoning_effort: "medium" } });
  expect(child()).toMatchObject({ model: "confirmed-model", provider: "confirmed-service", reasoningEffort: "medium" });
});

it("clears previous execution metadata for a new unknown incarnation and rejects stale model events", () => {
  receive({ type: "subagent.start", prompt: "first", model: "old-child", provider: "old-service", reasoning_effort: "high" });
  receive({ type: "subagent.done", status: "completed", result: { content: "first result" } });
  receive({ type: "subagent.start", prompt: "continue", mailbox_epoch: 2 });
  expect(child()).toMatchObject({ mailboxEpoch: 2, model: undefined, provider: undefined, reasoningEffort: undefined });
  receive({ type: "subagent.progress", model: "stale-child", provider: "stale-service", reasoning_effort: "high" });
  expect(child().model).toBeUndefined();
  receive({ type: "subagent.progress", mailbox_epoch: 2, model: "resolved-child", provider: "resolved-service", reasoning_effort: "low" });
  expect(child().model).toBe("resolved-child");
  receive({ type: "subagent.progress", mailbox_epoch: 2, model: "", provider: "", reasoning_effort: "" });
  expect(child().model).toBeUndefined();
  expect(child().provider).toBeUndefined();
});

it("hydrates camel or wire metadata without manufacturing values for legacy records", () => {
  expect(subagentModelPatch({ model: "saved-child", provider: "saved-service", reasoningEffort: "high" }))
    .toEqual({ model: "saved-child", provider: "saved-service", reasoningEffort: "high" });
  expect(subagentModelPatch({ summary: "legacy child" })).toEqual({});
});
