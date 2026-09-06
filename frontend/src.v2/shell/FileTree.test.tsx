/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { WorkspaceTreeNode } from "../protocol/workspace";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    value: () => ({
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
    }),
  });
});

const mocks = vi.hoisted(() => ({
  listWorkspaceTree: vi.fn(),
  requestGitChanges: vi.fn(),
  isDesktop: vi.fn(() => false),
  fsListTree: vi.fn(),
  searchWorkspaceFiles: vi.fn(),
}));

vi.mock("../protocol/workspace", () => ({
  listWorkspaceTree: (...args: unknown[]) => mocks.listWorkspaceTree(...args),
  writeWorkspaceFile: vi.fn(),
  createWorkspaceDirectory: vi.fn(),
  searchWorkspaceFiles: mocks.searchWorkspaceFiles,
}));

vi.mock("../desktop/runtime", () => ({
  desktop: () => undefined,
  isDesktop: mocks.isDesktop,
  openPath: vi.fn(),
  fsListTree: mocks.fsListTree,
  fsSearchFiles: vi.fn().mockResolvedValue([]),
  trustWorkspace: vi.fn(),
}));

vi.mock("../workspace/openWorkspaceFolder", () => ({
  openWorkspaceFolder: vi.fn(),
}));

import { useAppStore } from "../stores";
import { FileTree } from "./FileTree";
import { readExpandedPaths, writeExpandedPaths } from "./fileTreeHelpers";

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
};

const rootNode = (workspace: string): WorkspaceTreeNode => ({
  name: workspace,
  path: ".",
  is_dir: true,
  children: [{ name: "src", path: "src", is_dir: true, children: [] }],
});

const directoryNode = (fileName: string): WorkspaceTreeNode => ({
  name: "src",
  path: "src",
  is_dir: true,
  children: [{ name: fileName, path: `src/${fileName}`, is_dir: false }],
});

const originalRequestGitChanges = useAppStore.getState().requestGitChanges;

