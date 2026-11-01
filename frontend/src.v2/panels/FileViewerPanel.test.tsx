/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => {
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  return { preview: vi.fn(), branches: vi.fn(), diff: vi.fn(), original: vi.fn(), external: vi.fn(), save: vi.fn(), copy: vi.fn(),
    colorize: vi.fn((_lines: unknown[]) => null), createUrl: vi.fn((_blob: Blob) => ""), revokeUrl: vi.fn((_url: string) => {}) };
});
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "file-viewer-session" }) }));
vi.mock("../protocol/file-workspace", async importOriginal => ({ ...await importOriginal<typeof import("../protocol/file-workspace")>(),
  readFileWorkspacePreview: mocks.preview, readFileWorkspaceBranches: mocks.branches, readFileWorkspaceDiff: mocks.diff, readFileWorkspaceOriginal: mocks.original }));
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: { fileOpenTarget: "cursor", fileOpenTargetPerPath: {} } }) }));
vi.mock("../hooks/useDesktopPreferenceTargets", () => ({ useDesktopPreferenceTargets: () => ({ targets: { fileOpenTargets: [
  { id: "cursor", label: "Cursor", kind: "editor" }, { id: "fileManager", label: "Files", kind: "fileManager" }], terminalShellTargets: [] } }) }));
vi.mock("../desktop/runtime", async importOriginal => ({ ...await importOriginal<typeof import("../desktop/runtime")>(), desktop: () => ({ openPreferredPath: mocks.external }), runtime: () => ({}) }));
vi.mock("../shell/tabs/ConversationFilesTree", () => ({ ConversationFilesTree: () => <div role="tree" aria-label="captured file tree" /> }));
vi.mock("../lib/monaco-colorize", () => ({ guessLanguageFromPath: () => "plaintext", useColorizedLines: mocks.colorize }));
vi.mock("../lib/save-blob", () => ({ saveBlobFile: mocks.save }));
vi.mock("../lib/clipboard", () => ({ copyText: mocks.copy }));
import { useAppStore } from "../stores";
import { FileViewerPanel } from "./FileViewerPanel";
import { workspaceFileAbsolutePath } from "../protocol/file-workspace";
import { PanelVisibilityContext } from "../shell/panelVisibility";

