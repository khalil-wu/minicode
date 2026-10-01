// @vitest-environment jsdom
import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { FileTree } from "./FileTree";
import { FileContextMenu } from "./FileTreeContextMenu";
import { fetchWithTimeout } from "../protocol/api";
import { listWorkspaceTree } from "../protocol/workspace";
import { isDesktop, desktop } from "../desktop/runtime";
import { showAlert, showConfirm, showPrompt } from "../overlays/DialogService";
import { pushToast } from "../overlays/ToastContainer";

vi.mock("../protocol/ws-outbox", async (load) => ({ ...await load<typeof import("../protocol/ws-outbox")>(), sendClientCommand: vi.fn(() => true) }));
vi.mock("../protocol/api", async (load) => ({ ...await load<typeof import("../protocol/api")>(), fetchWithTimeout: vi.fn() }));
vi.mock("../protocol/workspace", async (load) => ({
  ...await load<typeof import("../protocol/workspace")>(),
  listWorkspaceTree: vi.fn(async () => ({ name: "root", path: ".", is_dir: true, children: [{ name: "same.txt", path: "same.txt", is_dir: false }] })),
  searchWorkspaceFiles: vi.fn(async () => []),
}));
vi.mock("../desktop/runtime", async (load) => ({ ...await load<typeof import("../desktop/runtime")>(), isDesktop: vi.fn(() => false), desktop: vi.fn() }));
vi.mock("../overlays/DialogService", () => ({ showConfirm: vi.fn(), showPrompt: vi.fn(), showAlert: vi.fn(async () => {}) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../workspace/WorkspaceContextMenu", () => ({ WorkspaceContextMenu: ({ path }: { path: string }) => <div data-testid="workspace-menu" data-root={path} /> }));

const initial = useAppStore.getState();
const projection = vi.fn();
const requests: { method: string; root: string; path: string; newPath?: string }[] = [];
let roots: Map<string, Set<string>>;
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};
const changeRoot = (root: string) => act(() => useAppStore.setState({ workingDirectory: root, conversationId: root ? "B" : null }));
const renderFileMenu = (isDir = false) => render(<FileContextMenu menu={{ x: 20, y: 20, path: isDir ? "docs" : "same.txt", isDir }} workingDirectory="C:/A" onRefresh={vi.fn()} onClose={vi.fn()} />);

beforeEach(() => {
  vi.clearAllMocks();
  requests.length = 0;
  roots = new Map([["C:/A", new Set(["same.txt"])], ["C:/B", new Set(["same.txt"])]]);
  localStorage.clear();
  useAppStore.setState({ ...initial, workingDirectory: "C:/A", conversationId: "A", fileTreeVersion: 0, fileTreeRevealRequests: [], fileChanges: [], gitChanges: { workingTree: [], staged: [], untracked: [] }, activeEditorPath: null, renameEditorPath: projection });
  vi.mocked(isDesktop).mockReturnValue(false);
  vi.mocked(showConfirm).mockResolvedValue(true);
  vi.mocked(showPrompt).mockResolvedValue("renamed.txt");
  vi.mocked(fetchWithTimeout).mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    const root = url.searchParams.get("workspace_root")!;
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    const path = url.searchParams.get("path") || body.path;
    requests.push({ method: init?.method || "GET", root, path, newPath: body.new_path });
    const entries = roots.get(root);
    if (!entries) return new Response(JSON.stringify({ detail: "Original workspace folder is missing" }), { status: 404 });
    if (init?.method === "DELETE") entries.delete(path);
    if (url.pathname.endsWith("/rename")) { entries.delete(path); entries.add(body.new_path); }
    return new Response("{}", { status: 200 });
  });
  HTMLElement.prototype.scrollIntoView = vi.fn();
});
afterEach(cleanup);

