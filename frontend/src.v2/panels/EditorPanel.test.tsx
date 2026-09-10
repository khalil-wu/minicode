/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fsReadFileInfo, fsSearchFiles, isDesktop } from "../desktop/runtime";
import { compareWriteWorkspaceFile, readWorkspaceFile, searchWorkspaceFiles } from "../protocol/workspace";
import { pushToast } from "../overlays/ToastContainer";
import { useAppStore } from "../stores";
import { clearEditorWorkspaceBufferCacheForTests, persistEditorTabs } from "../stores/shared-helpers";
import { EditorPanel } from "./EditorPanel";

const editorMocks = vi.hoisted(() => ({
  actions: [] as Array<{ run: (editor: unknown) => void }>,
  showConfirm: vi.fn(),
}));

vi.mock("../overlays/DialogService", () => ({ showConfirm: editorMocks.showConfirm }));
vi.mock("./PdfAttachmentPreview", () => ({
  PdfAttachmentPreview: ({ url, name }: { url: string; name: string }) => (
    <div data-testid="pdf-preview" data-url={url} aria-label={`PDF 预览 ${name}`} />
  ),
}));

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
});

vi.mock("@monaco-editor/react", async () => {
  const ReactModule = await import("react");
  return {
    loader: { config: vi.fn() },
    default: function MockMonacoEditor({ value, path, onChange, onMount, options }: {
      value: string;
      path: string;
      onChange?: (value: string) => void;
      onMount: (editor: unknown) => void;
      options?: { readOnly?: boolean };
    }) {
      ReactModule.useEffect(() => {
        let dispose: () => void;
        onMount({
          addAction: (action: { run: (editor: unknown) => void }) => editorMocks.actions.push(action),
          focus: vi.fn(),
          onDidChangeCursorPosition: vi.fn(),
          onDidDispose: (listener: () => void) => { dispose = listener; },
        });
        return () => dispose?.();
      }, []);
      return ReactModule.createElement("textarea", {
        "data-testid": "monaco-editor",
        "data-model-path": path,
        value,
        readOnly: options?.readOnly,
        onChange: (event: React.ChangeEvent<HTMLTextAreaElement>) => onChange?.(event.currentTarget.value),
      });
    },
  };
});

vi.mock("monaco-editor", () => ({
  editor: {},
  languages: {},
}));

vi.mock("monaco-editor/editor/editor.api.js", () => ({ editor: {}, languages: {} }));
vi.mock("monaco-editor/languages/definitions/typescript/register.js", () => ({}));
vi.mock("monaco-editor/languages/definitions/javascript/register.js", () => ({}));
vi.mock("monaco-editor/languages/definitions/css/register.js", () => ({}));
vi.mock("monaco-editor/languages/definitions/html/register.js", () => ({}));
vi.mock("monaco-editor/languages/definitions/markdown/register.js", () => ({}));
vi.mock("monaco-editor/languages/definitions/python/register.js", () => ({}));

vi.mock("../desktop/runtime", () => ({
  desktop: vi.fn(() => null),
  fsCompareWriteFile: vi.fn(),
  fsReadFileInfo: vi.fn(),
  fsSearchFiles: vi.fn(),
  isDesktop: vi.fn(() => false),
  revealPath: vi.fn(),
}));

vi.mock("../protocol/workspace", () => ({
  compareWriteWorkspaceFile: vi.fn(),
  readWorkspaceFile: vi.fn(),
  searchWorkspaceFiles: vi.fn(),
}));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