beforeEach(() => {
  const BrowserURL = URL;
  vi.stubGlobal("URL", class extends BrowserURL { static createObjectURL = mocks.createUrl; static revokeObjectURL = mocks.revokeUrl; });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  mocks.preview.mockReset().mockImplementation(async (_owner, path) => ({ path, file_name: path, content: `saved ${path}`, media_type: "text/plain", kind: "text", truncated: false }));
  mocks.branches.mockReset().mockResolvedValue({ is_git_repo: false, current_branch: null, default_base_branch: null, branches: [], commits: [] });
  mocks.diff.mockReset().mockResolvedValue({ diff: "" }); mocks.external.mockReset(); mocks.save.mockReset();
  mocks.original.mockReset().mockResolvedValue(new Blob(["original bytes"])); mocks.copy.mockReset();
  mocks.colorize.mockReset().mockReturnValue(null);
  mocks.createUrl.mockReset().mockImplementation(() => `blob:file-viewer-${mocks.createUrl.mock.calls.length}`);
  mocks.revokeUrl.mockClear();
  useAppStore.setState({ workspaceFileTabs: [], isConnected: true, conversationId: null, workingDirectory: "C:/home", previewArtifact: null,
    fileTreeVersion: 0, draft: "unsent", panelSlots: [{ id: "chat", kind: "chat", focused: true }], conversations: [] });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const gitBranches = (base = "main") => ({ is_git_repo: true, current_branch: "feature", default_base_branch: base, branches: [base, "feature"], commits: [] });

it.each(["active", "ancestor"])("retires file menus on %s hiding and retains reading state and drafts", async (source) => {
  const owner = { scopeId: "home:C:/home", conversationId: null, executionCwd: "C:/home", browserRoot: "C:/home" };
  mocks.branches.mockResolvedValue(gitBranches());
  const id = useAppStore.getState().openWorkspaceFileTab(owner, "file.ts");
  const panel = (visible: boolean) => <PanelVisibilityContext.Provider value={source === "ancestor" ? visible : true}>
    <FileViewerPanel tabId={id} active={source === "active" ? visible : true} />
  </PanelVisibilityContext.Provider>;
  const view = render(panel(true));
  const baseline = await screen.findByRole("button", { name: "文件比较基线" });
  fireEvent.click(baseline);
  fireEvent.click(screen.getByRole("menuitemradio", { name: "feature" }));
  await waitFor(() => expect(mocks.diff).toHaveBeenLastCalledWith(owner, "file.ts", "feature", expect.any(AbortSignal)));
  fireEvent.click(screen.getByRole("button", { name: "在文件中搜索" }));
  const search = screen.getByRole("textbox", { name: "在文件中搜索" });
  fireEvent.change(search, { target: { value: "saved" } });
  const content = view.container.querySelector<HTMLElement>(".mc-file-viewer-content")!;
  content.scrollTop = 120;
  const options = screen.getByRole("button", { name: "打开选项菜单" });
  fireEvent.click(options);
  expect(screen.getByRole("menu", { name: "打开选项菜单" })).toBeTruthy();
  view.rerender(panel(false));
  expect(screen.queryByRole("menu")).toBeNull();
  expect(options.getAttribute("aria-expanded")).toBe("false");
  view.rerender(panel(true));
  await act(async () => {});
  expect(screen.queryByRole("menu")).toBeNull();
  expect(screen.getByRole("textbox", { name: "在文件中搜索" })).toBe(search);
  expect((search as HTMLInputElement).value).toBe("saved");
  expect(screen.getByRole("button", { name: "文件比较基线" }).textContent).toBe("feature");
  expect(content.scrollTop).toBe(120);
  expect(useAppStore.getState().draft).toBe("unsent");
  fireEvent.click(baseline);
  view.rerender(panel(false));
  view.rerender(panel(true));
  expect(screen.queryByRole("menu")).toBeNull();
});

it("closes old file actions when the same tab changes owner and opens actions for its current file", async () => {
  const one = { scopeId: "home:C:/one", conversationId: null, executionCwd: "C:/one", browserRoot: "C:/one" };
  const two = { scopeId: "home:D:/two", conversationId: null, executionCwd: "D:/two", browserRoot: "D:/two" };
  const id = useAppStore.getState().openWorkspaceFileTab(one, "first.ts");
  render(<FileViewerPanel tabId={id} />);
  await screen.findByText("saved first.ts");
  fireEvent.click(screen.getByRole("button", { name: "打开选项菜单" }));
  expect(screen.getByRole("menuitem", { name: "Cursor" })).toBeTruthy();
  await act(async () => useAppStore.setState(state => ({ workspaceFileTabs: state.workspaceFileTabs.map(tab => tab.id === id
    ? { ...tab, owner: two, path: "second.ts", title: "second.ts" } : tab) })));
  await screen.findByText("saved second.ts");
  expect(screen.queryByRole("menu")).toBeNull();
  expect(mocks.external).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "打开选项菜单" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Cursor" }));
  expect(mocks.external).toHaveBeenCalledExactlyOnceWith("D:/two/second.ts", undefined,
    { targetId: "cursor", workspaceRoot: "D:/two", persistPreferredTargetPath: "D:/two" });
});

it("requests one comparison per file refresh even when the refreshed branches arrive as a new object", async () => {
  const owner = { scopeId: "home:C:/home", conversationId: null, executionCwd: "C:/home", browserRoot: "C:/home" };
  mocks.branches.mockImplementation(async () => gitBranches());
  const id = useAppStore.getState().openWorkspaceFileTab(owner, "file.ts");
  render(<FileViewerPanel tabId={id} />);
  await waitFor(() => expect(mocks.diff).toHaveBeenCalledTimes(1));
  await act(async () => useAppStore.setState({ fileTreeVersion: 1 }));
  await waitFor(() => expect(mocks.preview).toHaveBeenCalledTimes(2));
  expect(mocks.branches).toHaveBeenCalledTimes(2);
  expect(mocks.diff).toHaveBeenCalledTimes(2);
  expect(mocks.diff).toHaveBeenLastCalledWith(owner, "file.ts", "main", expect.any(AbortSignal));
  expect(useAppStore.getState().draft).toBe("unsent");
});

it("refreshes original bytes when an image changes without changing media type", async () => {
  const owner = { scopeId: "home:C:/home", conversationId: null, executionCwd: "C:/home", browserRoot: "C:/home" };
  const original = new Blob(["first image"]), updated = new Blob(["updated image"]);
  mocks.preview.mockImplementation(async () => ({ path: "picture.png", file_name: "picture.png", content: "", media_type: "image/png", kind: "image", truncated: false }));
  mocks.original.mockResolvedValueOnce(original).mockResolvedValueOnce(updated);
  const id = useAppStore.getState().openWorkspaceFileTab(owner, "picture.png");
  render(<FileViewerPanel tabId={id} />);
  expect((await screen.findByRole("img", { name: "picture.png" })).getAttribute("src")).toBe("blob:file-viewer-1");
  await act(async () => useAppStore.setState({ fileTreeVersion: 1 }));
  await waitFor(() => expect(screen.getByRole("img", { name: "picture.png" }).getAttribute("src")).toBe("blob:file-viewer-2"));
  expect(mocks.original).toHaveBeenCalledTimes(2);
  expect(mocks.createUrl).toHaveBeenLastCalledWith(updated);
  expect(mocks.revokeUrl).toHaveBeenCalledWith("blob:file-viewer-1");
});

it("ignores an old raw-media result after a newer file revision has loaded", async () => {
  const owner = { scopeId: "home:C:/home", conversationId: null, executionCwd: "C:/home", browserRoot: "C:/home" };
  let completeOld!: (blob: Blob) => void;
  const current = new Blob(["current bytes"]);
  mocks.preview.mockImplementation(async () => ({ path: "picture.png", file_name: "picture.png", content: "", media_type: "image/png", kind: "image", truncated: false }));
  mocks.original.mockImplementationOnce(() => new Promise<Blob>(resolve => { completeOld = resolve; })).mockResolvedValueOnce(current);
  const id = useAppStore.getState().openWorkspaceFileTab(owner, "picture.png");
  render(<FileViewerPanel tabId={id} />);
  await waitFor(() => expect(mocks.original).toHaveBeenCalledTimes(1));
  const oldSignal = mocks.original.mock.calls[0][2] as AbortSignal;
  await act(async () => useAppStore.setState({ fileTreeVersion: 1 }));
  await screen.findByRole("img", { name: "picture.png" });
  expect(oldSignal.aborted).toBe(true);
  await act(async () => completeOld(new Blob(["obsolete bytes"])));
  expect(mocks.createUrl).toHaveBeenCalledExactlyOnceWith(current);
  expect(screen.getByRole("img", { name: "picture.png" }).getAttribute("src")).toBe("blob:file-viewer-1");
});

it("keeps a new execution owner out of the previous owner's branch and patch state", async () => {
  const one = { scopeId: "home:C:/one", conversationId: null, executionCwd: "C:/one", browserRoot: "C:/one" };
  const two = { scopeId: "home:D:/two", conversationId: null, executionCwd: "D:/two", browserRoot: "D:/two" };
  let completeBranches!: (value: ReturnType<typeof gitBranches>) => void;
  mocks.branches.mockResolvedValueOnce(gitBranches("main")).mockImplementationOnce(() => new Promise(resolve => { completeBranches = resolve; }));
  mocks.preview.mockImplementation(async owner => ({ path: "file.ts", file_name: "file.ts", content: owner.executionCwd === one.executionCwd ? "from-one\n" : "from-two\n", media_type: "text/plain", kind: "text", truncated: false }));
  mocks.diff.mockImplementation(async owner => ({ diff: `--- a/file.ts\n+++ b/file.ts\n@@ -1 +1 @@\n-old\n+${owner.executionCwd === one.executionCwd ? "from-one" : "from-two"}\n` }));
  const id = useAppStore.getState().openWorkspaceFileTab(one, "file.ts");
  render(<FileViewerPanel tabId={id} />);
  await waitFor(() => expect(mocks.diff).toHaveBeenCalledTimes(1));
  await act(async () => useAppStore.setState(state => ({ workspaceFileTabs: state.workspaceFileTabs.map(tab => tab.id === id ? { ...tab, owner: two } : tab) })));
  await screen.findByText("from-two");
  expect(screen.queryByText("from-one")).toBeNull();
  expect(screen.queryByRole("button", { name: "文件比较基线" })).toBeNull();
  expect(mocks.diff).toHaveBeenCalledTimes(1);
  await act(async () => completeBranches(gitBranches("develop")));
  await waitFor(() => expect(mocks.diff).toHaveBeenCalledTimes(2));
  expect(mocks.diff).toHaveBeenLastCalledWith(two, "file.ts", "develop", expect.any(AbortSignal));
});

it("retains the selected execution baseline when only the authoritative browsing root is reconciled", async () => {
  const cwd = "C:/tasks/run";
  const owner = { scopeId: "conversation:a", conversationId: "a", executionCwd: cwd, browserRoot: cwd };
  useAppStore.setState({ conversationId: "a", workingDirectory: cwd, pendingConversationSwitchId: null });
  mocks.branches.mockImplementation(async () => gitBranches("main"));
  const id = useAppStore.getState().openWorkspaceFileTab(owner, "file.ts");
  render(<FileViewerPanel tabId={id} />);
  await waitFor(() => expect(mocks.diff).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole("button", { name: "文件比较基线" }));
  fireEvent.click(screen.getByRole("menuitemradio", { name: "feature" }));
  await waitFor(() => expect(mocks.diff).toHaveBeenLastCalledWith(owner, "file.ts", "feature", expect.any(AbortSignal)));
  act(() => useAppStore.setState({ conversations: [{ id: "a", title: "A", updatedAt: "", executionCwd: cwd, projectlessBrowserRoot: "C:/tasks" }] }));
  await waitFor(() => expect(mocks.preview).toHaveBeenLastCalledWith({ ...owner, browserRoot: "C:/tasks" }, "run/file.ts", expect.any(AbortSignal)));
  await waitFor(() => expect(mocks.diff).toHaveBeenLastCalledWith({ ...owner, browserRoot: "C:/tasks" }, "run/file.ts", "feature", expect.any(AbortSignal)));
  expect(mocks.branches).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: "文件比较基线" }).textContent).toBe("feature");
  expect(workspaceFileAbsolutePath(useAppStore.getState().workspaceFileTabs[0].owner, useAppStore.getState().workspaceFileTabs[0].path)).toBe("C:/tasks/run/file.ts");
});

