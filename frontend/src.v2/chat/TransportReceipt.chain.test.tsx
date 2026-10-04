// @vitest-environment jsdom
import { act } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ServerEvent } from "../protocol/events";
import type { ChatMessage } from "../stores/types";
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", {
  configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
}));
const mocks = vi.hoisted(() => ({ awaitResult: vi.fn(), send: vi.fn(() => true), toast: vi.fn() }));
vi.mock("../protocol/ws-outbox", async (load) => ({ ...await load<typeof import("../protocol/ws-outbox")>(),
  sendClientCommandAwaitResult: mocks.awaitResult, sendClientCommand: mocks.send,
}));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));
import { useAppStore } from "../stores";
import { clearEditorWorkspaceBufferCacheForTests } from "../stores/shared-helpers";
import { handleSessionEvent } from "./sessionEvents";
import { handleCommandResultEvent } from "./commandResultEvents";
import { registerWebSocketSender } from "../protocol/ws-outbox";

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const oldMessage: ChatMessage = { id: "older", role: "user", content: "Earlier input", timestamp: 1, artifacts: [] };
const recalled: ChatMessage = { id: "recalled", role: "user", content: "Question to edit", timestamp: 2, artifacts: [],
  contextRefs: [{ kind: "skill", name: "workflow", path: "C:/A/.minicode/skills/workflow/SKILL.md" }],
  attachmentRefs: [{ id: "file-ref", kind: "document", name: "file.txt", mediaType: "text/plain", sizeBytes: 5, artifactId: "artifact-owned", inputSource: "full_text", sourceCharCount: 5 }],
};
const buffers = { textStreamBuffer: { destroy: vi.fn(), flush: vi.fn(), push: vi.fn() }, thinkingStreamBuffer: { destroy: vi.fn(), flush: vi.fn(), push: vi.fn() } };
beforeEach(() => {
  vi.resetAllMocks();
  mocks.send.mockReturnValue(true);
  clearEditorWorkspaceBufferCacheForTests();
  localStorage.clear();
  useAppStore.setState({
    conversationId: "A", workingDirectory: "C:/A", conversations: [{ id: "A", title: "A", workspaceRoot: "C:/A", updatedAt: "2026-10-03" }],
    messages: [oldMessage, recalled], conversationMessages: {}, conversationStreaming: {}, conversationWorkbenchStates: {}, conversationAgentStates: {},
    conversationRecallTruncations: {}, isStreaming: false, draft: "", attachments: [], selectedMentions: [], selectedSkills: [], quotedMessage: null,
    availableSkills: [], runtimeCapabilities: null, editorTabs: [], activeTabPath: null,
  });
});

