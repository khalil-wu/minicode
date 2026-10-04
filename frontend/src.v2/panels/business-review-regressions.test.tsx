// @vitest-environment jsdom
import React from "react";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { DiffPanel } from "./DiffPanel";
import { EditorPanel } from "./EditorPanel";
import { useAgentEditReview } from "./useAgentEditReview";

const seams = vi.hoisted(() => ({ send: vi.fn(), awaitSend: vi.fn(), confirm: vi.fn(), toast: vi.fn(), write: vi.fn() }));
vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: seams.send,
  sendClientCommandAwaitResult: seams.awaitSend, sendPromptResponseCommand: vi.fn(),
  commandResultSucceeded: (r: any) => r?.level === "info",
}));
vi.mock("../overlays/DialogService", () => ({ showConfirm: seams.confirm }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: seams.toast }));
vi.mock("../lib/monaco-colorize", () => ({ useColorizedLines: () => null, extractFilePathFromDiff: () => "a.txt", guessLanguageFromPath: () => "plaintext" }));
vi.mock("../lib/file-icons", () => ({ fileIcon: () => null, fileGlyphColor: () => "inherit" }));
vi.mock("../desktop/runtime", () => ({ isDesktop: () => false, fsReadFileInfo: vi.fn(), fsSearchFiles: vi.fn(), revealPath: vi.fn() }));
vi.mock("../protocol/workspace", () => ({ compareWriteWorkspaceFile: seams.write, readWorkspaceFile: vi.fn(), searchWorkspaceFiles: vi.fn() }));
vi.mock("monaco-editor/editor/editor.worker?worker", () => ({ default: class {} }));
vi.mock("monaco-editor/editor/editor.api.js", () => ({}));
vi.mock("@monaco-editor/react", () => ({ default: () => <div>Editor</div>, loader: { config: vi.fn() } }));

const patch = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n";
const original = useAppStore.getState();
beforeEach(() => {
  vi.clearAllMocks(); seams.send.mockReturnValue(true);
  seams.awaitSend.mockResolvedValue({ type: "command.result", level: "info", message: "" });
  useAppStore.setState({
    ...original, conversationId: "conv-A", workingDirectory: "C:/audit/A", messages: [], diffReview: null,
    gitChanges: { ...original.gitChanges, loading: false, workingTree: [{ path: "a.txt", patch, additions: 1, deletions: 1 }], staged: [], untracked: [] },
    requestGitChanges: vi.fn(), setGitChangesLoading: vi.fn(),
    editorTabs: [], activeTabPath: null, editorOpenRequests: [], fileChanges: [],
  });
});
afterEach(() => { cleanup(); useAppStore.setState(original, true); });

