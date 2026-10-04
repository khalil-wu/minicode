// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FsEntry } from "../desktop/runtime";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", {
  configurable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
}));
const mocks = vi.hoisted(() => ({
  fsListTree: vi.fn(), writeWorkspaceFile: vi.fn(), createWorkspaceDirectory: vi.fn(),
  showPrompt: vi.fn(), showAlert: vi.fn(), pushToast: vi.fn(),
}));
vi.mock("../desktop/runtime", () => ({
  desktop: () => undefined, isDesktop: () => true, trustWorkspace: async () => {},
  fsListTree: mocks.fsListTree, fsSearchFiles: async () => [], openPath: vi.fn(), revealPath: vi.fn(),
}));
vi.mock("../protocol/workspace", () => ({
  listWorkspaceTree: vi.fn(), searchWorkspaceFiles: vi.fn(),
  writeWorkspaceFile: mocks.writeWorkspaceFile, createWorkspaceDirectory: mocks.createWorkspaceDirectory,
}));
vi.mock("../overlays/DialogService", () => ({ showPrompt: mocks.showPrompt, showAlert: mocks.showAlert }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: mocks.pushToast }));
vi.mock("../workspace/openWorkspaceFolder", () => ({ openWorkspaceFolder: vi.fn() }));
vi.mock("../lib/file-icons", () => ({
  fileGlyphColor: () => "gray", fileGlyphKind: () => "file", fileIcon: () => null, folderIcon: () => null,
}));