describe("FileTree root-bound user actions", () => {
  it("clears an A file menu rather than restoring it after B's tree refresh", async () => {
    const view = render(<FileTree />);
    await waitFor(() => expect(view.container.querySelector('[data-tree-path="same.txt"]')).toBeTruthy());
    fireEvent.contextMenu(view.container.querySelector('[data-tree-path="same.txt"]')!);
    expect(screen.getByRole("menu")).toBeTruthy();
    changeRoot("C:/B");
    await waitFor(() => expect(listWorkspaceTree).toHaveBeenCalledWith("C:/B", "."));
    await waitFor(() => expect(view.container.querySelector('[data-tree-path="same.txt"]')).toBeTruthy());
    expect(screen.queryByRole("menu")).toBeNull();
    fireEvent.contextMenu(view.container.querySelector('[data-tree-path="same.txt"]')!);
    fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));
    await waitFor(() => expect(requests).toEqual([{ method: "DELETE", root: "C:/B", path: "same.txt", newPath: undefined }]));
    expect(roots.get("C:/A")?.has("same.txt")).toBe(true);
    expect(roots.get("C:/B")?.has("same.txt")).toBe(false);
  });

  it("clears root and toolbar menus on workspace scope changes", async () => {
    render(<FileTree />);
    await waitFor(() => expect(screen.getByTitle("C:/A")).toBeTruthy());
    fireEvent.contextMenu(screen.getByTitle("C:/A").parentElement!);
    expect(screen.getByTestId("workspace-menu").getAttribute("data-root")).toBe("C:/A");
    fireEvent.click(screen.getByRole("button", { name: "更多文件操作" }));
    changeRoot("C:/B");
    await waitFor(() => expect(listWorkspaceTree).toHaveBeenCalledWith("C:/B", "."));
    expect(screen.queryByTestId("workspace-menu")).toBeNull();
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it.each(["C:/B", ""])("cancels an A delete confirmation after scope changes to %s", async (root) => {
    const pending = deferred<boolean>();
    vi.mocked(showConfirm).mockReturnValueOnce(pending.promise);
    renderFileMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));
    await waitFor(() => expect(showConfirm).toHaveBeenCalledTimes(1));
    changeRoot(root);
    await act(async () => pending.resolve(true));
    expect(requests).toEqual([]);
    expect(pushToast).toHaveBeenCalledWith(expect.stringContaining("原文件操作已取消"), "warning");
    expect(roots.get("C:/B")?.has("same.txt")).toBe(true);
  });

  it.each(["C:/B", ""])("cancels an A rename prompt after scope changes to %s", async (root) => {
    const pending = deferred<string | null>();
    vi.mocked(showPrompt).mockReturnValueOnce(pending.promise);
    renderFileMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "重命名…" }));
    await waitFor(() => expect(showPrompt).toHaveBeenCalledTimes(1));
    changeRoot(root);
    await act(async () => pending.resolve("renamed.txt"));
    expect(requests).toEqual([]);
    expect(projection).not.toHaveBeenCalled();
    expect(pushToast).toHaveBeenCalledWith(expect.stringContaining("原文件操作已取消"), "warning");
  });

  it("dispatches an unchanged delete through the real workspace HTTP operation with A's root", async () => {
    renderFileMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));
    await waitFor(() => expect(roots.get("C:/A")?.has("same.txt")).toBe(false));
    expect(requests).toEqual([{ method: "DELETE", root: "C:/A", path: "same.txt", newPath: undefined }]);
    expect(roots.get("C:/B")?.has("same.txt")).toBe(true);
  });

  it("dispatches unchanged rename paths/root through the real HTTP operation and scopes editor projection", async () => {
    renderFileMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "重命名…" }));
    await waitFor(() => expect(projection).toHaveBeenCalledWith("same.txt", "renamed.txt", "C:/A"));
    expect(requests).toEqual([{ method: "POST", root: "C:/A", path: "same.txt", newPath: "renamed.txt" }]);
    expect(roots.get("C:/A")?.has("renamed.txt")).toBe(true);
    expect(roots.get("C:/B")?.has("same.txt")).toBe(true);
  });

  it("reports a vanished original root without retrying against B", async () => {
    roots.delete("C:/A");
    renderFileMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));
    await waitFor(() => expect(showAlert).toHaveBeenCalledWith(expect.objectContaining({ title: "删除失败", message: expect.stringContaining("Original workspace folder is missing") })));
    expect(requests.map((request) => request.root)).toEqual(["C:/A"]);
    expect(roots.get("C:/B")?.has("same.txt")).toBe(true);
  });

  it.each(["新建文件…", "新建文件夹…"])("cancels stale %s prompts at the same action boundary", async (label) => {
    const pending = deferred<string | null>();
    vi.mocked(showPrompt).mockReturnValueOnce(pending.promise);
    renderFileMenu(true);
    fireEvent.click(screen.getByRole("menuitem", { name: label }));
    await waitFor(() => expect(showPrompt).toHaveBeenCalledTimes(1));
    changeRoot("C:/B");
    await act(async () => pending.resolve("child"));
    expect(requests).toEqual([]);
    expect(pushToast).toHaveBeenCalledWith(expect.stringContaining("原文件操作已取消"), "warning");
  });

  it("keeps native delete's actual path bound to A and cancels stale large-directory confirmation", async () => {
    const pending = deferred<boolean>();
    vi.mocked(isDesktop).mockReturnValue(true);
    const deletePath = vi.fn(async () => ({ needsConfirmation: true, entryCount: 200 }));
    vi.mocked(desktop).mockReturnValue({ fs: { deletePath } } as unknown as ReturnType<typeof desktop>);
    vi.mocked(showConfirm).mockResolvedValueOnce(true).mockReturnValueOnce(pending.promise);
    renderFileMenu(true);
    fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));
    await waitFor(() => expect(showConfirm).toHaveBeenCalledTimes(2));
    expect(deletePath).toHaveBeenCalledWith("C:/A/docs", true, false);
    changeRoot("C:/B");
    await act(async () => pending.resolve(true));
    expect(deletePath).toHaveBeenCalledTimes(1);
    expect(pushToast).toHaveBeenCalledWith(expect.stringContaining("原文件操作已取消"), "warning");
  });
});
