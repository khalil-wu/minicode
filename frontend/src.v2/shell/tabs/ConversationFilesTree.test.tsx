/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  return { tree: vi.fn(), original: vi.fn(), preview: vi.fn(), branches: vi.fn(), search: vi.fn(), upload: vi.fn(), openPreferred: vi.fn(), toast: vi.fn(), copy: vi.fn(), target: "minicode",
    perPath: {} as Record<string, string>, targets: [] as Array<{ id: string; label: string; kind: string; icon: string }> };
});
vi.mock("../../protocol/file-workspace", async importOriginal => ({ ...await importOriginal<typeof import("../../protocol/file-workspace")>(),
  readFileWorkspaceTree: mocks.tree, readFileWorkspaceOriginal: mocks.original, readFileWorkspaceBranches: mocks.branches, searchFileWorkspace: mocks.search }));
vi.mock("../../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: { fileOpenTarget: mocks.target,
  fileOpenTargetPerPath: mocks.perPath } }) }));
vi.mock("../../hooks/useDesktopPreferenceTargets", () => ({ useDesktopPreferenceTargets: () => ({ targets: { fileOpenTargets: [
  { id: "minicode", label: "MiniCode", kind: "editor", hidden: true }, { id: "system", label: "默认应用", kind: "systemDefault", hidden: true }, ...mocks.targets], terminalShellTargets: [] } }) }));
