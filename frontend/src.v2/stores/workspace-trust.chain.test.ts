/* @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "./index";
import { pushToast } from "../overlays/ToastContainer";

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

beforeEach(() => {
  vi.clearAllMocks();
  useAppStore.setState({ conversationId: null, conversations: [{ id: "trust-owner", title: "Trust", updatedAt: "2026-10-09", workspaceRoot: "C:/workspace" }],
    workingDirectory: "", messages: [], conversationMessages: {}, conversationStreaming: {}, conversationWorkbenchStates: {},
    editorTabs: [], editorOpenRequests: [], conversationAgentStates: {}, isStreaming: false });
});
afterEach(() => { delete window.__MINICODE_RUNTIME__; });

it.each(["workspace", "conversation"])("shows a real desktop trust error at the %s request boundary", async (entry) => {
  const failure = new Error("EPERM: rename trusted_workspaces.json");
  const trust = vi.fn().mockRejectedValue(failure);
  window.__MINICODE_RUNTIME__ = { desktop: { trustWorkspace: trust } as never };
  if (entry === "workspace") useAppStore.getState().setWorkingDirectory("C:/workspace");
  else useAppStore.getState().applyConversationSwitched({ conversationId: "trust-owner" });
  await vi.waitFor(() => expect(pushToast).toHaveBeenCalledWith("无法信任工作区 C:/workspace：EPERM: rename trusted_workspaces.json", "error"));
  expect(trust).toHaveBeenCalledWith("C:/workspace");
  expect(useAppStore.getState().workingDirectory).toBe("C:/workspace");
});