it("keeps source coloring dormant for a rendered Markdown preview and a hidden file tab", async () => {
  const owner = { scopeId: "home:C:/home", conversationId: null, executionCwd: "C:/home", browserRoot: "C:/home" };
  mocks.preview.mockResolvedValue({ path: "report.md", file_name: "report.md", content: "# Report", media_type: "text/markdown", kind: "document", truncated: false });
  const id = useAppStore.getState().openWorkspaceFileTab(owner, "report.md", undefined, undefined, { isPreview: true });
  const view = render(<FileViewerPanel tabId={id} />);
  await screen.findByRole("heading", { name: "Report" });
  expect(mocks.colorize.mock.calls.every(([rows]) => rows.length === 0)).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "查看源代码" }));
  expect(mocks.colorize.mock.lastCall?.[0]).toEqual([expect.objectContaining({ text: "# Report" })]);
  view.rerender(<FileViewerPanel tabId={id} active={false} />);
  expect(mocks.colorize.mock.lastCall?.[0]).toEqual([]);
});

it("keeps two file contents independent and exposes flat installed targets with captured Save As", async () => {
  const owner = { scopeId: "home:C:/home", conversationId: null, executionCwd: "C:/home", browserRoot: "C:/home" };
  const a = useAppStore.getState().openWorkspaceFileTab(owner, "a.ts"), b = useAppStore.getState().openWorkspaceFileTab(owner, "b.ts");
  render(<><FileViewerPanel tabId={a} /><FileViewerPanel tabId={b} /></>);
  await screen.findByText("saved a.ts"); await screen.findByText("saved b.ts");
  const first = screen.getByRole("region", { name: "文件查看器：a.ts" });
  fireEvent.click(within(first).getByRole("button", { name: "在文件中搜索" }));
  fireEvent.change(within(first).getByRole("textbox", { name: "在文件中搜索" }), { target: { value: "saved" } });
  expect(within(first).getByText("1/1")).toBeTruthy();
  expect(screen.getByText("saved b.ts")).toBeTruthy();
  fireEvent.click(within(first).getByRole("button", { name: "打开选项菜单" }));
  expect(screen.getAllByRole("menuitem").map(item => item.textContent?.trim())).toEqual(["Cursor", "打开所在文件夹", "另存为…"]);
  expect(screen.queryByRole("menuitem", { name: "打开方式" })).toBeNull();
  fireEvent.click(screen.getByRole("menuitem", { name: "另存为…" }));
  await waitFor(() => expect(mocks.original).toHaveBeenCalledWith(owner, "a.ts"));
  expect(mocks.save).toHaveBeenCalledWith(await mocks.original.mock.results[0].value, "a.ts");
  expect(useAppStore.getState()).toMatchObject({ previewArtifact: null, conversationId: null, workingDirectory: "C:/home", draft: "unsent" });
});

