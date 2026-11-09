/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { WorkspaceSearchPanel } from "./WorkspaceSearchPanel";
import { searchWorkspaceText } from "../protocol/workspace-search";
import { applyWorkspaceBufferEdits } from "./applyWorkspaceBufferEdits";

vi.hoisted(() => Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../protocol/workspace-search", async (original) => ({ ...await original<typeof import("../protocol/workspace-search")>(), searchWorkspaceText: vi.fn() }));
vi.mock("./applyWorkspaceBufferEdits", async (original) => ({ ...await original<typeof import("./applyWorkspaceBufferEdits")>(), applyWorkspaceBufferEdits: vi.fn(async () => {}) }));
vi.mock("./monacoEditorFeatures", () => ({ loadMiniCodeEditorFeatures: vi.fn(async () => {}) }));
vi.mock("@monaco-editor/react", () => ({ loader: { config: vi.fn() }, DiffEditor: ({ original, modified }: { original: string; modified: string }) => <div aria-label="只读实际差异"><del>{original}</del><ins>{modified}</ins></div> }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const content = "😀 foo\r\nfoo\r\n";
const match = (id: string, offset: number, line: number, column: number) => ({ id, offset, length: 3, line, column, end_line: line, end_column: column + 3, text: "foo", snippet: "foo", groups: [], named_groups: {} });

describe("project search and selected replacement UI", () => {
  it("invalidates a pending query before a late result can enable replacement", async () => {
    const workspace = "/search-query-invalidation";
    useAppStore.setState({ workingDirectory: workspace, editorTabs: [] });
    let finish!: (value: Awaited<ReturnType<typeof searchWorkspaceText>>) => void;
    vi.mocked(searchWorkspaceText).mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    render(<WorkspaceSearchPanel />);
    fireEvent.change(screen.getByRole("textbox", { name: "搜索项目内容" }), { target: { value: "foo" } });
    fireEvent.click(screen.getByRole("button", { name: "搜索", exact: true }));
    const signal = vi.mocked(searchWorkspaceText).mock.calls[0][3]!;
    fireEvent.change(screen.getByRole("textbox", { name: "搜索项目内容" }), { target: { value: "bar" } });
    expect(signal.aborted).toBe(true);
    finish({ workspace_root: workspace, files: [], match_count: 7, truncated: false, issues: [] });
    await waitFor(() => expect(screen.getByRole("button", { name: "搜索", exact: true }).hasAttribute("disabled")).toBe(false));
    expect(screen.queryByText(/已显示 7/)).toBeNull();
    expect(screen.getByRole("button", { name: /预览选中替换/ }).hasAttribute("disabled")).toBe(true);
  });
  it("searches live buffers, reveals an exact UTF-16 range, previews outside sidebar containment and invalidates applied results", async () => {
    const workspace = "/search-panel-selection";
    useAppStore.setState({ workingDirectory: workspace, appMode: "code", panelSlots: [{ id: "editor", kind: "editor", focused: true }], editorTabs: [
      { id: "existing", path: "src/main.ts", content, original: "disk baseline", contentHash: "disk-hash", loading: false },
    ], activeTabPath: "src/main.ts", activeEditorPath: "src/main.ts", editorOpenRequests: [] });
    vi.mocked(searchWorkspaceText).mockResolvedValue({ workspace_root: workspace, match_count: 2, truncated: false, issues: [], files: [
      { path: "src/main.ts", content, original: "disk baseline", content_hash: "disk-hash", size_bytes: 16, read_only: false, from_buffer: true, matches: [match("m1", 3, 1, 4), match("m2", 8, 2, 1)] },
    ] });
    const mounted = render(<div style={{ contain: "layout paint", overflow: "hidden", width: 240 }}><WorkspaceSearchPanel /></div>);
    fireEvent.change(screen.getByRole("textbox", { name: "搜索项目内容" }), { target: { value: "foo" } });
    fireEvent.click(screen.getByRole("button", { name: "搜索", exact: true }));
    await screen.findByText(/已显示 2 处匹配/);
    expect(vi.mocked(searchWorkspaceText).mock.calls[0][2]).toEqual([{ path: "src/main.ts", content, original: "disk baseline", content_hash: "disk-hash", read_only: false }]);
    fireEvent.click(screen.getByRole("button", { name: /1:4/ }));
    expect(useAppStore.getState().editorOpenRequests.at(-1)).toMatchObject({ path: "src/main.ts", line: 1, column: 4, endLine: 1, endColumn: 7 });
    fireEvent.click(screen.getByRole("checkbox", { name: "选择 src/main.ts 第2行第1列" }));
    fireEvent.click(screen.getByText("替换", { selector: "summary" }));
    fireEvent.change(screen.getByRole("textbox", { name: "替换为" }), { target: { value: "bar" } });
    fireEvent.click(screen.getByRole("button", { name: /预览选中替换/ }));
    const dialog = await screen.findByRole("dialog", { name: "项目替换预览" });
    expect(mounted.container.contains(dialog)).toBe(false);
    expect(dialog.parentElement?.parentElement).toBe(document.body);
    fireEvent.click(screen.getByRole("button", { name: "应用到编辑缓冲区" }));
    await waitFor(() => expect(applyWorkspaceBufferEdits).toHaveBeenCalledOnce());
    expect(vi.mocked(applyWorkspaceBufferEdits).mock.calls[0][1][0]).toMatchObject({ before: content, after: "😀 bar\r\nfoo\r\n", edits: [{ offset: 3, length: 3, text: "bar" }] });
    await waitFor(() => expect(screen.queryByText(/已显示 2 处匹配/)).toBeNull());
    mounted.unmount();
    render(<WorkspaceSearchPanel />);
    expect((screen.getByRole("textbox", { name: "搜索项目内容" }) as HTMLInputElement).value).toBe("foo");
    fireEvent.click(screen.getByText("替换", { selector: "summary" }));
    expect((screen.getByRole("textbox", { name: "替换为" }) as HTMLInputElement).value).toBe("bar");
    expect(screen.queryByRole("button", { name: /1:4/ })).toBeNull();
    expect((screen.getByRole("button", { name: /预览选中替换/ }) as HTMLButtonElement).disabled).toBe(true);
  });
  it("shows truncated results as only displayed matches and keeps closed-panel query options", async () => {
    useAppStore.setState({ workingDirectory: "/search-panel-truncated", editorTabs: [], activeTabPath: null, activeEditorPath: null });
    vi.mocked(searchWorkspaceText).mockResolvedValue({ workspace_root: "/search-panel-truncated", files: [], match_count: 1000, truncated: true, issues: [] });
    const first = render(<WorkspaceSearchPanel />);
    fireEvent.change(screen.getByRole("textbox", { name: "搜索项目内容" }), { target: { value: "foo" } });
    fireEvent.click(screen.getByRole("button", { name: "全词匹配" }));
    fireEvent.click(screen.getByRole("button", { name: "搜索", exact: true }));
    await screen.findByText(/结果已截断，仅可替换显示的匹配/);
    first.unmount();
    render(<WorkspaceSearchPanel />);
    expect(screen.getByRole("button", { name: "全词匹配" }).getAttribute("aria-pressed")).toBe("true");
  });
});
