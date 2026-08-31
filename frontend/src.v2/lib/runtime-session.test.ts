import { describe, expect, it } from "vitest";
import {
  hasRuntimePendingUserAction,
  hasRuntimePendingUserActionForConversation,
  runtimePendingUserActionLabel,
  runtimePendingUserActionLabelForConversation,
} from "./runtime-session";

describe("hasRuntimePendingUserAction", () => {
  it("uses pending approval count when present", () => {
    expect(hasRuntimePendingUserAction({ pending_approval_count: 1 })).toBe(true);
    expect(hasRuntimePendingUserAction({ pending_approval_count: 0 })).toBe(false);
  });

  it("falls back to pending approval detail list", () => {
    expect(hasRuntimePendingUserAction({
      pending_approvals: [{ request_id: "ask-1", type: "control_request", subtype: "elicitation" }],
    })).toBe(true);
  });

  it("handles missing runtime session", () => {
    expect(hasRuntimePendingUserAction(null)).toBe(false);
    expect(hasRuntimePendingUserAction(undefined)).toBe(false);
  });
});

describe("runtimePendingUserActionLabel", () => {
  it("describes restored ask-user prompts", () => {
    expect(runtimePendingUserActionLabel({
      pending_approval_count: 1,
      pending_approvals: [{ request_id: "ask-1", type: "control_request", subtype: "elicitation" }],
    })).toBe("等待回复");
  });

  it("describes control-protocol elicitation prompts as replies", () => {
    expect(runtimePendingUserActionLabel({
      pending_approvals: [{ request_id: "ctrl-ask", type: "control_request", subtype: "elicitation" }],
    })).toBe("等待回复");
  });

  it("describes restored tool approval prompts", () => {
    expect(runtimePendingUserActionLabel({
      pending_approvals: [{
        request_id: "approval-1",
        type: "control_request",
        subtype: "can_use_tool",
        tool_name: "write_file",
      }],
    })).toBe("等待 write_file");
  });

  it("falls back to a generic approval label when only a count is available", () => {
    expect(runtimePendingUserActionLabel({ pending_approval_count: 2 })).toBe("等待批准");
  });
});

describe("conversation-scoped runtime pending prompts", () => {
  it("matches pending prompts to the conversation that owns them", () => {
    const runtimeSession = {
      active_conversation_id: "conv-active",
      pending_approval_count: 1,
      pending_approvals: [{
        request_id: "ask-inactive",
        type: "control_request",
        subtype: "elicitation",
        conversation_id: "conv-inactive",
      }],
    };

    expect(hasRuntimePendingUserActionForConversation(runtimeSession, "conv-inactive")).toBe(true);
    expect(runtimePendingUserActionLabelForConversation(runtimeSession, "conv-inactive")).toBe("等待回复");
    expect(hasRuntimePendingUserActionForConversation(runtimeSession, "conv-active")).toBe(false);
    expect(runtimePendingUserActionLabelForConversation(runtimeSession, "conv-active")).toBeNull();
  });

  it("keeps unscoped pending prompts on the active conversation", () => {
    const runtimeSession = {
      active_conversation_id: "conv-active",
      pending_approval_count: 1,
      pending_approvals: [{
        request_id: "approval-1",
        type: "control_request",
        subtype: "can_use_tool",
        tool_name: "write_file",
      }],
    };

    expect(hasRuntimePendingUserActionForConversation(runtimeSession, "conv-active")).toBe(true);
    expect(runtimePendingUserActionLabelForConversation(runtimeSession, "conv-active")).toBe("等待 write_file");
    expect(hasRuntimePendingUserActionForConversation(runtimeSession, "conv-other")).toBe(false);
  });
});