it("rebuilds a restored projectless file reference from canonical owner metadata while preserving its tab and draft", async () => {
  const cwd = "C:/tasks/2026-10-25/report", path = "2026-10-25/report/outputs/alignment-check.md";
  const owner = { scopeId: "conversation:a", conversationId: "a", executionCwd: cwd, browserRoot: cwd };
  const stored = { id: "restored", owner, path: "outputs/alignment-check.md", title: "alignment-check.md", open: true, pinned: true, pane: "right" as const, line: 2 };
  let rejectOld!: (reason: Error) => void;
  mocks.preview.mockImplementation((requestOwner, requestPath) => requestOwner.browserRoot === cwd
    ? new Promise((_resolve, reject) => { rejectOld = reject; })
    : Promise.resolve({ path: requestPath, file_name: "alignment-check.md", content: "# 对齐验收\n右侧栏最近产物验收", media_type: "text/markdown", kind: "document", truncated: false }));
  useAppStore.setState({ workspaceFileTabs: [stored], conversationId: "a", workingDirectory: cwd, conversations: [], rightStackTab: "file:restored" });
  render(<FileViewerPanel tabId={stored.id} />);
  await waitFor(() => expect(mocks.preview).toHaveBeenCalledTimes(1));
  act(() => useAppStore.setState({ conversations: [{ id: "a", title: "A", updatedAt: "", executionCwd: cwd, projectlessBrowserRoot: "C:/tasks" }] }));
  await screen.findByText("# 对齐验收");
  await act(async () => { rejectOld(new Error("File not found: outputs/alignment-check.md")); });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(mocks.preview).toHaveBeenLastCalledWith({ ...owner, browserRoot: "C:/tasks" }, path, expect.any(AbortSignal));
  expect(useAppStore.getState().workspaceFileTabs).toEqual([{ ...stored, owner: { ...owner, browserRoot: "C:/tasks" }, path }]);
  expect(useAppStore.getState()).toMatchObject({ conversationId: "a", workingDirectory: cwd, rightStackTab: "file:restored", draft: "unsent" });
});

