import { beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "../stores";
import { handleRuntimeEvent } from "./runtimeEvents";
import { handleSessionEvent } from "./sessionEvents";
import type { ServerEvent } from "../protocol/events";

beforeEach(() => useAppStore.setState({
  conversationId: "child-state-owner", messages: [], inspectorEntries: [],
  subagents: [{ id: "child-live", role: "subagent", status: "running", currentTool: "list_files",
    waitingOn: "tool", currentActivity: "Listing files", activityLog: ["Listing files"] }],
}));

describe("source child model state", () => {
  it("clears the previous tool and reads the real next iteration without showing connection prose", () => {
    handleRuntimeEvent({
      type: "subagent.progress", conversation_id: "child-state-owner", subagent_id: "child-live",
      iteration: 2, status: "running", tool_name: "", waiting_on: "model", user_visible: false,
      current_activity: "create scene", activity_kind: "provider", source_event_type: "runtime.span",
      snapshot: { current_tool: "", waiting_on: "model", iteration: 2 },
    } as unknown as ServerEvent);
    const child = useAppStore.getState().subagents[0];
    expect(child.currentTool).toBe("");
    expect(child.waitingOn).toBe("model");
    expect(child.iteration).toBe(2);
    expect(child.currentActivity).toBe("create scene");
    expect(child.activityLog).toEqual(["Listing files"]);
  });

  it("restores explicit empty current tool, model wait, and real progress time on conversation reload", () => {
    const buffer = { push() {}, flush() {}, destroy() {} };
    handleSessionEvent({
      type: "conversation.switched", conversation_id: "child-state-owner",
      conversation: { id: "child-state-owner", title: "Scene", updated_at: "2026-10-09T11:00:00Z", messages: [],
        context_snapshot: { ui_agent_state: { subagents: [{ id: "child-live", role: "subagent", status: "running",
          current_tool: "", waiting_on: "model", iteration: 2, last_progress_at: 4567 }] } } },
    } as unknown as ServerEvent, { textStreamBuffer: buffer, thinkingStreamBuffer: buffer });
    const child = useAppStore.getState().subagents[0];
    expect(child.currentTool).toBe("");
    expect(child.waitingOn).toBe("model");
    expect(child.iteration).toBe(2);
    expect(child.lastProgressAt).toBe(4567);
  });
});
