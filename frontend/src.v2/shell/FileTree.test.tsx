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
}));

vi.mock("../protocol/workspace", () => ({
  listWorkspaceTree: (...args: unknown[]) => mocks.listWorkspaceTree(...args),
  writeWorkspaceFile: vi.fn(),
  createWorkspaceDirectory: vi.fn(),
  searchWorkspaceFiles: vi.fn().mockResolvedValue([]),
}));

vi.mock("../desktop/runtime", () => ({
  desktop: () => undefined,
  isDesktop: () => false,
  openPath: vi.fn(),
  fsListTree: vi.fn(),
  fsSearchFiles: vi.fn().mockResolvedValue([]),
  trustWorkspace: vi.fn(),
}));

vi.mock("../workspace/openWorkspaceFolder", () => ({
  openWorkspaceFolder: vi.fn(),
}));

import { useAppStore } from "../stores";
import { FileTree } from "./FileTree";

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
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
});