it("passes the real file owner through rendered Markdown links and resolves relative images against the document directory", async () => {
  const base = "C:/tasks", cwd = `${base}/2026-10-25/report`;
  const owner = { scopeId: "conversation:a", conversationId: "a", executionCwd: cwd, browserRoot: base };
  mocks.preview.mockResolvedValue({ path: "2026-10-25/report/outputs/report.md", file_name: "report.md",
    content: `[Linked](<${cwd}/outputs/linked.md#L7>)\n\n![Local](pixel.png)\n\n![Sibling](../assets/logo.png)`, media_type: "text/markdown", kind: "document", truncated: false });
  useAppStore.setState({ conversationId: "a", workingDirectory: cwd, conversations: [{ id: "a", title: "A", updatedAt: "", executionCwd: cwd, projectlessBrowserRoot: base }],
    pendingConversationSwitchId: null, workspaceFileTabs: [] });
  const id = useAppStore.getState().openWorkspaceFileTab(owner, "2026-10-25/report/outputs/report.md");
  render(<FileViewerPanel tabId={id} />);
  fireEvent.click(await screen.findByRole("button", { name: "渲染 Markdown" }));
  fireEvent.click(await screen.findByRole("button", { name: "Linked" }));
  const linked = useAppStore.getState().workspaceFileTabs.at(-1)!;
  expect(linked.owner).toEqual(owner);
  expect(linked.line).toBe(7);
  expect(useAppStore.getState().rightStackTab).toBe(`file:${linked.id}`);
  expect(workspaceFileAbsolutePath(linked.owner, linked.path)).toBe(`${cwd}/outputs/linked.md`);
  const img = screen.getByRole("img", { name: "Local" });
  const raw = new URL(img.getAttribute("src")!);
  expect(raw.pathname).toBe("/api/workspace/browser-raw");
  expect(Object.fromEntries(raw.searchParams)).toMatchObject({ session_id: "file-viewer-session", conversation_id: "a", execution_cwd: cwd,
    path: "2026-10-25/report/outputs/pixel.png" });
  const sibling = new URL(screen.getByRole("img", { name: "Sibling" }).getAttribute("src")!);
  expect(Object.fromEntries(sibling.searchParams)).toMatchObject({ path: "2026-10-25/report/assets/logo.png" });
  fireEvent.click(img.closest("button")!);
  expect(useAppStore.getState().workspaceFileTabs.at(-1)?.owner).toEqual(owner);
  expect(useAppStore.getState()).toMatchObject({ conversationId: "a", workingDirectory: cwd, draft: "unsent" });
});