describe("business diff and editor ownership", () => {
  it("never transplants a confirmed discard from workspace A into B", async () => {
    let finish!: (value: boolean) => void;
    seams.confirm.mockReturnValue(new Promise<boolean>((resolve) => { finish = resolve; }));
    render(<DiffPanel />);
    fireEvent.click(screen.getByRole("button", { name: "放弃 a.txt 的更改" }));
    await waitFor(() => expect(seams.confirm).toHaveBeenCalledOnce());
    act(() => useAppStore.setState({ workingDirectory: "C:/audit/B", conversationId: "conv-B" }));
    await act(async () => finish(true));
    expect(seams.send).not.toHaveBeenCalled();
    expect(seams.awaitSend).not.toHaveBeenCalled();
  });

  it("still discards the explicitly confirmed file for the original owner", async () => {
    seams.confirm.mockResolvedValue(true); render(<DiffPanel />);
    fireEvent.click(screen.getByRole("button", { name: "放弃 a.txt 的更改" }));
    await waitFor(() => expect(seams.awaitSend).toHaveBeenCalledWith(expect.objectContaining({
      type: "diff.git_revert_file", path: "a.txt", workspace: "C:/audit/A", conversation_id: "conv-A", confirmed: true,
    }), "diff.git_revert_file", { silent: true }));
  });

  it("opens review comments with the keyboard and does not submit an IME confirmation", async () => {
    useAppStore.setState({ diffReview: {
      requestId: "review-A", conversationId: "conv-A", diff: patch,
      files: [{ path: "a.txt", patch }], selectedPath: "a.txt", status: "pending", mode: "approval",
      fileDecisions: {}, lineComments: [],
    } as any });
    render(<DiffPanel />);
    const line = screen.getAllByRole("button", { name: /评论 Diff 第/ })[0];
    fireEvent.keyDown(line, { key: "Enter" });
    const input = screen.getByRole("textbox", { name: /评论 Diff 第/ });
    fireEvent.change(input, { target: { value: "中文评论" } });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    expect(useAppStore.getState().diffReview?.lineComments).toHaveLength(0);
    fireEvent.keyDown(input, { key: "Enter", isComposing: false });
    expect(useAppStore.getState().diffReview?.lineComments).toHaveLength(1);
  });

  it("selects the root file diff, not the first nested file sharing its basename", () => {
    useAppStore.setState({ editorTabs: [{ id: "root-tab", path: "a.txt", content: "new\n", original: "new\n", loading: false, error: null } as any], activeTabPath: "a.txt",
      gitChanges: { ...useAppStore.getState().gitChanges, workingTree: [{ path: "src/a.txt", patch: "NESTED_PATCH", additions: 9, deletions: 0 }, { path: "a.txt", patch, additions: 1, deletions: 1 }] },
    });
    render(<EditorPanel />); fireEvent.click(screen.getByRole("button", { name: /Diff/ }));
    expect(useAppStore.getState().diffReview?.diff).toBe(patch);
  });

  it("attaches review decorations to the replacement editor instance", () => {
    const first = { set: vi.fn(), clear: vi.fn() }, second = { set: vi.fn(), clear: vi.fn() };
    const make = (collection: typeof first) => ({ createDecorationsCollection: vi.fn(() => collection), executeEdits: vi.fn(), focus: vi.fn() });
    const a = make(first), b = make(second), ref = { current: a };
    const args = { editorRef: ref, path: "a.txt", content: "new\n", readOnly: false,
      turnDiff: { turnId: "turn-A", revision: 1, diff: patch } as any, workingDirectory: "C:/audit/A", editorEpoch: 1 };
    const result = renderHook((props) => useAgentEditReview(props), { initialProps: args });
    expect(a.createDecorationsCollection).toHaveBeenCalled();
    ref.current = b; result.rerender({ ...args, editorEpoch: 2 });
    expect(first.clear).toHaveBeenCalled(); expect(b.createDecorationsCollection).toHaveBeenCalledOnce();
  });

  it("surfaces a rejected file-save request without losing the dirty buffer", async () => {
    useAppStore.setState({ editorTabs: [{ id: "save-tab", path: "a.txt", content: "new\n", original: "old\n", contentHash: "hash-old", loading: false, error: null } as any], activeTabPath: "a.txt" });
    seams.write.mockRejectedValue(new Error("connection failed at save boundary"));
    render(<EditorPanel />);
    act(() => window.dispatchEvent(new Event("editor:save")));
    await waitFor(() => expect(seams.toast).toHaveBeenCalledWith(expect.stringContaining("connection failed at save boundary"), "error", 3500));
    expect(screen.getByText("保存失败")).toBeTruthy();
    expect(useAppStore.getState().editorTabs[0].content).toBe("new\n");
    expect(useAppStore.getState().editorTabs[0].original).toBe("old\n");
  });

  it("retires an editor context menu when its workspace owner changes", () => {
    useAppStore.setState({ editorTabs: [{ id: "tab-A", path: "a.txt", content: "A", original: "A", loading: false, error: null } as any], activeTabPath: "a.txt" });
    const view = render(<EditorPanel />);
    fireEvent.contextMenu(view.container.querySelector(".editor-tab")!, { clientX: 50, clientY: 50 });
    expect(screen.getByText("关闭所有标签页")).toBeTruthy();
    act(() => useAppStore.setState({ workingDirectory: "C:/audit/B", conversationId: "conv-B",
      editorTabs: [{ id: "tab-B", path: "a.txt", content: "B", original: "B", loading: false, error: null } as any], activeTabPath: "a.txt" }));
    expect(screen.queryByText("关闭所有标签页")).toBeNull();
    expect(useAppStore.getState().editorTabs[0].id).toBe("tab-B");
  });
});