vi.mock("../../desktop/runtime", async (importOriginal) => ({ ...(await importOriginal<typeof import("../../desktop/runtime")>()), desktop: () => ({ openPreferredPath: mocks.openPreferred }) }));
vi.mock("../../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "session-a" }) }));
vi.mock("../../protocol/api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../../protocol/api")>()), uploadAttachment: mocks.upload }));
vi.mock("../../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
vi.mock("../../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("../../lib/clipboard", () => ({ copyText: mocks.copy }));
import { useAppStore } from "../../stores";
import { ConversationFilesTree } from "./ConversationFilesTree";
import type { ConversationBrowserEntry, ConversationBrowserTree } from "../../protocol/conversation-browser";
import { sendClientCommand } from "../../protocol/ws-outbox";
import { handleSessionEvent } from "../../chat/sessionEvents";
import { emptyConversationWorkbenchState } from "../../stores/ui-slice";

const browserRoot = "C:/Users/ago/Documents";
const owner = { scopeId: "conversation:owner-a", conversationId: "owner-a", executionCwd: "C:/tasks/a", browserRoot };
const entry = (path: string, is_dir = false): ConversationBrowserEntry => ({ name: path.split("/").at(-1)!, path, is_dir, size_bytes: is_dir ? null : 12, modified_at: "", has_children: is_dir });
const page = (entries: ConversationBrowserEntry[], root = browserRoot): ConversationBrowserTree => ({ workspace_root: root, requested_path: ".", entries });
const show = () => render(<ConversationFilesTree owner={owner} browserRoot={browserRoot} connected version={1} />);
const selectAction = (name: string, action: string) => { fireEvent.contextMenu(screen.getByText(name), { clientX: 20, clientY: 20 }); fireEvent.click(screen.getByRole("menuitem", { name: action })); };
const fileText = (file: File) => new Promise<string>((resolve) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.readAsText(file); });

const openSubmenu = (item: HTMLElement) => { item.focus(); fireEvent.keyDown(item, { key: "ArrowRight" }); };

beforeEach(() => {
  Object.values(mocks).forEach((mock) => { if (typeof mock === "function" && "mockReset" in mock) mock.mockReset(); });
  mocks.target = "minicode";
  mocks.perPath = {}; mocks.targets = [];
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  URL.createObjectURL = vi.fn(() => "blob:original"); URL.revokeObjectURL = vi.fn();
  mocks.tree.mockResolvedValue(page([entry("notes", true), entry("note.txt"), entry("photo.png")]));
  mocks.branches.mockResolvedValue({ is_git_repo: false, current_branch: null, default_base_branch: null, branches: [], commits: [] });
  mocks.search.mockResolvedValue({ results: [] });
  mocks.original.mockResolvedValue(new Blob(["original bytes\r\n"], { type: "text/plain" }));
  mocks.upload.mockResolvedValue({ conversation_id: "owner-a", artifact_id: "uploaded-original", attachment: { artifact_id: "uploaded-original", file_name: "note.txt", kind: "document", media_type: "text/plain" } });
  vi.mocked(sendClientCommand).mockReturnValue(true);
  useAppStore.setState({ conversationId: "owner-a", pendingConversationSwitchId: null, conversations: [{ id: "owner-a", title: "Owner A", updatedAt: "", executionCwd: owner.executionCwd, projectlessBrowserRoot: browserRoot }],
    workingDirectory: owner.executionCwd, draft: "authored draft", attachments: [{ id: "existing", name: "keep.txt", type: "text/plain", size: 1, status: "ready" }],
    activeConversationDraftId: null, conversationDrafts: {}, homeConversationDraftIds: {}, isConnected: true,
    composerDraftsReady: true, conversationWorkbenchStates: {}, workspaceComposerDrafts: {}, sideChats: {}, editorOpenRequests: [], appMode: "chat", openWorkspaceFileTab: mocks.preview });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("read-only conversation Files tree", () => {
  it("keeps the task collection filesystem root separate from the actual task Git workspace", async () => {
    const base = "C:/tasks", cwd = `${base}/2026-10-25/report`;
    const taskOwner = { scopeId: "conversation:task", conversationId: "task", executionCwd: cwd, browserRoot: base };
    const file = "2026-10-25/report/outputs/alignment-check.md";
    useAppStore.setState({ conversationId: "task", workingDirectory: cwd,
      conversations: [{ id: "task", title: "Task", updatedAt: "", executionCwd: cwd, projectlessBrowserRoot: base }] });
    mocks.tree.mockImplementation(async (_owner, path) => path === "." ? page([entry("2026-10-25", true)], base)
      : page([entry(file)], base));
    const { rerender } = render(<ConversationFilesTree owner={taskOwner} browserRoot={base} connected version={1} />);
    await screen.findByText("2026-10-25");
    expect(mocks.branches).not.toHaveBeenCalled();
    mocks.branches.mockRejectedValueOnce(new Error("Workspace folder is not trusted."));
    rerender(<ConversationFilesTree owner={taskOwner} browserRoot={base} rootSelected connected version={1} />);
    await screen.findByText("Workspace folder is not trusted.");
    expect(mocks.branches).toHaveBeenCalledTimes(1);
    rerender(<ConversationFilesTree owner={taskOwner} browserRoot={cwd} connected version={1} />);
    const row = await screen.findByText("alignment-check.md");
    expect(row.closest('[role="treeitem"]')?.getAttribute("title")).toBe(`${cwd}/outputs/alignment-check.md`);
    fireEvent.click(row);
    expect(mocks.preview).toHaveBeenCalledWith(taskOwner, file, "alignment-check.md");
    await waitFor(() => expect(mocks.branches).toHaveBeenCalledWith(taskOwner, expect.any(AbortSignal), undefined, cwd));
    mocks.search.mockResolvedValue({ results: [{ path: file }] });
    fireEvent.change(screen.getByRole("textbox", { name: "筛选文件" }), { target: { value: "alignment" } });
    const match = await screen.findByText(file);
    expect(match.closest('[role="treeitem"]')?.getAttribute("title")).toBe(`${cwd}/outputs/alignment-check.md`);
    fireEvent.click(match);
    expect(mocks.preview).toHaveBeenLastCalledWith(taskOwner, file, "alignment-check.md");
    expect(useAppStore.getState()).toMatchObject({ conversationId: "task", workingDirectory: cwd, draft: "authored draft" });
  });

  it("does not infer task-collection capability limits from an ancestor path alone", async () => {
    const cwd = "C:/tasks/regular-project";
    const regular = { scopeId: "conversation:regular", conversationId: "regular", executionCwd: cwd, browserRoot: "C:/tasks" };
    useAppStore.setState({ conversationId: "regular", conversations: [{ id: "regular", title: "Regular", updatedAt: "", executionCwd: cwd }] });
    render(<ConversationFilesTree owner={regular} browserRoot="C:/tasks" connected version={1} />);
    await waitFor(() => expect(mocks.branches).toHaveBeenCalledWith({ ...regular, conversationId: null, executionCwd: "C:/tasks" }, expect.any(AbortSignal), undefined, "C:/tasks"));
  });

  it("passes the selected repository and comparison to All changes and filters unloaded paths", async () => {
    mocks.branches.mockResolvedValue({ is_git_repo: true, current_branch: "feature", default_base_branch: "origin/main", branches: ["origin/main"], commits: [] });
    mocks.search.mockResolvedValue({ results: [{ path: "unloaded/deep.ts" }] });
    show();
    fireEvent.click(await screen.findByRole("button", { name: "All changes" }));
    expect(useAppStore.getState().gitReviewRequest).toMatchObject({ path: "", workspaceRoot: browserRoot,
      source: "branch", baseBranch: "origin/main", fileOwner: { ...owner, conversationId: null, executionCwd: browserRoot }, conversationId: "owner-a" });
    fireEvent.change(screen.getByRole("textbox", { name: "筛选文件" }), { target: { value: "deep" } });
    await screen.findByText("unloaded/deep.ts");
    expect(mocks.search).toHaveBeenCalledWith(owner, "deep", expect.any(AbortSignal));
    expect(useAppStore.getState()).toMatchObject({ workingDirectory: owner.executionCwd, draft: "authored draft" });
  });
  it("uses the execution workspace preference when browsing a different projectless root, and explicit file targets stay temporary", async () => {
    mocks.target = "cursor"; mocks.perPath[owner.executionCwd] = "vscode";
    mocks.targets = [{ id: "vscode", label: "VS Code", kind: "editor", icon: "apps/vscode.png" }, { id: "cursor", label: "Cursor", kind: "editor", icon: "apps/cursor.png" },
      { id: "fileManager", label: "File Explorer", kind: "fileManager", icon: "apps/file-explorer.png" }];
    mocks.openPreferred.mockResolvedValue({ openInEditor: false });
    show(); await screen.findByText("note.txt");
    selectAction("note.txt", "在 VS Code 中打开");
    await waitFor(() => expect(mocks.openPreferred).toHaveBeenCalledWith(`${browserRoot}/note.txt`, undefined, { workspaceRoot: owner.executionCwd }));
    fireEvent.contextMenu(screen.getByText("note.txt"));
    openSubmenu(screen.getByRole("menuitem", { name: "打开方式" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Cursor" }));
    await waitFor(() => expect(mocks.openPreferred).toHaveBeenLastCalledWith(`${browserRoot}/note.txt`, undefined, { workspaceRoot: owner.executionCwd, targetId: "cursor" }));
    expect(mocks.perPath[owner.executionCwd]).toBe("vscode");
    expect(useAppStore.getState().draft).toBe("authored draft");
    fireEvent.contextMenu(screen.getByText("photo.png"));
    expect(screen.getByRole("menuitem", { name: "在 默认应用 中打开" })).toBeTruthy();
    openSubmenu(screen.getByRole("menuitem", { name: "打开方式" }));
    expect(screen.queryByRole("menuitem", { name: "VS Code" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Cursor" })).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "File Explorer" }));
    await waitFor(() => expect(mocks.openPreferred).toHaveBeenLastCalledWith(`${browserRoot}/photo.png`, undefined, { workspaceRoot: owner.executionCwd, targetId: "fileManager" }));
  });
  it("reads nested browser paths and previews files without changing the execution or editor root", async () => {
    mocks.tree.mockImplementation(async (_owner, path) => path === "." ? page([entry("notes", true)]) : page([entry("notes/nested.txt")]));
    show(); fireEvent.click(await screen.findByText("notes"));
    fireEvent.click(await screen.findByText("nested.txt"));
    expect(mocks.tree).toHaveBeenCalledWith(owner, "notes", expect.any(AbortSignal));
    expect(mocks.preview).toHaveBeenCalledWith(owner, "notes/nested.txt", "nested.txt");
    expect(useAppStore.getState().workingDirectory).toBe(owner.executionCwd);
    expect(useAppStore.getState().editorOpenRequests).toEqual([]);
  });

  it("exposes only supported read actions, copies the real browser path and saves the exact original blob", async () => {
    show(); await screen.findByText("note.txt");
    fireEvent.contextMenu(screen.getByText("note.txt"));
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent?.trim())).toEqual(["打开", "在 MiniCode 中打开", "另存为…", "复制路径", "添加到聊天"]);
    fireEvent.click(screen.getByRole("menuitem", { name: "复制路径" }));
    expect(mocks.copy).toHaveBeenCalledWith(`${browserRoot}/note.txt`, "路径");
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      expect(this.href).toBe("blob:original"); expect(this.download).toBe("note.txt");
    });
    selectAction("note.txt", "另存为…");
    await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
    expect(mocks.original).toHaveBeenCalledWith(owner, "note.txt", expect.any(AbortSignal));
    expect(URL.createObjectURL).toHaveBeenCalledWith(await mocks.original.mock.results[0].value);
    expect(useAppStore.getState().draft).toBe("authored draft");
  });

  it("opens text in the editor only inside the execution root and keeps other browser files in read-only preview", async () => {
    const view = show(); await screen.findByText("note.txt");
    selectAction("note.txt", "在 MiniCode 中打开");
    expect(mocks.preview).toHaveBeenCalledWith(owner, "note.txt", "note.txt");
    expect(useAppStore.getState().editorOpenRequests).toEqual([]);
    view.unmount(); mocks.tree.mockResolvedValue(page([entry("main.py"), entry("image.png")], owner.executionCwd));
    const executionOwner = { ...owner, browserRoot: owner.executionCwd };
    render(<ConversationFilesTree owner={executionOwner} browserRoot={owner.executionCwd} connected version={1} />);
    await screen.findByText("main.py");
    const editorOpen = vi.spyOn(useAppStore.getState(), "openEditorFile");
    selectAction("main.py", "在 MiniCode 中打开");
    expect(editorOpen).toHaveBeenCalledWith("C:/tasks/a/main.py", "main.py", { exact: true });
    expect(useAppStore.getState().editorOpenRequests).toMatchObject([{ path: "main.py", exact: true }]);
    selectAction("image.png", "在 MiniCode 中打开");
    expect(mocks.preview).toHaveBeenLastCalledWith(executionOwner, "image.png", "image.png");
    expect(useAppStore.getState().workingDirectory).toBe(owner.executionCwd);
    act(() => useAppStore.setState({ workingDirectory: "C:/other-editor-root", editorOpenRequests: [] }));
    selectAction("main.py", "在 MiniCode 中打开");
    expect(mocks.preview).toHaveBeenLastCalledWith(executionOwner, "main.py", "main.py");
    expect(useAppStore.getState().editorOpenRequests).toEqual([]);
    expect(useAppStore.getState().workingDirectory).toBe("C:/other-editor-root");
  });

  it("uses the existing external opener capability and displays its actual rejection", async () => {
    mocks.target = "system"; mocks.openPreferred.mockRejectedValue(new Error("File open target is outside the trusted workspace."));
    show(); await screen.findByText("note.txt"); selectAction("note.txt", "在 默认应用 中打开");
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith("File open target is outside the trusted workspace.", "error"));
    expect(mocks.openPreferred).toHaveBeenCalledWith(`${browserRoot}/note.txt`, undefined, { workspaceRoot: owner.executionCwd });
    expect(mocks.preview).not.toHaveBeenCalled();
    expect(useAppStore.getState().workingDirectory).toBe(owner.executionCwd);
  });

  it("preserves the draft and existing attachments, and keeps a late upload with its original conversation", async () => {
    let resolveUpload!: (value: unknown) => void;
    mocks.upload.mockImplementation(() => new Promise(resolve => { resolveUpload = resolve; }));
    show(); await screen.findByText("note.txt"); selectAction("note.txt", "添加到聊天");
    await waitFor(() => expect(mocks.upload).toHaveBeenCalledTimes(1));
    expect(await fileText(mocks.upload.mock.calls[0][2])).toBe("original bytes\r\n");
    expect(useAppStore.getState().draft).toBe("authored draft");
    expect(useAppStore.getState().attachments.map(item => item.name)).toEqual(["keep.txt", "note.txt"]);
    act(() => {
      useAppStore.setState(state => ({ conversations: [...state.conversations, { id: "owner-b", title: "B", updatedAt: "", executionCwd: "C:/tasks/b", workspaceRoot: "C:/tasks/b" }],
        conversationWorkbenchStates: { ...state.conversationWorkbenchStates, "owner-b": { ...emptyConversationWorkbenchState(), draft: "B draft",
          attachments: [{ id: "b", name: "b.txt", type: "text/plain", size: 1, status: "ready" }] } } }));
      useAppStore.getState().requestConversationSwitch("owner-b");
      const command = vi.mocked(sendClientCommand).mock.calls.find(([request]) => request.type === "conversation.switch" && request.conversation_id === "owner-b")![0];
      const buffers = { textStreamBuffer: { destroy: vi.fn() }, thinkingStreamBuffer: { destroy: vi.fn() } } as never;
      for (const hydrating of [true, false]) handleSessionEvent({ type: "conversation.switched", conversation_id: "owner-b",
        client_command_id: command.client_command_id, client_command_type: "conversation.switch", is_hydrating: hydrating, context_pending: hydrating,
        conversation: { id: "owner-b", title: "B", workspace_root: "C:/tasks/b", execution_cwd: "C:/tasks/b", messages: [] } } as never, buffers);
    });
    await act(async () => resolveUpload({ conversation_id: "owner-a", artifact_id: "uploaded-original", attachment: { artifact_id: "uploaded-original", file_name: "note.txt", kind: "document" } }));
    await waitFor(() => expect(useAppStore.getState().conversationWorkbenchStates["owner-a"].attachments.at(-1)?.status).toBe("ready"));
    expect(useAppStore.getState().conversationWorkbenchStates["owner-a"].draft).toBe("authored draft");
    const uploaded = useAppStore.getState().conversationWorkbenchStates["owner-a"].attachments.at(-1)!;
    expect(uploaded.localFile).toBe(mocks.upload.mock.calls[0][2]);
    expect(await fileText(uploaded.localFile!)).toBe("original bytes\r\n");
    expect(mocks.upload).toHaveBeenCalledTimes(1);
    expect(useAppStore.getState()).toMatchObject({ conversationId: "owner-b", workingDirectory: "C:/tasks/b", draft: "B draft", attachments: [{ id: "b", name: "b.txt" }] });
  });

  it("does not retarget a late original-file read to the next composer", async () => {
    let resolveOriginal!: (value: Blob) => void;
    mocks.original.mockImplementation(() => new Promise(resolve => { resolveOriginal = resolve; }));
    const view = show(); await screen.findByText("note.txt"); selectAction("note.txt", "添加到聊天");
    const signal = mocks.original.mock.calls[0][2] as AbortSignal;
    act(() => useAppStore.setState({ conversationId: "owner-b", draft: "B draft", attachments: [] })); view.unmount();
    await act(async () => resolveOriginal(new Blob(["late bytes"])));
    expect(signal.aborted).toBe(true); expect(mocks.upload).not.toHaveBeenCalled();
    expect(useAppStore.getState()).toMatchObject({ conversationId: "owner-b", draft: "B draft", attachments: [] });
  });

  it("does not publish an old tree after the owner changes and keeps real read errors retryable", async () => {
    let resolveOld!: (value: ConversationBrowserTree) => void;
    mocks.tree.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }));
    const view = show(); view.unmount();
    mocks.tree.mockRejectedValueOnce(new Error("Browser directory was removed."));
    show(); await screen.findByRole("alert");
    await act(async () => resolveOld(page([entry("stale.txt")])));
    expect(screen.queryByText("stale.txt")).toBeNull();
    expect(screen.getByRole("alert").textContent).toContain("Browser directory was removed.");
    mocks.tree.mockResolvedValue(page([entry("current.txt")])); fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByText("current.txt"); expect(screen.queryByRole("alert")).toBeNull();
  });
});
