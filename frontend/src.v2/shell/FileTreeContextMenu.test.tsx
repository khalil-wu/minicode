/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  deletePath: vi.fn(),
  showConfirm: vi.fn(),
  showAlert: vi.fn(),
  showPrompt: vi.fn(),
  renameWorkspacePath: vi.fn(),
  isDesktop: vi.fn(() => true),
  writeWorkspaceFile: vi.fn(),
  createWorkspaceDirectory: vi.fn(),
  deleteWorkspacePath: vi.fn(),
}));

vi.mock("../desktop/runtime", () => ({
  isDesktop: mocks.isDesktop,
  desktop: () => ({
    fs: {
      deletePath: mocks.deletePath,
    },
  }),
  revealPath: vi.fn(),
}));

vi.mock("../overlays/DialogService", () => ({
  showConfirm: mocks.showConfirm,
  showPrompt: mocks.showPrompt,
  showAlert: mocks.showAlert,
}));

vi.mock("../protocol/workspace", () => ({
  renameWorkspacePath: mocks.renameWorkspacePath,
  writeWorkspaceFile: mocks.writeWorkspaceFile,
  createWorkspaceDirectory: mocks.createWorkspaceDirectory,
  deleteWorkspacePath: mocks.deleteWorkspacePath,
}));

import { FileContextMenu } from "./FileTreeContextMenu";
import { useAppStore } from "../stores";
import { clearEditorWorkspaceBufferCacheForTests } from "../stores/shared-helpers";

