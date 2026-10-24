/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => {
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  return { preview: vi.fn(), branches: vi.fn(), diff: vi.fn(), original: vi.fn(), external: vi.fn(), save: vi.fn() };
});
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
vi.mock("../protocol/file-workspace", async importOriginal => ({ ...await importOriginal<typeof import("../protocol/file-workspace")>(),
  readFileWorkspacePreview: mocks.preview, readFileWorkspaceBranches: mocks.branches, readFileWorkspaceDiff: mocks.diff, readFileWorkspaceOriginal: mocks.original }));
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: { fileOpenTarget: "cursor", fileOpenTargets: [
  { id: "cursor", label: "Cursor", kind: "editor" }, { id: "fileManager", label: "Files", kind: "fileManager" }], fileOpenTargetPerPath: {} } }) }));
vi.mock("../desktop/runtime", async importOriginal => ({ ...await importOriginal<typeof import("../desktop/runtime")>(), desktop: () => ({ openPreferredPath: mocks.external }), runtime: () => ({}) }));
vi.mock("../shell/tabs/ConversationFilesTree", () => ({ ConversationFilesTree: () => <div role="tree" aria-label="captured file tree" /> }));
vi.mock("../lib/monaco-colorize", () => ({ guessLanguageFromPath: () => "plaintext", useColorizedLines: () => null }));
vi.mock("../lib/save-blob", () => ({ saveBlobFile: mocks.save }));
import { useAppStore } from "../stores";
import { FileViewerPanel } from "./FileViewerPanel";

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  mocks.preview.mockReset().mockImplementation(async (_owner, path) => ({ path, file_name: path, content: `saved ${path}`, media_type: "text/plain", kind: "text", truncated: false }));
  mocks.branches.mockReset().mockResolvedValue({ is_git_repo: false, current_branch: null, default_base_branch: null, branches: [], commits: [] });
  mocks.diff.mockReset().mockResolvedValue({ diff: "" }); mocks.external.mockReset(); mocks.save.mockReset();
  mocks.original.mockReset().mockResolvedValue(new Blob(["original bytes"]));
  useAppStore.setState({ workspaceFileTabs: [], isConnected: true, conversationId: null, workingDirectory: "C:/home", previewArtifact: null,
    draft: "unsent", panelSlots: [{ id: "chat", kind: "chat", focused: true }], conversations: [] });
});
afterEach(cleanup);

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