it("opens a Markdown file reference rendered, while retaining source and file-tree controls", async () => {
  const owner = { scopeId: "conversation:a", conversationId: "a", executionCwd: "C:/project", browserRoot: "C:/project" };
  mocks.preview.mockResolvedValue({ path: "report.md", file_name: "report.md", content: "# Report\n\nReal content.",
    media_type: "text/markdown", kind: "document", truncated: false });
  const id = useAppStore.getState().openWorkspaceFileTab(owner, "report.md", "Report link", undefined, { isPreview: true });
  const view = render(<FileViewerPanel tabId={id} />);
  expect(await screen.findByRole("heading", { name: "Report" })).toBeTruthy();
  expect(view.container.querySelector(".mc-file-viewer")?.getAttribute("data-compact-markdown")).toBe("true");
  expect(screen.queryByRole("tree", { name: "captured file tree" })).toBeNull();
  mocks.original.mockResolvedValue({ text: async () => "complete file content" });
  fireEvent.click(screen.getByRole("button", { name: "复制文件内容" }));
  await waitFor(() => expect(mocks.copy).toHaveBeenCalledWith("complete file content", "文件内容", "文件内容已复制。"));
  expect(mocks.original).toHaveBeenCalledWith(owner, "report.md");
  fireEvent.click(screen.getByRole("button", { name: "在 Cursor 中打开" }));
  await waitFor(() => expect(mocks.external).toHaveBeenCalledWith("C:/project/report.md", undefined,
    { targetId: "cursor", workspaceRoot: "C:/project", persistPreferredTargetPath: "C:/project" }));
  fireEvent.click(screen.getByRole("button", { name: "查看源代码" }));
  expect(screen.getByText("# Report")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "查看预览" }));
  expect(screen.getByRole("heading", { name: "Report" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "切换文件树" }));
  expect(screen.getByRole("tree", { name: "captured file tree" })).toBeTruthy();
  expect(useAppStore.getState().draft).toBe("unsent");
});

it("keeps non-Markdown file references in the code viewer", async () => {
  const owner = { scopeId: "conversation:a", conversationId: "a", executionCwd: "C:/project", browserRoot: "C:/project" };
  mocks.preview.mockResolvedValue({ path: "note.txt", file_name: "note.txt", content: "# Not a heading",
    media_type: "text/plain", kind: "text", truncated: false });
  const id = useAppStore.getState().openWorkspaceFileTab(owner, "note.txt", undefined, undefined, { isPreview: true });
  render(<FileViewerPanel tabId={id} />);
  expect(await screen.findByText("# Not a heading")).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "Not a heading" })).toBeNull();
});
