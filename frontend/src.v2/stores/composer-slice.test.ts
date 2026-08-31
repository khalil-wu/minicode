import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(() => true),
  sendClientCommandAwaitResult: vi.fn(async (_command, expectedCommand) => ({
    type: "command.result",
    command: expectedCommand,
    level: "success",
    message: "",
    data: {},
  })),
  commandResultSucceeded: (event: { level?: string }) => {
    const level = String(event.level || "").toLowerCase();
    return level !== "error" && level !== "failed";
  },
}));

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: vi.fn(),
}));

import { sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import { useAppStore } from "./index";

describe("composer permission mode", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      conversationId: "conv-active",
      permissionMode: "auto",
      pendingApproval: {
        requestId: "approval-1",
        protocol: "control",
        toolName: "arbitrary_tool",
        args: {},
      },
      approvalQueue: [
        { requestId: "approval-2", toolName: "another_tool", args: {} },
      ],
      pendingDiffReview: { requestId: "diff-1", protocol: "control", diff: "patch" },
      diffReview: {
        requestId: "diff-1",
        protocol: "control",
        diff: "patch",
        files: [],
        status: "pending",
        fileDecisions: {},
      },
    });
  });

  it.each(["bypass", "auto", "plan"] as const)(
    "syncs %s without deciding pending approvals in the frontend",
    (mode) => {
      useAppStore.getState().setPermissionMode(mode);

      expect(sendClientCommandAwaitResult).toHaveBeenCalledTimes(1);
      expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
        type: "conversation.permission_mode.set",
        mode,
        source: "frontend.ui",
        conversation_id: "conv-active",
      }, "conversation.permission_mode.set");
      const state = useAppStore.getState();
      expect(state.permissionMode).toBe("auto");
      expect(state.pendingApproval?.requestId).toBe("approval-1");
      expect(state.approvalQueue.map((approval) => approval.requestId)).toEqual(["approval-2"]);
      expect(state.pendingDiffReview?.requestId).toBe("diff-1");
      expect(state.diffReview?.requestId).toBe("diff-1");
    },
  );

  // Regression: `sendClientCommandAwaitResult` *resolves* with an error-level
  // command.result instead of rejecting, so the old `.catch(() => undefined)`
  // never saw a refusal. The user accepted the 完全访问 danger dialog and got no
  // feedback at all while the pill silently stayed on the previous mode.
  it("surfaces a backend refusal of a permission mode change", async () => {
    vi.mocked(sendClientCommandAwaitResult).mockResolvedValueOnce({
      type: "command.result",
      command: "conversation.permission_mode.set",
      level: "error",
      message: "工作区不可信，禁止完全访问",
      data: {},
    } as never);

    useAppStore.getState().setPermissionMode("bypass");
    await vi.waitFor(() => expect(pushToast).toHaveBeenCalled());

    expect(pushToast).toHaveBeenCalledWith(
      "切换权限模式失败：工作区不可信，禁止完全访问",
      "error",
      6000,
    );
    expect(useAppStore.getState().permissionMode).toBe("auto");
  });

  it("surfaces a transport failure of a permission mode change", async () => {
    vi.mocked(sendClientCommandAwaitResult).mockRejectedValueOnce(new Error("操作超时"));

    useAppStore.getState().setPermissionMode("bypass");
    await vi.waitFor(() => expect(pushToast).toHaveBeenCalled());

    expect(pushToast).toHaveBeenCalledWith("切换权限模式失败：操作超时", "error", 6000);
  });

  it("stays silent when the backend accepts a permission mode change", async () => {
    useAppStore.getState().setPermissionMode("plan");
    await vi.waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalled());
    await Promise.resolve();

    expect(pushToast).not.toHaveBeenCalled();
  });

  it("surfaces a backend refusal of a reasoning effort change", async () => {
    vi.mocked(sendClientCommandAwaitResult).mockResolvedValueOnce({
      type: "command.result",
      command: "effort",
      level: "error",
      message: "当前模型不支持 minimal",
      data: {},
    } as never);

    useAppStore.getState().setEffortLevel("minimal");
    await vi.waitFor(() => expect(pushToast).toHaveBeenCalled());

    expect(pushToast).toHaveBeenCalledWith(
      "切换推理强度失败：当前模型不支持 minimal",
      "error",
      6000,
    );
  });
});