describe("transport receipt and authoritative projection order", () => {
  it.each([false, true])("restores the captured input after the server projects truncation first (new draft=%s)", async (newDraft) => {
    const receipt = deferred<{ level: string; command: string; message: string }>();
    mocks.awaitResult.mockReturnValue(receipt.promise);
    const pending = useAppStore.getState().recallMessage("recalled");
    handleSessionEvent({ type: "conversation.switched", conversation_id: "A", conversation: {
      id: "A", title: "A", workspace_root: "C:/A", revision: 2,
      messages: [{ id: "older", role: "user", content: "Earlier input", timestamp: 1 }],
    }, session: { active_conversation_id: "A", workspace_root: "C:/A" } } as ServerEvent, buffers);
    expect(useAppStore.getState().messages.map((item) => item.id)).toEqual(["older"]);
    if (newDraft) useAppStore.getState().setDraft("New input typed meanwhile");
    await act(async () => receipt.resolve({ level: "success", command: "conversation.truncate", message: "" }));
    expect(await pending).toBe(true);
    const state = useAppStore.getState();
    expect(state.messages.map((item) => item.id)).toEqual(["older"]);
    expect(state.draft).toBe(newDraft ? "New input typed meanwhile" : "Question to edit");
    if (!newDraft) {
      expect(state.attachments[0]).toMatchObject({ status: "ready", conversationId: "A", artifactId: "artifact-owned", inputSource: "full_text", sourceCharCount: 5 });
      expect(state.selectedSkills[0]).toMatchObject({ name: "workflow" });
    }
    expect(state.conversationRecallTruncations.A.removedIds).toContain("recalled");
  });

  it("shows a missing truncate receipt without deleting or replacing the input", async () => {
    mocks.awaitResult.mockRejectedValue(new Error("connection lost before result"));
    expect(await useAppStore.getState().recallMessage("recalled")).toBe(false);
    expect(useAppStore.getState().messages.map((item) => item.id)).toEqual(["older", "recalled"]);
    expect(mocks.toast).toHaveBeenCalledWith("connection lost before result", "error", 4000);
  });

  it.each(["conversation.worktree.cleanup", "conversation.worktree.handoff.execute"])("uses the editor/catalog workspace transition for %s", (command) => {
    useAppStore.setState({
      editorTabs: [{ id: "owned-A-buffer", path: "same.ts", content: "A draft", original: "A baseline", loading: false }], activeTabPath: "same.ts",
      availableSkills: [{ name: "A skill", description: "old scope" }], runtimeCapabilities: { skills: [{ name: "A skill" }] },
      selectedSkills: [{ name: "A skill" }],
    });
    handleCommandResultEvent({ type: "command.result", command, level: "success", message: "", data: {
      conversation_id: "A", workspace_root: "C:/B", removed: true, completed: true,
    } } as ServerEvent);
    expect(useAppStore.getState().workingDirectory).toBe("C:/B");
    expect(useAppStore.getState().editorTabs).toEqual([]);
    expect(useAppStore.getState().availableSkills).toEqual([]);
    expect(useAppStore.getState().selectedSkills).toEqual([]);
    useAppStore.getState().setWorkingDirectory("C:/A");
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ id: "owned-A-buffer", content: "A draft", original: "A baseline" });
  });

  it("keeps a late inactive worktree receipt out of the visible workspace", () => {
    useAppStore.setState({ conversationId: "B", workingDirectory: "C:/B" });
    handleCommandResultEvent({ type: "command.result", command: "conversation.worktree.handoff.execute", level: "success", message: "", data: {
      conversation_id: "A", workspace_root: "C:/new-A", completed: true,
    } } as ServerEvent);
    expect(useAppStore.getState().workingDirectory).toBe("C:/B");
    expect(useAppStore.getState().conversations.find((item) => item.id === "A")?.workspaceRoot).toBe("C:/new-A");
  });

  it("does not let a foreign or unowned command result open the active UI", () => {
    useAppStore.setState({ conversationId: "B", workingDirectory: "C:/B", settingsOpen: false, agentProgress: [] });
    for (const owner of [{ conversation_id: "A", workspace_root: "C:/A" }, {}]) {
      handleCommandResultEvent({ type: "command.result", command: "settings", level: "info", message: "Opening settings",
        ...owner, data: { ui_action: "open_settings:provider" } } as ServerEvent);
    }
    expect(useAppStore.getState().settingsOpen).toBe(false);
    expect(useAppStore.getState().agentProgress).toEqual([]);
    handleCommandResultEvent({ type: "command.result", command: "settings", level: "info", message: "Opening settings",
      conversation_id: "B", workspace_root: "C:/B", data: { ui_action: "open_settings:provider" } } as ServerEvent);
    expect(useAppStore.getState().settingsOpen).toBe(true);
  });

  it("settles the original awaited receipt before rejecting foreign UI projection", async () => {
    const { sendClientCommandAwaitResult } = await vi.importActual<typeof import("../protocol/ws-outbox")>("../protocol/ws-outbox");
    registerWebSocketSender(() => true);
    const receipt = sendClientCommandAwaitResult({ type: "runtime.capabilities.inspect", client_command_id: "cmd-origin-A" }, "runtime.capabilities.inspect", { silent: true });
    useAppStore.setState({ conversationId: "B", workingDirectory: "C:/B", settingsOpen: false });
    handleCommandResultEvent({ type: "command.result", command: "runtime.capabilities.inspect", level: "success", message: "",
      client_command_id: "cmd-origin-A", conversation_id: "A", workspace_root: "C:/A", data: { ui_action: "open_settings:provider" } } as ServerEvent);
    expect((await receipt).level).toBe("success");
    expect(useAppStore.getState().settingsOpen).toBe(false);
    registerWebSocketSender(null);
  });
});