describe("FileTree directory request ownership", () => {
  beforeEach(() => {
    localStorage.clear();
    mocks.listWorkspaceTree.mockReset();
    mocks.requestGitChanges.mockReset();
    mocks.isDesktop.mockReturnValue(false);
    mocks.fsListTree.mockReset();
    mocks.searchWorkspaceFiles.mockReset().mockResolvedValue([]);
    useAppStore.setState({
      workingDirectory: "workspace-a",
      fileTreeVersion: 0,
      fileTreeRevealRequests: [],
      activeEditorPath: null,
      fileChanges: [],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
      requestGitChanges: mocks.requestGitChanges,
    });
  });

  afterEach(() => {
    cleanup();
    useAppStore.setState({
      workingDirectory: "",
      requestGitChanges: originalRequestGitChanges,
    });
  });

  it("shows search errors and retries the same query", async () => {
    mocks.listWorkspaceTree.mockResolvedValue(rootNode("workspace-a"));
    mocks.searchWorkspaceFiles.mockRejectedValueOnce(new Error("Search permission denied"))
      .mockResolvedValueOnce([{ path: "needle.ts", name: "needle.ts" }]);
    render(<FileTree />);
    fireEvent.change(await screen.findByRole("textbox", { name: "搜索工作区文件" }), { target: { value: "needle" } });

    expect((await screen.findByRole("alert")).textContent).toContain("Search permission denied");
    expect(screen.queryByText(/没有与/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重试搜索" }));

    expect(await screen.findByText("needle.ts")).toBeTruthy();
    expect(mocks.searchWorkspaceFiles).toHaveBeenLastCalledWith("workspace-a", "needle", 60, "all");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("ignores a failed search from the previous workspace", async () => {
    const oldSearch = deferred<Array<{ path: string; name: string }>>();
    mocks.listWorkspaceTree.mockResolvedValue(rootNode("workspace-a"));
    mocks.searchWorkspaceFiles.mockReturnValueOnce(oldSearch.promise).mockResolvedValue([{ path: "new-file.ts", name: "new-file.ts" }]);
    render(<FileTree />);
    fireEvent.change(await screen.findByRole("textbox", { name: "搜索工作区文件" }), { target: { value: "file" } });
    await waitFor(() => expect(mocks.searchWorkspaceFiles).toHaveBeenCalledTimes(1));
    act(() => useAppStore.setState({ workingDirectory: "workspace-b" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "搜索工作区文件" }), { target: { value: "file" } });
    await screen.findByText("new-file.ts");

    await act(async () => oldSearch.reject(new Error("Old workspace offline")));

    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText("new-file.ts")).toBeTruthy();
  });

  it("discards a directory response from the previous workspace epoch", async () => {
    const workspaceADirectory = deferred<WorkspaceTreeNode | null>();
    const workspaceBDirectory = deferred<WorkspaceTreeNode | null>();
    mocks.listWorkspaceTree.mockImplementation((workspace: string, path: string) => {
      if (path === ".") return Promise.resolve(rootNode(workspace));
      if (workspace === "workspace-a") return workspaceADirectory.promise;
      if (workspace === "workspace-b") return workspaceBDirectory.promise;
      throw new Error(`Unexpected workspace request: ${workspace}:${path}`);
    });

    render(<FileTree />);

    const workspaceAFolder = await screen.findByRole("treeitem", { name: "src" });
    fireEvent.click(workspaceAFolder);
    await waitFor(() => expect(mocks.listWorkspaceTree).toHaveBeenCalledWith("workspace-a", "src"));

    act(() => {
      useAppStore.setState({ workingDirectory: "workspace-b" });
    });

    await screen.findByTitle("workspace-b");
    const workspaceBFolder = await screen.findByRole("treeitem", { name: "src", expanded: false });
    fireEvent.click(workspaceBFolder);
    await waitFor(() => expect(mocks.listWorkspaceTree).toHaveBeenCalledWith("workspace-b", "src"));

    await act(async () => {
      workspaceBDirectory.resolve(directoryNode("workspace-b.ts"));
      await workspaceBDirectory.promise;
    });
    expect(await screen.findByText("workspace-b.ts")).toBeTruthy();

    await act(async () => {
      workspaceADirectory.resolve(directoryNode("workspace-a.ts"));
      await workspaceADirectory.promise;
    });

    expect(screen.getByText("workspace-b.ts")).toBeTruthy();
    expect(screen.queryByText("workspace-a.ts")).toBeNull();
  });

  it("shows a refresh failure beside the existing tree and clears it after retry", async () => {
    mocks.listWorkspaceTree.mockResolvedValue(rootNode("workspace-a"));
    render(<FileTree />);
    await screen.findByRole("treeitem", { name: "src" });

    mocks.listWorkspaceTree.mockRejectedValueOnce(new Error("Permission denied"));
    fireEvent.click(screen.getByRole("button", { name: "刷新文件" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Permission denied");
    expect(screen.getByRole("treeitem", { name: "src" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  it("does not show the previous project's files while the next project fails to load", async () => {
    const workspaceB = deferred<WorkspaceTreeNode>();
    mocks.listWorkspaceTree.mockImplementation((workspace: string) => (
      workspace === "workspace-a" ? Promise.resolve(rootNode(workspace)) : workspaceB.promise
    ));
    render(<FileTree />);
    await screen.findByRole("treeitem", { name: "src" });
    act(() => useAppStore.setState({ workingDirectory: "workspace-b" }));
    expect(screen.queryByRole("treeitem", { name: "src" })).toBeNull();
    await act(async () => { workspaceB.reject(new Error("Permission denied for workspace-b")); });
    expect((await screen.findByRole("alert")).textContent).toContain("workspace-b");
    expect(screen.queryByRole("tree")).toBeNull();
  });

  it("retains nested expansion after a desktop read error so retry can restore the files", async () => {
    const root = "C:/workspace-a";
    mocks.isDesktop.mockReturnValue(true);
    useAppStore.setState({ workingDirectory: root });
    writeExpandedPaths(root, new Set([`${root}/src`, `${root}/src/nested`]));
    let denied = true;
    mocks.fsListTree.mockImplementation(async (path: string) => {
      if (path === root) return [{ name: "src", path: `${root}/src`, isDirectory: true }];
      if (path === `${root}/src`) return [{ name: "nested", path: `${root}/src/nested`, isDirectory: true }];
      if (denied) throw new Error("Access denied");
      return [{ name: "leaf.ts", path: `${root}/src/nested/leaf.ts`, isDirectory: false }];
    });
    render(<FileTree />);
    expect((await screen.findByRole("alert")).textContent).toContain("Access denied");
    expect(readExpandedPaths(root)).toEqual(new Set([`${root}/src`, `${root}/src/nested`]));

    denied = false;
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("leaf.ts")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("does not apply stale desktop expansion cleanup to the new workspace", async () => {
    const rootA = "C:/workspace-a";
    const rootB = "C:/workspace-b";
    const staleRead = deferred<never>();
    mocks.isDesktop.mockReturnValue(true);
    useAppStore.setState({ workingDirectory: rootA });
    writeExpandedPaths(rootA, new Set([`${rootA}/src`]));
    writeExpandedPaths(rootB, new Set([`${rootB}/src`]));
    mocks.fsListTree.mockImplementation(async (path: string) => {
      if (path === `${rootA}/src`) return staleRead.promise;
      if (path === `${rootB}/src`) return [{ name: "b.ts", path: `${rootB}/src/b.ts`, isDirectory: false }];
      return [{ name: "src", path: `${path}/src`, isDirectory: true }];
    });
    render(<FileTree />);
    await waitFor(() => expect(mocks.fsListTree).toHaveBeenCalledWith(`${rootA}/src`));
    act(() => useAppStore.setState({ workingDirectory: rootB }));
    expect(await screen.findByText("b.ts")).toBeTruthy();
    await act(async () => { staleRead.reject(new Error("Directory not found")); });
    expect(screen.getByText("b.ts")).toBeTruthy();
    expect(screen.getByRole("treeitem", { name: "src", expanded: true })).toBeTruthy();
    expect(readExpandedPaths(rootB)).toEqual(new Set([`${rootB}/src`]));
  });
});