describe("EditorPanel", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    editorMocks.actions.length = 0;
    clearEditorWorkspaceBufferCacheForTests();
    localStorage.clear();
    vi.mocked(isDesktop).mockReturnValue(false);
    vi.mocked(fsSearchFiles).mockResolvedValue([]);
    vi.mocked(searchWorkspaceFiles).mockResolvedValue([]);
    useAppStore.setState({
      themeMode: "dark",
      workingDirectory: "C:\\projects\\demo",
      editorOpenRequests: [],
      activeEditorOpenRequestId: null,
      activeEditorPath: null,
      fileChanges: [],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false, live: null },
      diffReview: null,
      rightStackTab: "preview",
      panelSlots: [{ id: "editor", kind: "editor", label: "Editor", focused: true }],
      editorTabs: [],
      activeTabPath: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("reports basename resolution failures and can open the file when the service recovers", async () => {
    vi.mocked(searchWorkspaceFiles).mockRejectedValueOnce(new Error("Search service unavailable"));
    useAppStore.getState().openEditorFile("README.md");
    render(<EditorPanel />);

    await waitFor(() => expect(pushToast).toHaveBeenCalledWith("无法定位文件：Search service unavailable", "error", 5000));
    expect(useAppStore.getState().editorTabs).toHaveLength(0);
    vi.mocked(searchWorkspaceFiles).mockResolvedValueOnce([{ path: "docs/README.md", name: "README.md", score: 1 }]);
    vi.mocked(readWorkspaceFile).mockResolvedValueOnce({ path: "docs/README.md", content: "recovered file", content_hash: "recovered-hash" });

    act(() => useAppStore.getState().openEditorFile("README.md"));

    const editor = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;
    expect(editor.value).toBe("recovered file");
    expect(useAppStore.getState().activeTabPath).toBe("docs/README.md");
  });

  it.each([false, true])("opens exact root files without a tree scan even when basename search is unavailable (desktop=%s)", async (desktopMode) => {
    vi.mocked(isDesktop).mockReturnValue(desktopMode);
    vi.mocked(fsSearchFiles).mockImplementation(() => new Promise(() => {}));
    vi.mocked(searchWorkspaceFiles).mockImplementation(() => new Promise(() => {}));
    vi.mocked(readWorkspaceFile).mockResolvedValue({ path: "entry.py", content: "root file", content_hash: "root" });
    vi.mocked(fsReadFileInfo).mockResolvedValue({ content: "root file", contentHash: "root", sizeBytes: 9 });
    useAppStore.getState().openEditorFile("entry.py", "entry.py", { exact: true, line: 2 });
    render(<EditorPanel />);

    const editor = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;
    expect(editor.value).toBe("root file");
    expect(useAppStore.getState().activeTabPath).toBe("entry.py");
    expect(fsSearchFiles).not.toHaveBeenCalled();
    expect(searchWorkspaceFiles).not.toHaveBeenCalled();
  });

  it("preserves an absolute root file as an exact path through workspace normalization", async () => {
    vi.mocked(readWorkspaceFile).mockResolvedValue({ path: "entry.py", content: "absolute root", content_hash: "root" });
    useAppStore.getState().openEditorFile("C:\\projects\\demo\\entry.py");
    render(<EditorPanel />);
    expect((await screen.findByTestId("monaco-editor") as HTMLTextAreaElement).value).toBe("absolute root");
    expect(searchWorkspaceFiles).not.toHaveBeenCalled();
  });

  it("uses localized guidance when Code mode has no open file", () => {
    const { container } = render(<EditorPanel />);

    expect(container.querySelector(".editor-empty-file-icon svg")).toBeTruthy();
    expect(screen.getAllByText("未打开文件").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("从左侧项目文件或搜索中打开工作区文件。")).toBeTruthy();
  });

  it("opens Markdown files in edit mode by default and renders through the Markdown mode switch", async () => {
    useAppStore.setState({
      editorTabs: [{
        id: "editor-fixture-1",
        path: "docs/README.md",
        content: "# Hello\n\n![Logo](./assets/logo.svg)",
        original: "# Hello\n\n![Logo](./assets/logo.svg)",
        loading: false,
        error: null,
        largeFile: false,
      }],
      activeTabPath: "docs/README.md",
    });

    render(<EditorPanel />);

    expect(await screen.findByTestId("monaco-editor")).toBeTruthy();
    expect(screen.getByRole("tablist", { name: "Markdown 视图模式" })).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "预览" }));

    expect(screen.getByRole("heading", { name: "Hello" })).toBeTruthy();
    const image = screen.getByRole("img", { name: "Logo" });
    expect(image.getAttribute("loading")).toBe("lazy");
    expect(image.getAttribute("src")).toContain("docs%2Fassets%2Flogo.svg");
    expect(new URL(image.getAttribute("src")!).searchParams.get("workspace_root")).toBe("C:\\projects\\demo");
    expect(screen.getByRole("tab", { name: "编辑" }).getAttribute("aria-selected")).toBe("false");
    expect(screen.getByRole("tab", { name: "预览" }).getAttribute("aria-selected")).toBe("true");
  });

  it("keeps malformed percent-encoded Markdown fragments renderable in preview", async () => {
    useAppStore.setState({
      editorTabs: [{
        id: "editor-fixture-2",
        path: "docs/README.md",
        content: "[Jump](#broken%fragment)\n\n## Broken%fragment",
        original: "[Jump](#broken%fragment)\n\n## Broken%fragment",
        loading: false,
        error: null,
        largeFile: false,
      }],
      activeTabPath: "docs/README.md",
    });

    render(<EditorPanel />);
    fireEvent.click(await screen.findByRole("tab", { name: "预览" }));

    expect(screen.getByRole("heading", { name: "Broken%fragment" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Jump" }).getAttribute("href")).toContain("brokenfragment");
  });

  it("renders workspace-local absolute Markdown assets through relative raw URLs", async () => {
    useAppStore.setState({
      editorTabs: [{
        id: "editor-fixture-3",
        path: "docs/README.md",
        content: "# Hello\n\n![Logo](C:\\projects\\demo\\docs\\assets\\logo.svg)",
        original: "# Hello\n\n![Logo](C:\\projects\\demo\\docs\\assets\\logo.svg)",
        loading: false,
        error: null,
        largeFile: false,
      }],
      activeTabPath: "docs/README.md",
    });

    render(<EditorPanel />);
    fireEvent.click(await screen.findByRole("tab", { name: "预览" }));

    const image = screen.getByRole("img", { name: "Logo" });
    expect(image.getAttribute("src")).toContain("docs%2Fassets%2Flogo.svg");
    const url = new URL(image.getAttribute("src")!);
    expect(url.searchParams.get("path")).toBe("docs/assets/logo.svg");
    expect(url.searchParams.get("workspace_root")).toBe("C:\\projects\\demo");
  });

  it("can hide its own file tab chrome when hosted by the main work area", () => {
    useAppStore.setState({
      editorTabs: [{
        id: "editor-fixture-4",
        path: "random-1.txt",
        content: "hello",
        original: "hello",
        loading: false,
        error: null,
        largeFile: false,
      }],
      activeTabPath: "random-1.txt",
    });

    const { container } = render(<EditorPanel chrome="minimal" />);

    expect(container.querySelector(".editor-tab")).toBeNull();
    expect(screen.getByTestId("monaco-editor")).toBeTruthy();
  });

  it("keeps oversized files as lightweight notices instead of rendering Monaco", () => {
    useAppStore.setState({
      editorTabs: [{
        id: "editor-fixture-5",
        path: "data/images.md",
        content: "",
        original: "",
        loading: false,
        error: null,
        largeFile: true,
        loadWarning: "This file is 3.0 MB, which is above the 2.0 MB editor limit.",
        sizeBytes: 3 * 1024 * 1024,
      }],
      activeTabPath: "data/images.md",
    });

    render(<EditorPanel />);

    expect(screen.getByText("文件未加载到编辑器")).toBeTruthy();
    expect(screen.getByText(/above the 2\.0 MB editor limit/i)).toBeTruthy();
    expect(screen.queryByTestId("monaco-editor")).toBeNull();
    expect(screen.queryByText("预览")).toBeNull();
  });

  it("shows file load errors inside the active tab instead of rendering an empty editor", () => {
    useAppStore.setState({
      editorTabs: [{
        id: "editor-fixture-6",
        path: "missing.txt",
        content: "",
        original: "",
        loading: false,
        error: "Could not read missing.txt",
        largeFile: false,
      }],
      activeTabPath: "missing.txt",
    });

    render(<EditorPanel />);

    expect(screen.getByText("无法加载文件")).toBeTruthy();
    expect(screen.getByText("Could not read missing.txt")).toBeTruthy();
    expect(screen.queryByTestId("monaco-editor")).toBeNull();
  });

  it("reads desktop workspace files with an absolute path even when tabs store relative paths", async () => {
    vi.mocked(isDesktop).mockReturnValue(true);
    vi.mocked(fsReadFileInfo).mockResolvedValue({
      content: "from workspace root",
      contentHash: "hash-1",
      sizeBytes: 19,
    });
    useAppStore.setState({
      editorOpenRequests: [{ id: "open-1", path: "src/app.ts" }],
      activeEditorOpenRequestId: "open-1",
      activeEditorPath: "src/app.ts",
    });

    render(<EditorPanel />);

    const editor = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;
    expect(editor.value).toBe("from workspace root");
    expect(fsReadFileInfo).toHaveBeenCalledWith("C:/projects/demo/src/app.ts");
  });

  it("resolves a unique basename link before opening it from the transcript", async () => {
    vi.mocked(isDesktop).mockReturnValue(true);
    vi.mocked(fsSearchFiles).mockResolvedValue([
      { name: "extract_texture_features.py", path: "TF_FGC/scripts/extract_texture_features.py", kind: "file" },
    ]);
    vi.mocked(fsReadFileInfo).mockResolvedValue({
      content: "print('resolved')",
      contentHash: "hash-resolved",
      sizeBytes: 17,
    });
    useAppStore.setState({
      editorOpenRequests: [{ id: "open-basename", path: "extract_texture_features.py" }],
      activeEditorOpenRequestId: "open-basename",
      activeEditorPath: "extract_texture_features.py",
    });

    render(<EditorPanel />);

    const editor = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;
    expect(editor.value).toBe("print('resolved')");
    expect(fsSearchFiles).toHaveBeenCalledWith(
      "C:\\projects\\demo",
      "extract_texture_features.py",
      50,
      "file",
    );
    expect(fsReadFileInfo).toHaveBeenCalledWith(
      "C:/projects/demo/TF_FGC/scripts/extract_texture_features.py",
    );
    expect(searchWorkspaceFiles).not.toHaveBeenCalled();
  });

  it("opens MiniCode tool-result files as read-only editor tabs", async () => {
    vi.mocked(isDesktop).mockReturnValue(true);
    vi.mocked(fsReadFileInfo).mockResolvedValue({
      content: "persisted web result",
      contentHash: "hash-tool-result",
      sizeBytes: 20,
      readOnly: true,
    });
    const path = "C:/Users/ago/AppData/Roaming/minicode-desktop/data/tool-results/mc_web_fetch_example.txt";
    useAppStore.setState({
      editorOpenRequests: [{ id: "open-tool-result", path }],
      activeEditorOpenRequestId: "open-tool-result",
      activeEditorPath: path,
    });

    render(<EditorPanel />);

    const editor = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;
    expect(editor.value).toBe("persisted web result");
    expect(editor.readOnly).toBe(true);
    expect(screen.getByText("只读")).toBeTruthy();

    fireEvent.change(editor, { target: { value: "attempted edit" } });
    expect(useAppStore.getState().editorTabs.find((tab) => tab.path === path)?.content).toBe("persisted web result");
  });

  it("skips image-heavy Markdown previews instead of mounting every image", () => {
    const imageHeavyMarkdown = Array.from({ length: 90 }, (_, index) => `![image ${index}](./img-${index}.png)`).join("\n");
    useAppStore.setState({
      editorTabs: [{
        id: "editor-fixture-7",
        path: "data/images.md",
        content: imageHeavyMarkdown,
        original: imageHeavyMarkdown,
        loading: false,
        error: null,
        largeFile: false,
      }],
      activeTabPath: "data/images.md",
    });

    render(<EditorPanel />);

    fireEvent.click(screen.getByRole("tab", { name: "预览" }));

    expect(screen.getByText("已跳过 Markdown 预览")).toBeTruthy();
    expect(screen.queryAllByRole("img")).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "编辑 Markdown" }));

    expect(screen.getByTestId("monaco-editor")).toBeTruthy();
  });

  it("opens PDF files with the shared PDF.js preview in the editor pane", async () => {
    useAppStore.setState({
      editorTabs: [{
        id: "editor-fixture-8",
        path: "docs/report.pdf",
        content: "",
        original: "",
        loading: false,
        error: null,
        largeFile: false,
      }],
      activeTabPath: "docs/report.pdf",
    });

    render(<EditorPanel />);

    const preview = await screen.findByTestId("pdf-preview");
    expect(preview.getAttribute("data-url")).toContain("docs%2Freport.pdf");
    expect(preview.getAttribute("aria-label")).toBe("PDF 预览 report.pdf");
    expect(screen.queryByTestId("monaco-editor")).toBeNull();
  });

  it("renders SVG image files directly in the editor pane", () => {
    useAppStore.setState({
      editorTabs: [{
        id: "editor-fixture-9",
        path: "assets/logo.svg",
        content: "",
        original: "",
        loading: false,
        error: null,
        largeFile: false,
      }],
      activeTabPath: "assets/logo.svg",
    });

    render(<EditorPanel />);

    const image = screen.getByRole("img", { name: "logo.svg" });
    expect(image.getAttribute("src")).toContain("assets%2Flogo.svg");
    expect(screen.queryByTestId("monaco-editor")).toBeNull();
  });

  it("normalizes absolute PDF and SVG tab paths through the active workspace", async () => {
    useAppStore.setState({
      editorTabs: [{
        id: "editor-fixture-10",
        path: "C:\\projects\\demo\\assets\\logo.svg",
        content: "",
        original: "",
        loading: false,
        error: null,
        largeFile: false,
      }],
      activeTabPath: "C:\\projects\\demo\\assets\\logo.svg",
    });

    const { rerender } = render(<EditorPanel />);
    const image = screen.getByRole("img", { name: "logo.svg" });
    const imageUrl = new URL(image.getAttribute("src")!);
    expect(imageUrl.searchParams.get("path")).toBe("assets/logo.svg");
    expect(imageUrl.searchParams.get("workspace_root")).toBe("C:\\projects\\demo");

    useAppStore.setState({
      editorTabs: [{
        id: "editor-fixture-11",
        path: "C:\\projects\\demo\\docs\\report.pdf",
        content: "",
        original: "",
        loading: false,
        error: null,
        largeFile: false,
      }],
      activeTabPath: "C:\\projects\\demo\\docs\\report.pdf",
    });
    rerender(<EditorPanel />);

    const frame = await screen.findByTestId("pdf-preview");
    const frameUrl = new URL(frame.getAttribute("data-url")!);
    expect(frameUrl.searchParams.get("path")).toBe("docs/report.pdf");
    expect(frameUrl.searchParams.get("workspace_root")).toBe("C:\\projects\\demo");
  });

  it("opens the active file diff from the editor status bar", () => {
    const patch = [
      "diff --git a/src/app.ts b/src/app.ts",
      "@@ -1 +1,2 @@",
      "-old",
      "+new",
      "+line",
    ].join("\n");
    useAppStore.setState({
      editorTabs: [{
        id: "editor-fixture-12",
        path: "src/app.ts",
        content: "new\nline",
        original: "new\nline",
        loading: false,
        error: null,
        largeFile: false,
      }],
      activeTabPath: "src/app.ts",
      gitChanges: {
        workingTree: [{ path: "src/app.ts", patch, additions: 2, deletions: 1 }],
        staged: [],
        untracked: [],
        loading: false,
        live: null,
      },
    });

    render(<EditorPanel />);

    fireEvent.click(screen.getByRole("button", { name: /Diff/i }));

    const state = useAppStore.getState();
    expect(state.rightStackTab).toBe("diff");
    expect(state.diffReview).toMatchObject({
      status: "viewing",
      mode: "view",
      selectedPath: "src/app.ts",
    });
    expect(state.diffReview?.diff).toContain("+line");
  });

  it("keeps the tab dirty when the user types while a save request is in flight", async () => {
    let resolveSave: ((value: {
      ok: true;
      file: { content: string; content_hash: string; size_bytes: number };
    }) => void) | undefined;
    vi.mocked(compareWriteWorkspaceFile).mockImplementation(() => new Promise((resolve) => {
      resolveSave = resolve;
    }));
    useAppStore.setState({
      editorTabs: [{
        id: "editor-fixture-13",
        path: "src/app.ts",
        content: "first edit",
        original: "disk baseline",
        contentHash: "hash-before",
        sizeBytes: 13,
        loading: false,
        error: null,
        largeFile: false,
      }],
      activeTabPath: "src/app.ts",
    });

    render(<EditorPanel />);
    const editor = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;

    act(() => {
      window.dispatchEvent(new Event("editor:save"));
    });
    await waitFor(() => {
      expect(compareWriteWorkspaceFile).toHaveBeenCalledWith(
        "src/app.ts",
        "hash-before",
        "first edit",
        "C:\\projects\\demo",
      );
    });

    fireEvent.change(editor, { target: { value: "second edit while saving" } });
    await act(async () => {
      resolveSave?.({
        ok: true,
        file: { content: "first edit", content_hash: "hash-first-edit", size_bytes: 10 },
      });
      await Promise.resolve();
    });

    const tab = useAppStore.getState().editorTabs.find((item) => item.path === "src/app.ts");
    expect(tab?.content).toBe("second edit while saving");
    expect(tab?.original).toBe("first edit");
    expect(tab?.contentHash).toBe("hash-first-edit");
    expect(tab?.sizeBytes).toBe(10);
    expect(screen.getByText("10 B")).toBeTruthy();
    expect(tab?.content).not.toBe(tab?.original);
  });

  it("loads persisted tabs when workspace restoration finishes after the panel mounts", async () => {
    const workspace = "/tmp/restored-editor";
    persistEditorTabs([{ id: "editor-fixture-14", path: "saved.txt", content: "", original: "", loading: true }], workspace);
    useAppStore.setState({ workingDirectory: "" });
    vi.mocked(readWorkspaceFile).mockResolvedValueOnce({
      path: "saved.txt", content: "中文恢复", content_hash: "restored-hash", size_bytes: 12,
    });
    render(<EditorPanel />);

    act(() => useAppStore.getState().setWorkingDirectory(workspace));

    const editor = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;
    expect(editor.value).toBe("中文恢复");
    expect(readWorkspaceFile).toHaveBeenCalledTimes(1);
    expect(readWorkspaceFile).toHaveBeenCalledWith("saved.txt", workspace);
    expect(screen.queryByText("正在加载文件...")).toBeNull();
  });

  it("does not apply an old workspace read to a restored tab with the same path", async () => {
    let resolveOld: ((value: { path: string; content: string; size_bytes: number }) => void) | undefined;
    vi.mocked(readWorkspaceFile)
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce({ path: "same.txt", content: "new workspace", size_bytes: 13 });
    useAppStore.setState({
      editorTabs: [{ id: "editor-fixture-15", path: "same.txt", content: "", original: "", loading: true }],
      activeTabPath: "same.txt",
    });
    persistEditorTabs([{ id: "editor-fixture-16", path: "same.txt", content: "", original: "", loading: true }], "/tmp/other-editor");
    render(<EditorPanel />);
    await waitFor(() => expect(readWorkspaceFile).toHaveBeenCalledTimes(1));

    act(() => useAppStore.getState().setWorkingDirectory("/tmp/other-editor"));
    const editor = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;
    expect(editor.value).toBe("new workspace");
    await act(async () => resolveOld?.({ path: "same.txt", content: "old workspace", size_bytes: 99 }));

    expect(editor.value).toBe("new workspace");
    expect(useAppStore.getState().editorTabs[0]?.sizeBytes).toBe(13);
  });

  it.each(["bytes", "characters", "lines"] as const)("applies the %s limit on reload and clears it when the file shrinks", async (limit) => {
    vi.mocked(isDesktop).mockReturnValue(true);
    const content = limit === "lines" ? "line\n".repeat(20_000)
      : limit === "characters" ? "text".repeat(250_001) : "large desktop snapshot";
    const sizeBytes = limit === "bytes" ? 3 * 1024 * 1024 : content.length;
    vi.mocked(fsReadFileInfo)
      .mockResolvedValueOnce({ content, contentHash: "large-hash", sizeBytes })
      .mockResolvedValueOnce({ content: "small", contentHash: "small-hash", sizeBytes: 5 });
    useAppStore.setState({
      editorTabs: [{ id: "editor-fixture-17", path: "growing.txt", content: "old", original: "old", loading: false, externalChanged: true, sizeBytes: 3 }],
      activeTabPath: "growing.txt",
    });
    render(<EditorPanel />);

    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    await screen.findByText("文件未加载到编辑器");
    expect(screen.queryByTestId("monaco-editor")).toBeNull();
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({
      content: "", original: "", largeFile: true, sizeBytes, contentHash: "large-hash",
    });

    act(() => useAppStore.getState().markTabExternalChanged("growing.txt"));
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    const editor = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;
    expect(editor.value).toBe("small");
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({
      largeFile: false, loadWarning: null, sizeBytes: 5, contentHash: "small-hash", externalChanged: false,
    });
  });

  it("updates read-only metadata on reload instead of retaining the previous snapshot flags", async () => {
    vi.mocked(isDesktop).mockReturnValue(true);
    vi.mocked(fsReadFileInfo)
      .mockResolvedValueOnce({ content: "generated", contentHash: "generated-hash", sizeBytes: 9, readOnly: true })
      .mockResolvedValueOnce({ content: "editable", contentHash: "editable-hash", sizeBytes: 8, readOnly: false });
    useAppStore.setState({
      editorTabs: [{ id: "editor-fixture-18", path: "result.txt", content: "old", original: "old", loading: false, externalChanged: true }],
      activeTabPath: "result.txt",
    });
    render(<EditorPanel />);

    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    await screen.findByText("只读");
    const editor = screen.getByTestId("monaco-editor") as HTMLTextAreaElement;
    expect(editor.readOnly).toBe(true);
    fireEvent.change(editor, { target: { value: "not allowed" } });
    expect(useAppStore.getState().editorTabs[0]?.content).toBe("generated");

    act(() => useAppStore.getState().markTabExternalChanged("result.txt"));
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    await waitFor(() => expect(editor.value).toBe("editable"));
    expect(editor.readOnly).toBe(false);
    expect(screen.queryByText("只读")).toBeNull();
    expect(screen.getByText("8 B")).toBeTruthy();
  });

  it("shows a lightweight limit notice when an automatic reload receives HTTP 413", async () => {
    vi.mocked(readWorkspaceFile).mockRejectedValueOnce(new Error("File is too large. Max supported size is 2097152 bytes."));
    useAppStore.setState({
      editorTabs: [{ id: "editor-fixture-19", path: "growing.txt", content: "old", original: "old", loading: false, sizeBytes: 3 }],
      activeTabPath: "growing.txt",
    });
    render(<EditorPanel />);

    act(() => useAppStore.setState({ fileChanges: [{ path: "growing.txt", event: "modify", sequence: 1, timestamp: 1 }] }));

    await screen.findByText("文件未加载到编辑器");
    expect(screen.queryByTestId("monaco-editor")).toBeNull();
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "", original: "", largeFile: true });
    expect(useAppStore.getState().editorTabs[0]?.sizeBytes).toBeUndefined();
  });

  it("keeps edits typed while a reload is pending, including their previous metadata", async () => {
    let resolveRead: ((value: { path: string; content: string; size_bytes: number }) => void) | undefined;
    vi.mocked(readWorkspaceFile).mockImplementationOnce(() => new Promise((resolve) => { resolveRead = resolve; }));
    useAppStore.setState({
      editorTabs: [{ id: "editor-fixture-20", path: "editing.txt", content: "draft", original: "old", loading: false, externalChanged: true, sizeBytes: 3 }],
      activeTabPath: "editing.txt",
    });
    render(<EditorPanel />);
    const editor = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;

    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    fireEvent.change(editor, { target: { value: "new unsaved edit" } });
    await act(async () => resolveRead?.({ path: "editing.txt", content: "external", size_bytes: 8 }));

    expect(editor.value).toBe("new unsaved edit");
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ original: "old", sizeBytes: 3, externalChanged: true });
  });

  it("settles a save in its cached workspace while another workspace saves the same relative path", async () => {
    let resolveFirst!: (value: Awaited<ReturnType<typeof compareWriteWorkspaceFile>>) => void;
    vi.mocked(compareWriteWorkspaceFile)
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
      .mockResolvedValueOnce({ ok: true, file: { path: "same.txt", content: "B edit", content_hash: "B saved" } });
    useAppStore.setState({
      editorTabs: [{ id: "editor-fixture-21", path: "same.txt", content: "A edit", original: "A disk", contentHash: "A old", loading: false }],
      activeTabPath: "same.txt",
    });
    render(<EditorPanel />);
    await screen.findByTestId("monaco-editor");
    act(() => {
      window.dispatchEvent(new Event("editor:save"));
      window.dispatchEvent(new Event("editor:save"));
    });
    expect(compareWriteWorkspaceFile).toHaveBeenCalledTimes(1);

    act(() => {
      useAppStore.getState().setWorkingDirectory("C:/other");
      useAppStore.setState({
        editorTabs: [{ id: "editor-fixture-22", path: "same.txt", content: "B edit", original: "B disk", contentHash: "B old", loading: false }],
        activeTabPath: "same.txt",
      });
    });
    await act(async () => { window.dispatchEvent(new Event("editor:save")); });
    expect(compareWriteWorkspaceFile).toHaveBeenLastCalledWith("same.txt", "B old", "B edit", "C:/other");
    await act(async () => resolveFirst({ ok: true, file: { path: "same.txt", content: "A edit", content_hash: "A saved" } }));
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "B edit", original: "B edit", contentHash: "B saved" });

    act(() => useAppStore.getState().setWorkingDirectory("C:/projects/demo"));
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "A edit", original: "A edit", contentHash: "A saved" });
  });

  it("retains a late save conflict in the originating workspace", async () => {
    let resolveSave!: (value: Awaited<ReturnType<typeof compareWriteWorkspaceFile>>) => void;
    vi.mocked(compareWriteWorkspaceFile).mockImplementationOnce(() => new Promise((resolve) => { resolveSave = resolve; }));
    useAppStore.setState({
      editorTabs: [{ id: "editor-fixture-23", path: "same.txt", content: "draft", original: "disk", contentHash: "old", loading: false }],
      activeTabPath: "same.txt",
    });
    render(<EditorPanel />);
    act(() => { window.dispatchEvent(new Event("editor:save")); });
    act(() => useAppStore.getState().setWorkingDirectory("C:/other"));
    await act(async () => resolveSave({ ok: false, conflict: true, message: "Changed on disk" }));
    act(() => useAppStore.getState().setWorkingDirectory("C:/projects/demo"));
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "draft", original: "disk", contentHash: "old", externalChanged: true });
    expect(screen.getByRole("button", { name: "重新加载" })).toBeTruthy();
  });

  it("loads the renamed path and discards the old pending read", async () => {
    let resolveOld!: (value: Awaited<ReturnType<typeof readWorkspaceFile>>) => void;
    vi.mocked(readWorkspaceFile)
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce({ path: "renamed/main.ts", content: "current", content_hash: "new hash" });
    useAppStore.setState({
      editorTabs: [{ id: "editor-fixture-24", path: "src/main.ts", content: "", original: "", loading: true }],
      activeTabPath: "src/main.ts",
    });
    render(<EditorPanel />);
    act(() => useAppStore.getState().renameEditorPath("src", "renamed", "C:/projects/demo"));
    await waitFor(() => expect(useAppStore.getState().editorTabs[0]?.content).toBe("current"));
    await act(async () => resolveOld({ path: "src/main.ts", content: "stale", content_hash: "old hash" }));
    expect(readWorkspaceFile).toHaveBeenCalledTimes(2);
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ path: "renamed/main.ts", content: "current", loading: false });
  });

  it("uses the currently selected file when the mounted Monaco action opens side chat", async () => {
    useAppStore.setState({
      editorTabs: ["first.ts", "second.ts"].map((path) => ({ id: path, path, content: "code", original: "code", loading: false })),
      activeTabPath: "first.ts",
    });
    render(<EditorPanel />);
    await screen.findByTestId("monaco-editor");
    fireEvent.click(screen.getByRole("button", { name: "second.ts", exact: true }));
    act(() => editorMocks.actions[0].run({
      getSelection: () => ({}),
      getModel: () => ({ getValueInRange: () => "selected code" }),
    }));
    expect(useAppStore.getState().sideChatPendingContext).toMatchObject({ text: "selected code", source: "second.ts" });
  });

  it("uses distinct Monaco models for the same relative path in different workspaces", async () => {
    const tab = { id: "editor-fixture-26", path: "same.ts", content: "code", original: "code", loading: false };
    useAppStore.setState({ editorTabs: [tab], activeTabPath: tab.path });
    render(<EditorPanel />);
    const first = await screen.findByTestId("monaco-editor");
    const firstPath = first.getAttribute("data-model-path");
    act(() => {
      useAppStore.getState().setWorkingDirectory("C:/other");
      useAppStore.setState({ editorTabs: [{ ...tab, id: "other-workspace-buffer", content: "other code" }], activeTabPath: tab.path });
    });
    const second = await screen.findByTestId("monaco-editor");
    expect(second).not.toBe(first);
    expect(second.getAttribute("data-model-path")).not.toBe(firstPath);
  });

  it.each(["workspace", "content"] as const)("does not discard a buffer changed during the close confirmation: %s", async (change) => {
    let resolveConfirm!: (value: boolean) => void;
    editorMocks.showConfirm.mockImplementationOnce(() => new Promise((resolve) => { resolveConfirm = resolve; }));
    useAppStore.setState({
      editorTabs: [{ id: "editor-fixture-27", path: "same.ts", content: "draft", original: "disk", loading: false }],
      activeTabPath: "same.ts",
    });
    render(<EditorPanel />);
    fireEvent.click(screen.getByRole("button", { name: "关闭 same.ts" }));
    await waitFor(() => expect(editorMocks.showConfirm).toHaveBeenCalled());
    act(() => {
      if (change === "workspace") {
        useAppStore.getState().setWorkingDirectory("C:/other");
        useAppStore.setState({ editorTabs: [{ id: "editor-fixture-28", path: "same.ts", content: "new draft", original: "disk", loading: false }], activeTabPath: "same.ts" });
      } else useAppStore.getState().updateTabContent("same.ts", "new draft");
    });
    await act(async () => resolveConfirm(true));
    expect(useAppStore.getState().editorTabs).toHaveLength(1);
    expect(useAppStore.getState().editorTabs[0].content).toBe("new draft");
  });

  it("does not report a dirty renamed buffer as changed when disk still matches its baseline", async () => {
    vi.mocked(readWorkspaceFile).mockResolvedValueOnce({ path: "renamed.txt", content: "disk", content_hash: "old hash" });
    useAppStore.setState({
      editorTabs: [{ id: "editor-fixture-29", path: "renamed.txt", content: "draft", original: "disk", contentHash: "old hash", loading: false, externalChanged: true }],
      activeTabPath: "renamed.txt",
      fileChanges: [{ path: "renamed.txt", event: "create", sequence: 1, timestamp: 1 }],
    });
    render(<EditorPanel />);
    await waitFor(() => expect(useAppStore.getState().editorTabs[0].externalChanged).toBe(false));
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "draft", original: "disk", contentHash: "old hash" });
    expect(screen.queryByRole("button", { name: "重新加载" })).toBeNull();
  });

  it("keeps newly typed edits and reports a real disk change while a watcher read is pending", async () => {
    let resolveRead!: (value: Awaited<ReturnType<typeof readWorkspaceFile>>) => void;
    vi.mocked(readWorkspaceFile).mockImplementationOnce(() => new Promise((resolve) => { resolveRead = resolve; }));
    useAppStore.setState({
      editorTabs: [{ id: "editor-fixture-30", path: "file.txt", content: "disk", original: "disk", contentHash: "old hash", loading: false }],
      activeTabPath: "file.txt",
    });
    render(<EditorPanel />);
    const editor = await screen.findByTestId("monaco-editor");
    act(() => useAppStore.getState().addFileChange({ path: "file.txt", event: "modify", timestamp: 1 }));
    fireEvent.change(editor, { target: { value: "typed while reading" } });
    await act(async () => resolveRead({ path: "file.txt", content: "external change", content_hash: "external hash" }));
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "typed while reading", original: "disk", contentHash: "old hash", externalChanged: true });
  });

  it("settles the save before reconciling a file watcher event that arrived during the request", async () => {
    let resolveSave!: (value: Awaited<ReturnType<typeof compareWriteWorkspaceFile>>) => void;
    vi.mocked(compareWriteWorkspaceFile).mockImplementationOnce(() => new Promise((resolve) => { resolveSave = resolve; }));
    vi.mocked(readWorkspaceFile).mockResolvedValueOnce({ path: "file.txt", content: "external after save", content_hash: "external hash" });
    useAppStore.setState({
      editorTabs: [{ id: "editor-fixture-31", path: "file.txt", content: "first edit", original: "disk", contentHash: "old hash", loading: false }],
      activeTabPath: "file.txt",
    });
    render(<EditorPanel />);
    act(() => { window.dispatchEvent(new Event("editor:save")); });
    act(() => {
      useAppStore.getState().updateTabContent("file.txt", "second edit");
      useAppStore.getState().addFileChange({ path: "file.txt", event: "modify", timestamp: 1 });
    });
    expect(readWorkspaceFile).not.toHaveBeenCalled();
    await act(async () => resolveSave({ ok: true, file: { path: "file.txt", content: "first edit", content_hash: "saved hash" } }));
    await waitFor(() => expect(readWorkspaceFile).toHaveBeenCalledTimes(1));
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "second edit", original: "first edit", contentHash: "saved hash", externalChanged: true });
  });

  it.each([false, true])("refreshes disk state after returning to a workspace with a dirty buffer=%s", async (dirty) => {
    vi.mocked(readWorkspaceFile).mockResolvedValueOnce({ path: "file.txt", content: "changed while away", content_hash: "new hash" });
    useAppStore.setState({
      editorTabs: [{ id: "editor-fixture-32", path: "file.txt", content: dirty ? "draft" : "disk", original: "disk", contentHash: "old hash", loading: false }],
      activeTabPath: "file.txt",
    });
    render(<EditorPanel />);
    act(() => useAppStore.getState().setWorkingDirectory("C:/other"));
    act(() => useAppStore.getState().setWorkingDirectory("C:/projects/demo"));
    await waitFor(() => expect(readWorkspaceFile).toHaveBeenCalled());
    expect(useAppStore.getState().editorTabs[0]).toMatchObject(dirty
      ? { content: "draft", original: "disk", contentHash: "old hash", externalChanged: true }
      : { content: "changed while away", original: "changed while away", contentHash: "new hash", externalChanged: false });
  });

  it("settles a pending save on the same buffer after a rename and preserves newer edits", async () => {
    let resolveSave!: (value: Awaited<ReturnType<typeof compareWriteWorkspaceFile>>) => void;
    vi.mocked(compareWriteWorkspaceFile)
      .mockImplementationOnce(() => new Promise((resolve) => { resolveSave = resolve; }))
      .mockResolvedValueOnce({ ok: true, file: { path: "renamed.txt", content: "second edit", content_hash: "second hash" } });
    vi.mocked(readWorkspaceFile).mockResolvedValue({ path: "renamed.txt", content: "first edit", content_hash: "saved hash" });
    useAppStore.setState({
      editorTabs: [{ id: "buffer-being-renamed", path: "before.txt", content: "first edit", original: "disk", contentHash: "old hash", loading: false }],
      activeTabPath: "before.txt",
    });
    render(<EditorPanel />);
    const modelPath = (await screen.findByTestId("monaco-editor")).getAttribute("data-model-path");
    act(() => { window.dispatchEvent(new Event("editor:save")); });
    act(() => {
      useAppStore.getState().updateTabContent("before.txt", "second edit");
      useAppStore.getState().renameEditorPath("before.txt", "renamed.txt", "C:/projects/demo");
      useAppStore.getState().addFileChange({ path: "renamed.txt", event: "create", timestamp: 1 });
    });
    expect(readWorkspaceFile).not.toHaveBeenCalled();
    await act(async () => resolveSave({ ok: true, file: { path: "before.txt", content: "first edit", content_hash: "saved hash" } }));
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ id: "buffer-being-renamed", path: "renamed.txt", content: "second edit", original: "first edit", contentHash: "saved hash", externalChanged: false });
    expect(screen.getByTestId("monaco-editor").getAttribute("data-model-path")).toBe(modelPath);
    await act(async () => { window.dispatchEvent(new Event("editor:save")); });
    expect(compareWriteWorkspaceFile).toHaveBeenLastCalledWith("renamed.txt", "saved hash", "second edit", "C:\\projects\\demo");
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "second edit", original: "second edit", contentHash: "second hash" });
  });

  it("does not apply an old save to a closed and reopened buffer at the same path", async () => {
    let resolveSave!: (value: Awaited<ReturnType<typeof compareWriteWorkspaceFile>>) => void;
    vi.mocked(compareWriteWorkspaceFile).mockImplementationOnce(() => new Promise((resolve) => { resolveSave = resolve; }));
    act(() => {
      useAppStore.getState().openEditorTab("file.txt");
      useAppStore.getState().markTabLoaded("file.txt", "disk", null, "old hash");
      useAppStore.getState().updateTabContent("file.txt", "first edit");
    });
    render(<EditorPanel />);
    act(() => { window.dispatchEvent(new Event("editor:save")); });
    act(() => {
      useAppStore.getState().closeEditorTab("file.txt");
      useAppStore.getState().openEditorTab("file.txt");
      useAppStore.getState().markTabLoaded("file.txt", "disk", null, "old hash");
      useAppStore.getState().updateTabContent("file.txt", "reopened draft");
    });
    await act(async () => resolveSave({ ok: true, file: { path: "file.txt", content: "first edit", content_hash: "saved hash" } }));
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "reopened draft", original: "disk", contentHash: "old hash" });
  });

  it.each([false, true])("keeps the last requested file selected when an earlier basename search resolves late (desktop=%s)", async (desktopMode) => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    vi.mocked(isDesktop).mockReturnValue(desktopMode);
    const search = desktopMode ? vi.mocked(fsSearchFiles) : vi.mocked(searchWorkspaceFiles);
    search.mockImplementationOnce(async () => {
      await gate;
      return [{ name: "slow.txt", path: "slow.txt", score: 1, kind: "file" }];
    });
    vi.mocked(readWorkspaceFile).mockImplementation(async (path) => ({ path, content: path, content_hash: path }));
    vi.mocked(fsReadFileInfo).mockImplementation(async (path) => ({ content: path, contentHash: path }));
    render(<EditorPanel />);
    act(() => useAppStore.getState().openEditorFile("slow.txt"));
    await waitFor(() => expect(search).toHaveBeenCalledTimes(1));
    act(() => useAppStore.getState().openEditorFile("src/fast.txt"));
    await waitFor(() => expect(useAppStore.getState().activeTabPath).toBe("src/fast.txt"));
    await act(async () => release());
    await waitFor(() => expect(useAppStore.getState().editorTabs.find((tab) => tab.path === "slow.txt")?.loading).toBe(false));
    expect(useAppStore.getState().activeTabPath).toBe("src/fast.txt");
    expect(useAppStore.getState().activeEditorPath).toBe("src/fast.txt");
    expect(useAppStore.getState().editorOpenRequests).toEqual([]);
  });

  it.each(["select", "close all", "workspace round trip"])("does not activate a pending open after %s", async (action) => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    vi.mocked(searchWorkspaceFiles).mockImplementationOnce(async () => {
      await gate;
      return [{ name: "slow.txt", path: "slow.txt", score: 1 }];
    });
    vi.mocked(readWorkspaceFile).mockImplementation(async (path) => ({ path, content: "disk", content_hash: "hash" }));
    const tab = { id: "selected-buffer", path: "selected.txt", content: "disk", original: "disk", contentHash: "hash", loading: false };
    useAppStore.setState({ editorTabs: [tab], activeTabPath: tab.path, activeEditorPath: tab.path });
    render(<EditorPanel />);
    act(() => useAppStore.getState().openEditorFile("slow.txt"));
    await waitFor(() => expect(searchWorkspaceFiles).toHaveBeenCalledTimes(1));
    act(() => {
      const state = useAppStore.getState();
      if (action === "select") state.setActiveTab("selected.txt");
      if (action === "close all") state.closeAllEditorTabs();
      if (action === "workspace round trip") {
        state.setWorkingDirectory("C:/other");
        state.setWorkingDirectory("C:/projects/demo");
      }
    });
    await act(async () => release());
    expect(useAppStore.getState().activeTabPath).toBe(action === "close all" ? null : "selected.txt");
    if (action !== "select") expect(useAppStore.getState().editorTabs.some((tab) => tab.path === "slow.txt")).toBe(false);
  });

  it("opens the new path when a rename changes an unresolved request", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    vi.mocked(searchWorkspaceFiles).mockImplementationOnce(async () => {
      await gate;
      return [{ name: "before.txt", path: "before.txt", score: 1 }];
    });
    vi.mocked(readWorkspaceFile).mockResolvedValue({ path: "src/after.txt", content: "renamed", content_hash: "hash" });
    render(<EditorPanel />);
    act(() => useAppStore.getState().openEditorFile("before.txt"));
    await waitFor(() => expect(searchWorkspaceFiles).toHaveBeenCalledTimes(1));
    act(() => useAppStore.getState().renameEditorPath("before.txt", "src/after.txt", "C:/projects/demo"));
    await waitFor(() => expect(useAppStore.getState().activeTabPath).toBe("src/after.txt"));
    await act(async () => release());
    expect(useAppStore.getState().editorTabs.map((tab) => tab.path)).toEqual(["src/after.txt"]);
    expect(readWorkspaceFile).not.toHaveBeenCalledWith("before.txt", expect.anything());
  });

  it("does not apply an old watcher read to a closed and reopened buffer with the same baseline", async () => {
    let release!: (snapshot: { path: string; content: string; content_hash: string }) => void;
    vi.mocked(readWorkspaceFile)
      .mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }))
      .mockResolvedValueOnce({ path: "file.txt", content: "disk", content_hash: "hash" });
    const tab = { id: "old-buffer", path: "file.txt", content: "disk", original: "disk", contentHash: "hash", loading: false };
    useAppStore.setState({ editorTabs: [tab], activeTabPath: tab.path, activeEditorPath: tab.path });
    render(<EditorPanel />);
    act(() => useAppStore.getState().addFileChange({ path: "file.txt", event: "change", timestamp: 1 }));
    await waitFor(() => expect(readWorkspaceFile).toHaveBeenCalledTimes(1));
    act(() => {
      useAppStore.getState().closeEditorTab("file.txt");
      useAppStore.getState().openEditorTab("file.txt");
    });
    await waitFor(() => expect(useAppStore.getState().editorTabs[0].loading).toBe(false));
    await act(async () => release({ path: "file.txt", content: "stale watcher snapshot", content_hash: "stale" }));
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "disk", original: "disk", contentHash: "hash" });
  });

  it("decodes Markdown resource URLs exactly once and keeps fragments outside the disk path", () => {
    const content = [
      "![Space](./assets/space%20logo.svg)",
      "![Percent](./assets/literal%2520.svg)",
      "![File URI](file:///C:/projects/demo/docs/assets/logo.svg#icon)",
      "[Download](./assets/logo.svg#icon)",
      "[Remote](https://example.com/a//b?x=1#part)",
      "[Malformed file URI](file://%notvalid/logo.svg)",
    ].join("\n\n");
    useAppStore.setState({ editorTabs: [{ id: "markdown-resources", path: "docs/readme.md", content, original: content, loading: false }], activeTabPath: "docs/readme.md" });
    render(<EditorPanel />);
    fireEvent.click(screen.getByRole("tab", { name: "预览" }));
    for (const [name, path] of [["Space", "docs/assets/space logo.svg"], ["Percent", "docs/assets/literal%20.svg"], ["File URI", "docs/assets/logo.svg"]]) {
      const url = new URL(screen.getByRole("img", { name }).getAttribute("src")!);
      expect(url.searchParams.get("path")).toBe(path);
      expect(url.searchParams.get("workspace_root")).toBe("C:\\projects\\demo");
    }
    const download = new URL(screen.getByRole("link", { name: "Download" }).getAttribute("href")!);
    expect(download.searchParams.get("path")).toBe("docs/assets/logo.svg");
    expect(download.hash).toBe("#icon");
    expect(screen.getByRole("link", { name: "Remote" }).getAttribute("href")).toBe("https://example.com/a//b?x=1#part");
    expect(screen.getByText("Malformed file URI").getAttribute("href") ?? "").toBe("");
  });

  it.each(["C:/projects/demo", "/projects/demo", "//server/share/demo"])("resolves an absolute Markdown owner under %s", (root) => {
    const content = "![Logo](../assets/logo.svg)";
    useAppStore.setState({ workingDirectory: root, editorTabs: [{ id: "absolute-owner", path: `${root}/docs/readme.md`, content, original: content, loading: false }], activeTabPath: `${root}/docs/readme.md` });
    render(<EditorPanel />);
    fireEvent.click(screen.getByRole("tab", { name: "预览" }));
    const url = new URL(screen.getByRole("img", { name: "Logo" }).getAttribute("src")!);
    expect(url.searchParams.get("path")).toBe("assets/logo.svg");
    expect(url.searchParams.get("workspace_root")).toBe(root);
  });

  it.each(["image", "pdf", "markdown"])("refreshes %s resources only when their file changes", async (kind) => {
    const asset = kind === "pdf" ? "assets/report.pdf" : "assets/literal%20.svg";
    const path = kind === "markdown" ? "readme.md" : asset;
    const content = kind === "markdown" ? "![Logo](./assets/literal%2520.svg)" : "";
    useAppStore.setState({ editorTabs: [{ id: "media-buffer", path, content, original: content, loading: false }], activeTabPath: path });
    render(<EditorPanel />);
    if (kind === "markdown") fireEvent.click(screen.getByRole("tab", { name: "预览" }));
    if (kind === "pdf") await screen.findByTestId("pdf-preview");
    const resourceUrl = () => kind === "pdf"
      ? screen.getByTestId("pdf-preview").getAttribute("data-url")!
      : screen.getByRole("img").getAttribute("src")!;
    const before = resourceUrl();
    expect(new URL(before).searchParams.get("path")).toBe(asset);
    act(() => useAppStore.getState().addFileChange({ path: "unrelated.txt", event: "change", timestamp: 1 }));
    expect(resourceUrl()).toBe(before);
    act(() => useAppStore.getState().addFileChange({ path: `C:/projects/demo/${asset}`, event: "change", timestamp: 2 }));
    const after = resourceUrl();
    expect(after).not.toBe(before);
    expect(new URL(after).searchParams.get("path")).toBe(asset);
    expect(readWorkspaceFile).not.toHaveBeenCalled();
  });
});