import { useAppStore } from "../stores";
import { FileTree } from "./FileTree";
import { SearchResultRow } from "./FileTreeSearchResult";
import { expandedStorageKey, normalizeChangePath, parentTreePath, readExpandedPaths } from "./fileTreeHelpers";

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};
const entry = (path: string, isDirectory = true): FsEntry => ({
  path, name: path.split("/").at(-1)!, isDirectory,
});
const rootLoaded = (root: string) => waitFor(() => expect(document.querySelector(`[data-tree-path="${root}/src"]`)).not.toBeNull());

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callback(0); return 1; });
  mocks.fsListTree.mockImplementation(async (path: string) => path === "C:/A" || path === "C:/B"
    ? [entry(`${path}/src`)] : []);
  mocks.showAlert.mockResolvedValue(undefined);
  useAppStore.setState({
    workingDirectory: "C:/A", conversationId: "owner", fileChanges: [], fileTreeRevealRequests: [],
    fileTreeVersion: 0, activeEditorPath: null, editorTabs: [], activeTabPath: null,
    gitChanges: { workingTree: [], staged: [], untracked: [], loading: false }, requestGitChanges: vi.fn(),
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("file explorer workspace ownership", () => {
  it("binds file changes and folder reveal requests at their production store entrance", () => {
    const store = useAppStore.getState();
    store.addFileChange({ path: "src/file.txt", event: "modify", timestamp: 1 });
    store.requestFileTreeReveal("src/deep");
    store.setWorkingDirectory("C:/B");
    expect(useAppStore.getState().fileChanges[0].workspaceRoot).toBe("C:/A");
    expect(useAppStore.getState().fileTreeRevealRequests[0].workspaceRoot).toBe("C:/A");
  });

  it("discards a queued reveal from a previously active workspace", async () => {
    useAppStore.getState().requestFileTreeReveal("src/deep");
    useAppStore.getState().setWorkingDirectory("C:/B");
    render(<FileTree />);
    await rootLoaded("C:/B");
    await waitFor(() => expect(useAppStore.getState().fileTreeRevealRequests).toHaveLength(0));
    expect(mocks.fsListTree).not.toHaveBeenCalledWith("C:/B/src");
    expect(readExpandedPaths("C:/B").size).toBe(0);
  });

  it("does not commit a partially loaded old reveal after a workspace switch", async () => {
    const oldChild = deferred<FsEntry[]>();
    mocks.fsListTree.mockImplementation(async (path: string) => {
      if (path === "C:/A/src") return [entry("C:/A/src/deep")];
      if (path === "C:/A/src/deep") return oldChild.promise;
      return [entry(`${path}/src`)];
    });
    render(<FileTree />);
    await rootLoaded("C:/A");
    act(() => useAppStore.getState().requestFileTreeReveal("src/deep"));
    await waitFor(() => expect(mocks.fsListTree).toHaveBeenCalledWith("C:/A/src/deep"));
    act(() => useAppStore.getState().setWorkingDirectory("C:/B"));
    await rootLoaded("C:/B");
    await act(async () => oldChild.resolve([]));
    expect(readExpandedPaths("C:/A").size).toBe(0);
    expect(readExpandedPaths("C:/B").size).toBe(0);
    expect(mocks.fsListTree).not.toHaveBeenCalledWith("C:/B/src");
  });

  it("drops the previous workspace's pending watcher batch", async () => {
    render(<FileTree />);
    await rootLoaded("C:/A");
    act(() => useAppStore.getState().addFileChange({ path: "obsolete/file.txt", event: "modify", timestamp: 1 }));
    act(() => useAppStore.getState().setWorkingDirectory("C:/B"));
    await rootLoaded("C:/B");
    act(() => useAppStore.getState().addFileChange({ path: "src/new.txt", event: "modify", timestamp: 2 }));
    await waitFor(() => expect(mocks.fsListTree).toHaveBeenCalledWith("C:/B/src"));
    expect(mocks.fsListTree).not.toHaveBeenCalledWith("C:/B/obsolete");
  });

  it.each([
    ["新建文件", "same.txt", "writeWorkspaceFile"],
    ["新建文件夹", "nested", "createWorkspaceDirectory"],
  ] as const)("cancels %s when its dialog outlives the active workspace", async (label, path, mutation) => {
    const answer = deferred<string>();
    mocks.showPrompt.mockReturnValue(answer.promise);
    render(<FileTree />);
    await rootLoaded("C:/A");
    fireEvent.click(screen.getByRole("button", { name: "更多文件操作" }));
    fireEvent.click(screen.getByRole("menuitem", { name: label }));
    await waitFor(() => expect(mocks.showPrompt).toHaveBeenCalled());
    act(() => useAppStore.getState().setWorkingDirectory("C:/B"));
    await act(async () => answer.resolve(path));
    expect(mocks[mutation]).not.toHaveBeenCalled();
    expect(mocks.pushToast).toHaveBeenCalledWith(expect.stringContaining("工作区已切换"), "warning");
  });

  it("persists the two workspaces' scroll offsets under their own keys", async () => {
    render(<FileTree />);
    await rootLoaded("C:/A");
    const originalList = screen.getByRole("tree");
    originalList.scrollTop = 321;
    fireEvent.scroll(originalList);
    act(() => useAppStore.getState().setWorkingDirectory("C:/B"));
    await rootLoaded("C:/B");
    const nextList = screen.getByRole("tree");
    nextList.scrollTop = 876;
    fireEvent.scroll(nextList);
    await waitFor(() => expect(localStorage.getItem("minicode.file-tree.scroll:c:/b")).toBe("876"));
    expect(localStorage.getItem("minicode.file-tree.scroll:c:/a")).toBe("321");
  });

  it("restores scroll when a fast A to B to A switch cancels B's root load", async () => {
    const pending = deferred<FsEntry[]>();
    mocks.fsListTree.mockImplementation(async (path: string) => path === "C:/B"
      ? pending.promise : [entry(`${path}/src`)]);
    render(<FileTree />);
    await rootLoaded("C:/A");
    const list = screen.getByRole("tree");
    list.scrollTop = 321;
    fireEvent.scroll(list);
    act(() => useAppStore.getState().setWorkingDirectory("C:/B"));
    await waitFor(() => expect(mocks.fsListTree).toHaveBeenCalledWith("C:/B"));
    act(() => useAppStore.getState().setWorkingDirectory("C:/A"));
    await rootLoaded("C:/A");
    expect(screen.getByRole("tree").scrollTop).toBe(321);
    await act(async () => pending.resolve([]));
  });

  it("sends a UNC watcher reload to the original network share", async () => {
    const root = "//server/share/repo";
    useAppStore.getState().setWorkingDirectory(root);
    mocks.fsListTree.mockImplementation(async (path: string) => path === root ? [entry(`${root}/src`)] : []);
    render(<FileTree />);
    await rootLoaded(root);
    act(() => useAppStore.getState().addFileChange({ path: "src/file.ts", event: "modify", timestamp: 1 }));
    await waitFor(() => expect(mocks.fsListTree).toHaveBeenCalledWith("//server/share/repo/src"));
    expect(mocks.fsListTree).not.toHaveBeenCalledWith("/server/share/repo/src");
  });

  it("preserves UNC identity for watcher parents and folder reveal paths", () => {
    expect(normalizeChangePath("\\\\server\\share\\repo\\src")).toBe("//server/share/repo/src");
    expect(parentTreePath("//server/share/repo/src/file.ts", "//server/share/repo")).toBe("//server/share/repo/src");
    expect(expandedStorageKey("//SERVER/share/repo")).toBe("minicode.files.expanded://server/share/repo");
  });

  it.each(["src/File.ts", "C:/A/src/File.ts", "c:/a/SRC/file.ts"])("projects the Git status of the Windows search result %s", (path) => {
    render(<SearchResultRow result={{ path, name: "File.ts" }} workingDirectory="C:/A"
      gitMap={new Map([["c:/a/src/file.ts", "modified"]])} activeEditorPath={null} onContextMenu={vi.fn()} />);
    expect(screen.getByText("M")).toBeTruthy();
  });
});