describe("FileContextMenu desktop deletion", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mocks.isDesktop.mockReturnValue(true);
    clearEditorWorkspaceBufferCacheForTests();
    localStorage.clear();
    useAppStore.setState({
      workingDirectory: "C:/repo", editorTabs: [], activeTabPath: null,
      activeEditorPath: null, editorOpenRequests: [],
      panelSlots: [{ id: "editor", kind: "editor", label: "Editor", focused: true }],
    });
  });

  afterEach(() => {
    cleanup();
  });

  it("retries a large directory deletion only after the second confirmation", async () => {
    const onRefresh = vi.fn();
    const onClose = vi.fn();
    mocks.showConfirm.mockResolvedValueOnce(true).mockResolvedValueOnce(true);
    mocks.deletePath
      .mockResolvedValueOnce({
        needsConfirmation: true,
        path: "C:/repo/vendor",
        entryCount: 51,
      })
      .mockResolvedValueOnce({
        deleted: true,
        path: "C:/repo/vendor",
        is_dir: true,
      });

    render(
      <FileContextMenu
        menu={{ path: "C:/repo/vendor", isDir: true, x: 0, y: 0 }}
        workingDirectory="C:/repo"
        onRefresh={onRefresh}
        onClose={onClose}
      />,
    );

    fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));

    await waitFor(() => expect(mocks.deletePath).toHaveBeenCalledTimes(2));
    expect(mocks.deletePath).toHaveBeenNthCalledWith(1, "C:/repo/vendor", true, false);
    expect(mocks.deletePath).toHaveBeenNthCalledWith(2, "C:/repo/vendor", true, true);
    expect(mocks.showConfirm).toHaveBeenNthCalledWith(2, expect.objectContaining({
      title: "确认删除大型目录",
      message: expect.stringContaining("51+ 个项目"),
    }));
    expect(onRefresh).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalled();
  });

  it.each([
    ["新建文件…", "创建失败", mocks.writeWorkspaceFile],
    ["新建文件夹…", "创建失败", mocks.createWorkspaceDirectory],
    ["删除", "删除失败", mocks.deleteWorkspacePath],
  ] as const)("shows the actual failure for the web %s action and leaves the tree intact", async (action, title, mutation) => {
    mocks.isDesktop.mockReturnValue(false);
    mocks.showPrompt.mockResolvedValueOnce("same");
    mocks.showConfirm.mockResolvedValueOnce(true);
    mutation.mockRejectedValueOnce(new Error("Workspace folder is not trusted."));
    const onRefresh = vi.fn();
    const onClose = vi.fn();
    render(<FileContextMenu menu={{ path: "src", isDir: true, x: 0, y: 0 }} workingDirectory="C:/repo" onRefresh={onRefresh} onClose={onClose} />);
    fireEvent.click(screen.getByRole("menuitem", { name: action }));
    await waitFor(() => expect(mocks.showAlert).toHaveBeenCalledWith({ title, message: "Workspace folder is not trusted." }));
    expect(onRefresh).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it("keeps the directory when the second confirmation is cancelled", async () => {
    const onRefresh = vi.fn();
    const onClose = vi.fn();
    mocks.showConfirm.mockResolvedValueOnce(true).mockResolvedValueOnce(false);
    mocks.deletePath.mockResolvedValueOnce({
      needsConfirmation: true,
      path: "C:/repo/vendor",
      entryCount: 51,
    });

    render(
      <FileContextMenu
        menu={{ path: "C:/repo/vendor", isDir: true, x: 0, y: 0 }}
        workingDirectory="C:/repo"
        onRefresh={onRefresh}
        onClose={onClose}
      />,
    );

    fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));

    await waitFor(() => expect(mocks.showConfirm).toHaveBeenCalledTimes(2));
    expect(mocks.deletePath).toHaveBeenCalledTimes(1);
    expect(mocks.deletePath).toHaveBeenCalledWith("C:/repo/vendor", true, false);
    expect(onRefresh).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it.each([false, true])("shows the IPC failure when deletion fails after large-directory confirmation=%s", async (largeDirectory) => {
    const onRefresh = vi.fn();
    const onClose = vi.fn();
    mocks.showConfirm.mockResolvedValue(true);
    if (largeDirectory) {
      mocks.deletePath.mockResolvedValueOnce({ needsConfirmation: true, path: "C:/repo/vendor", entryCount: 51 });
    }
    mocks.deletePath.mockRejectedValueOnce(new Error("Path targets a protected path and cannot be modified."));
    render(
      <FileContextMenu
        menu={{ path: "C:/repo/vendor", isDir: true, x: 0, y: 0 }}
        workingDirectory="C:/repo" onRefresh={onRefresh} onClose={onClose}
      />,
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));
    await waitFor(() => expect(mocks.showAlert).toHaveBeenCalledWith({
      title: "删除失败", message: "Path targets a protected path and cannot be modified.",
    }));
    expect(onRefresh).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it("offers the editor only for editable text files", () => {
    const common = { workingDirectory: "C:/repo", onRefresh: vi.fn(), onClose: vi.fn() };
    const { unmount } = render(
      <FileContextMenu menu={{ path: "src/main.py", isDir: false, x: 0, y: 0 }} {...common} />,
    );
    expect(screen.getByRole("menuitem", { name: "在编辑器中打开" })).toBeTruthy();
    unmount();

    render(
      <FileContextMenu menu={{ path: "paper_draft.docx", isDir: false, x: 0, y: 0 }} {...common} />,
    );
    expect(screen.queryByRole("button", { name: "在编辑器中打开" })).toBeNull();
    expect(screen.queryByRole("button", { name: "在预览面板中打开" })).toBeNull();
  });

  it.each([false, true])("preserves unsaved descendants when a directory rename settles in an inactive workspace=%s", async (switchWorkspace) => {
    let finishRename!: () => void;
    mocks.showPrompt.mockResolvedValueOnce("renamed");
    mocks.renameWorkspacePath.mockImplementationOnce(() => new Promise<void>((resolve) => { finishRename = resolve; }));
    const draft = { id: "editor-fixture-1", path: "src/main.ts", content: "unsaved", original: "disk", contentHash: "disk hash", loading: false };
    useAppStore.setState({
      editorTabs: [draft, { ...draft, id: "child-buffer", path: "src/nested/child.ts" }, { ...draft, id: "unrelated-buffer", path: "src-other/keep.ts" }],
      activeTabPath: draft.path, activeEditorPath: draft.path,
    });
    const onRefresh = vi.fn();
    render(<FileContextMenu menu={{ path: "C:/repo/src", isDir: true, x: 0, y: 0 }} workingDirectory="C:/repo" onRefresh={onRefresh} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("menuitem", { name: "重命名…" }));
    await waitFor(() => expect(mocks.renameWorkspacePath).toHaveBeenCalledWith("C:/repo/src", "C:/repo/renamed", "C:/repo"));
    if (switchWorkspace) act(() => {
      useAppStore.getState().setWorkingDirectory("C:/other");
      useAppStore.setState({ editorTabs: [{ ...draft, id: "other-workspace-buffer", content: "other workspace draft" }], activeTabPath: draft.path });
    });
    await act(async () => finishRename());
    if (switchWorkspace) {
      expect(useAppStore.getState().editorTabs[0]).toMatchObject({ path: "src/main.ts", content: "other workspace draft" });
      act(() => useAppStore.getState().setWorkingDirectory("C:/repo"));
    }
    const state = useAppStore.getState();
    expect(state.editorTabs.map((tab) => tab.path)).toEqual(["renamed/main.ts", "renamed/nested/child.ts", "src-other/keep.ts"]);
    expect(state.editorTabs[0]).toMatchObject({ content: "unsaved", original: "disk", contentHash: "disk hash", loading: false });
    expect(state.activeTabPath).toBe("renamed/main.ts");
    expect(state.activeEditorPath).toBe("renamed/main.ts");
    expect(JSON.parse(localStorage.getItem("minicode.editor.tabs:c:/repo")!)).toEqual(["renamed/main.ts", "renamed/nested/child.ts", "src-other/keep.ts"]);
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("keeps the original buffer and presents the server's rename error", async () => {
    mocks.showPrompt.mockResolvedValueOnce("exists.ts");
    mocks.renameWorkspacePath.mockRejectedValueOnce(new Error("Target already exists: exists.ts"));
    const tab = { id: "editor-fixture-2", path: "before.ts", content: "unsaved", original: "disk", loading: false };
    useAppStore.setState({ editorTabs: [tab], activeTabPath: tab.path });
    const onRefresh = vi.fn();
    render(<FileContextMenu menu={{ path: "C:/repo/before.ts", isDir: false, x: 0, y: 0 }} workingDirectory="C:/repo" onRefresh={onRefresh} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("menuitem", { name: "重命名…" }));
    await waitFor(() => expect(mocks.showAlert).toHaveBeenCalledWith({ title: "重命名失败", message: "Target already exists: exists.ts" }));
    expect(useAppStore.getState().editorTabs).toEqual([tab]);
    expect(onRefresh).not.toHaveBeenCalled();
  });
});
