/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fsReadFileInfo, fsSearchFiles, isDesktop } from "../desktop/runtime";
import { compareWriteWorkspaceFile, readWorkspaceFile, searchWorkspaceFiles } from "../protocol/workspace";
import { pushToast } from "../overlays/ToastContainer";
import { useAppStore } from "../stores";
import { clearEditorWorkspaceBufferCacheForTests, editorStateForWorkspace, loadPersistedEditorTabs, persistEditorTabs } from "../stores/shared-helpers";
import { EditorPanel } from "./EditorPanel";
import type { CodeSelectionRange } from "../stores/types";

const editorMocks = vi.hoisted(() => ({
  actions: [] as Array<{ run: (editor: unknown) => void }>,
  showConfirm: vi.fn(),
  editOperations: [] as string[],
  trigger: vi.fn(),
}));

vi.mock("../overlays/DialogService", () => ({ showConfirm: editorMocks.showConfirm }));
vi.mock("./PdfAttachmentPreview", () => ({
  PdfAttachmentPreview: ({ url, name, onRetry }: { url: string; name: string; onRetry?: () => void }) => (
    <div data-testid="pdf-preview" data-url={url} aria-label={`PDF 预览 ${name}`}>{onRetry && <button onClick={onRetry}>重试 PDF 预览</button>}</div>
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
      const inputRef = ReactModule.useRef<HTMLTextAreaElement>(null);
      const propsRef = ReactModule.useRef({ value, path });
      propsRef.current = { value, path };
      const selections = ReactModule.useRef(new Map<string, { start: number; end: number }>());
      const listeners = ReactModule.useRef({ selection: () => {}, model: () => {}, content: () => {}, scroll: () => {} });
      const positionAt = (offset: number) => {
        const before = propsRef.current.value.slice(0, offset).split("\n");
        return { lineNumber: before.length, column: before[before.length - 1].length + 1 };
      };
      const offsetAt = (lineNumber: number, column: number) => propsRef.current.value.split("\n")
        .slice(0, lineNumber - 1).reduce((offset, line) => offset + line.length + 1, 0) + column - 1;
      ReactModule.useEffect(() => {
        let dispose: () => void;
        const mountedInput = inputRef.current!;
        onMount({
          trigger: editorMocks.trigger,
          addContentWidget: vi.fn(),
          layoutContentWidget: vi.fn(),
          removeContentWidget: vi.fn(),
          saveViewState: () => {
            const input = inputRef.current;
            if (!input) return null;
            const start = positionAt(input.selectionStart);
            const end = positionAt(input.selectionEnd);
            return { selection: { startLineNumber: start.lineNumber, startColumn: start.column, endLineNumber: end.lineNumber, endColumn: end.column }, scrollTop: input.scrollTop };
          },
          restoreViewState: (saved: { selection: CodeSelectionRange; scrollTop: number }) => {
            const range = saved.selection;
            inputRef.current!.setSelectionRange(offsetAt(range.startLineNumber, range.startColumn), offsetAt(range.endLineNumber, range.endColumn));
            inputRef.current!.scrollTop = saved.scrollTop;
            listeners.current.selection();
          },
          addAction: (action: { run: (editor: unknown) => void }) => editorMocks.actions.push(action),
          focus: vi.fn(),
          pushUndoStop: () => editorMocks.editOperations.push("stop"),
          executeEdits: (source: string, edits: Array<{ range: CodeSelectionRange; text: string }>) => {
            editorMocks.editOperations.push(source);
            const edit = edits[0];
            onChange?.(propsRef.current.value.slice(0, offsetAt(edit.range.startLineNumber, edit.range.startColumn)) + edit.text
              + propsRef.current.value.slice(offsetAt(edit.range.endLineNumber, edit.range.endColumn)));
          },
          getSelection: () => {
            const start = positionAt(mountedInput.selectionStart);
            const end = positionAt(mountedInput.selectionEnd);
            return { startLineNumber: start.lineNumber, startColumn: start.column, endLineNumber: end.lineNumber, endColumn: end.column };
          },
          setSelection: (range: CodeSelectionRange) => inputRef.current!.setSelectionRange(offsetAt(range.startLineNumber, range.startColumn), offsetAt(range.endLineNumber, range.endColumn)),
          getPosition: () => {
            const before = propsRef.current.value.slice(0, mountedInput.selectionEnd).split("\n");
            return { lineNumber: before.length, column: before[before.length - 1].length + 1 };
          },
          getModel: () => ({
            uri: { path: decodeURIComponent(new URL(propsRef.current.path).pathname) },
            getValueInRange: (range: CodeSelectionRange) => propsRef.current.value.slice(offsetAt(range.startLineNumber, range.startColumn), offsetAt(range.endLineNumber, range.endColumn)),
            getLineCount: () => propsRef.current.value.split("\n").length,
            getLineMaxColumn: (line: number) => propsRef.current.value.split("\n")[line - 1].length + 1,
            getEOL: () => "\n",
          }),
          onDidChangeCursorPosition: vi.fn(),
          onDidChangeCursorSelection: (listener: () => void) => { listeners.current.selection = listener; return { dispose: vi.fn() }; },
          onDidChangeModel: (listener: () => void) => { listeners.current.model = listener; },
          onDidChangeModelContent: (listener: () => void) => { listeners.current.content = listener; },
          onDidScrollChange: (listener: () => void) => { listeners.current.scroll = listener; },
          onDidDispose: (listener: () => void) => { dispose = listener; },
        });
        return () => dispose?.();
      }, []);
      ReactModule.useEffect(() => {
        const selection = selections.current.get(path) ?? { start: 0, end: 0 };
        inputRef.current!.setSelectionRange(selection.start, selection.end);
        listeners.current.model();
      }, [path]);
      ReactModule.useEffect(() => { listeners.current.content(); }, [value]);
      return ReactModule.createElement("textarea", {
        ref: inputRef,
        "data-testid": "monaco-editor",
        "data-model-path": path,
        value,
        readOnly: options?.readOnly,
        onChange: (event: React.ChangeEvent<HTMLTextAreaElement>) => onChange?.(event.currentTarget.value),
        onScroll: () => listeners.current.scroll(),
        onSelect: (event: React.SyntheticEvent<HTMLTextAreaElement>) => {
          selections.current.set(path, { start: event.currentTarget.selectionStart, end: event.currentTarget.selectionEnd });
          listeners.current.selection();
        },
      });
    },
  };
});

vi.mock("./LiveMarkdownEditor", async () => {
  const ReactMarkdown = (await import("react-markdown")).default;
  const remarkGfm = (await import("remark-gfm")).default;
  const Monaco = (await import("@monaco-editor/react")).default;
  return {
    LiveMarkdownEditor: (props: {
      documentId: string; value: string; readOnly: boolean;
      components: import("react-markdown").Components; urlTransform: (url: string) => string;
      onChange: (value: string) => void; onMount: (editor: unknown) => void;
    }) => <div data-testid="live-markdown-editor">
      <Monaco value={props.value} path={`minicode-editor://buffer/${props.documentId}`} onChange={props.onChange} onMount={props.onMount} options={{ readOnly: props.readOnly }} />
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={props.components} urlTransform={props.urlTransform}>{props.value}</ReactMarkdown>
    </div>,
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
vi.mock("monaco-editor/languages/definitions/scss/register.js", () => ({}));
vi.mock("monaco-editor/languages/definitions/less/register.js", () => ({}));
vi.mock("monaco-editor/languages/definitions/html/register.js", () => ({}));
vi.mock("monaco-editor/languages/definitions/markdown/register.js", () => ({}));
vi.mock("monaco-editor/languages/definitions/python/register.js", () => ({}));
vi.mock("monaco-editor/languages/definitions/yaml/register.js", () => ({}));
vi.mock("./monacoLanguageServices", async (importOriginal) => ({
  ...await importOriginal<typeof import("./monacoLanguageServices")>(),
  configureMiniCodeMonacoWorkers: vi.fn(),
  loadMiniCodeLanguageServices: vi.fn(async () => {}),
  registerMiniCodeEditorOpener: vi.fn(() => ({ dispose: vi.fn() })),
}));

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
    editorMocks.editOperations.length = 0;
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
      sideChatOpen: false,
      sideChatPendingContext: null,
      sideChats: {},
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("isolates chat insertion from neighboring editor input with native undo stops", async () => {
    vi.mocked(readWorkspaceFile).mockResolvedValue({ path: "main.ts", content: "before after", content_hash: "original" });
    useAppStore.getState().openEditorFile("main.ts", "main.ts", { exact: true });
    render(<EditorPanel />);
    const editor = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;
    editor.setSelectionRange(7, 7);
    const event = new CustomEvent("editor:insert-text", { detail: { text: "INSERT " } });
    act(() => window.dispatchEvent(event));
    expect(editor.value).toBe("before INSERT after");
    expect(editorMocks.editOperations).toEqual(["stop", "chat-code-insert", "stop"]);
    expect(event.detail.handled).toBe(true);
  });

  it("recovers unsaved text and native reading position after an editor and workspace-cache restart", async () => {
    const original = "first line\nsecond line\nthird line";
    const draft = "first draft\nsecond line\nthird line";
    vi.mocked(readWorkspaceFile).mockResolvedValue({ path: "main.ts", content: original, content_hash: "original" });
    useAppStore.getState().openEditorFile("main.ts", "main.ts", { exact: true });
    const first = render(<EditorPanel />);
    const input = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: draft } });
    input.setSelectionRange(14, 21);
    fireEvent.select(input);
    input.scrollTop = 140;
    fireEvent.scroll(input);
    first.unmount();
    clearEditorWorkspaceBufferCacheForTests();
    useAppStore.setState({ editorTabs: loadPersistedEditorTabs("C:/projects/demo") });
    render(<EditorPanel />);
    const restored = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;
    await waitFor(() => expect(restored.value).toBe(draft));
    await waitFor(() => expect([restored.selectionStart, restored.selectionEnd, restored.scrollTop]).toEqual([14, 21, 140]));
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ original, contentHash: "original", draftRestored: true });
    expect(screen.getByText("已恢复草稿")).toBeTruthy();
  });

  it("routes code toolbar search, definition and references through native editor commands", async () => {
    vi.mocked(readWorkspaceFile).mockResolvedValue({ path: "main.ts", content: 'const message = "MiniCode";', content_hash: "original" });
    useAppStore.getState().openEditorFile("main.ts", "main.ts", { exact: true });
    render(<EditorPanel />);
    await screen.findByTestId("monaco-editor");
    fireEvent.click(screen.getByRole("button", { name: "查找" }));
    fireEvent.click(screen.getByRole("button", { name: "更多编辑器操作" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /^转到定义/ }));
    fireEvent.click(screen.getByRole("button", { name: "更多编辑器操作" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /^查找所有引用/ }));
    expect(editorMocks.trigger.mock.calls).toEqual([
      ["minicode.editor-action", "actions.find", {}],
      ["minicode.editor-action", "editor.action.revealDefinition", {}],
      ["minicode.editor-action", "editor.action.referenceSearch.trigger", {}],
    ]);
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

  it("opens Markdown as a single live editing surface without an edit/preview mode switch", async () => {
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
    expect(screen.getByTestId("live-markdown-editor")).toBeTruthy();
    expect(screen.queryByRole("tablist", { name: "Markdown 视图模式" })).toBeNull();
    const path = screen.getByRole("navigation", { name: "文件路径" });
    expect(path.getAttribute("title")).toBe("docs/README.md");


    expect(screen.getByRole("heading", { name: "Hello" })).toBeTruthy();
    const image = screen.getByRole("img", { name: "Logo" });
    expect(image.getAttribute("loading")).toBe("lazy");
    expect(image.getAttribute("src")).toContain("docs%2Fassets%2Flogo.svg");
    expect(new URL(image.getAttribute("src")!).searchParams.get("workspace_root")).toBe("C:\\projects\\demo");
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
    await screen.findByTestId("live-markdown-editor");

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
    await screen.findByTestId("live-markdown-editor");

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

  it("explains missing files without displaying the raw IPC failure and closes the stale tab", () => {
    useAppStore.setState({
      editorTabs: [{
        id: "editor-missing-instructions",
        path: ".minicode/INSTRUCTIONS.md",
        content: "",
        original: "",
        loading: false,
        error: "Error invoking remote method 'minicode:fs:readFile': Error: ENOENT: no such file or directory",
        largeFile: false,
      }],
      activeTabPath: ".minicode/INSTRUCTIONS.md",
    });

    const { container } = render(<EditorPanel />);
    expect(screen.getByText("文件不存在或已移动")).toBeTruthy();
    expect((container.querySelector("details") as HTMLDetailsElement).open).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "关闭标签" }));
    expect(useAppStore.getState().editorTabs).toHaveLength(0);
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

  it("reads an HTML script link from its owning directory through the desktop editor", async () => {
    vi.mocked(isDesktop).mockReturnValue(true);
    vi.mocked(fsReadFileInfo).mockResolvedValue({ content: "const game = {};", contentHash: "game-hash" });
    const { WorkspaceHTMLWorker } = await import("./workspaceHtmlService");
    const { URI } = await import("monaco-editor/base/common/uri.js");
    const { editorModelUri, registerMiniCodeEditorOpener } = await vi.importActual<typeof import("./monacoLanguageServices")>("./monacoLanguageServices");
    const owner = URI.parse(editorModelUri("mario-game/index.html", useAppStore.getState().workingDirectory));
    const worker = new WorkspaceHTMLWorker({ getMirrorModels: () => [{
      uri: owner, version: 1, getValue: () => '<script src="js/game.js"></script>',
    }] }, { languageId: "html", languageSettings: {} });
    const [link] = await worker.findDocumentLinks(owner.toString());
    let opener!: import("monaco-editor/editor/editor.api.js").editor.ICodeEditorOpener;
    const registration = registerMiniCodeEditorOpener({ editor: {
      registerEditorOpener: (handler: typeof opener) => { opener = handler; return { dispose() {} }; },
    } } as unknown as typeof import("monaco-editor/editor/editor.api.js"),
    (path, label, target) => useAppStore.getState().openEditorFile(path, label, target));
    opener.openCodeEditor({} as import("monaco-editor/editor/editor.api.js").editor.ICodeEditor, URI.parse(link.target));

    render(<EditorPanel />);

    expect((await screen.findByTestId("monaco-editor") as HTMLTextAreaElement).value).toBe("const game = {};");
    expect(useAppStore.getState().activeTabPath).toBe("mario-game/js/game.js");
    expect(fsReadFileInfo).toHaveBeenCalledExactlyOnceWith("C:/projects/demo/mario-game/js/game.js");
    registration.dispose();
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

  it("opens image-heavy Markdown directly in its live editable buffer", () => {
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


    expect(screen.getByTestId("live-markdown-editor")).toBeTruthy();
    expect((screen.getByTestId("monaco-editor") as HTMLTextAreaElement).value).toBe(imageHeavyMarkdown);
    expect(screen.queryByText("已跳过 Markdown 预览")).toBeNull();
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

    act(() => useAppStore.setState({ fileChanges: [{ path: "growing.txt", event: "modify", sequence: 1, timestamp: 1, workspaceRoot: useAppStore.getState().workingDirectory }] }));

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

  it("navigates file tabs with arrow, Home and End keys while retaining their buffers", async () => {
    const paths = ["first.ts", "second.ts", "third.ts"];
    useAppStore.setState({
      editorTabs: paths.map((path) => ({ id: path, path, content: path, original: path, loading: false })),
      activeTabPath: paths[0],
    });
    render(<EditorPanel />);
    await screen.findByTestId("monaco-editor");
    expect(screen.getByRole("tablist", { name: "打开的文件" })).toBeTruthy();
    const tab = (index: number) => screen.getByRole("tab", { name: paths[index], exact: true });
    expect(tab(0).getAttribute("tabindex")).toBe("0");
    expect(tab(1).getAttribute("tabindex")).toBe("-1");

    fireEvent.keyDown(tab(0), { key: "ArrowLeft" });
    expect(useAppStore.getState().activeTabPath).toBe(paths[2]);
    expect(document.activeElement).toBe(tab(2));
    fireEvent.keyDown(tab(2), { key: "ArrowRight" });
    expect(document.activeElement).toBe(tab(0));
    fireEvent.keyDown(tab(0), { key: "End" });
    expect(tab(2).getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(tab(2), { key: "Home" });
    expect(useAppStore.getState().activeTabPath).toBe(paths[0]);
    expect(useAppStore.getState().editorTabs.map((item) => item.content)).toEqual(paths);
  });

  it("keeps unsaved content when middle-click close is cancelled and closes it only after confirmation", async () => {
    useAppStore.setState({
      editorTabs: [{ id: "dirty-tab", path: "draft.ts", content: "unsaved edit", original: "original", loading: false }],
      activeTabPath: "draft.ts",
    });
    editorMocks.showConfirm.mockResolvedValue(false);
    render(<EditorPanel />);
    await screen.findByTestId("monaco-editor");
    const tab = screen.getByRole("tab", { name: "draft.ts" });
    expect(tab.getAttribute("aria-description")).toContain("未保存");
    fireEvent(tab, new MouseEvent("auxclick", { button: 1, bubbles: true }));
    await waitFor(() => expect(editorMocks.showConfirm).toHaveBeenCalledTimes(1));
    expect(useAppStore.getState().editorTabs[0].content).toBe("unsaved edit");

    editorMocks.showConfirm.mockResolvedValue(true);
    fireEvent(tab, new MouseEvent("auxclick", { button: 1, bubbles: true }));
    await waitFor(() => expect(useAppStore.getState().editorTabs).toHaveLength(0));
  });

  it("shows restored cursor positions for each model and hides them for media", async () => {
    const content = "first line\nsecond line\nthird line";
    useAppStore.setState({
      editorTabs: ["first.ts", "second.ts", "image.png"].map((path) => ({ id: path, path, content, original: content, loading: false })),
      activeTabPath: "first.ts",
    });
    render(<EditorPanel />);
    const editor = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;
    act(() => editor.focus());
    fireEvent.select(editor, { target: { selectionStart: 13, selectionEnd: 13 } });
    expect(screen.getByText("第 2 行，第 3 列")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "second.ts" }));
    expect(screen.getByText("第 1 行，第 1 列")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "first.ts" }));
    await waitFor(() => expect(screen.getByText("第 2 行，第 3 列")).toBeTruthy());
    fireEvent.click(screen.getByRole("tab", { name: "image.png" }));
    expect(screen.queryByText(/第 \d+ 行，第 \d+ 列/)).toBeNull();
  });

  it("uses the currently selected file when the mounted Monaco action opens side chat", async () => {
    useAppStore.setState({
      editorTabs: ["first.ts", "second.ts"].map((path) => ({ id: path, path, content: "code", original: "code", loading: false })),
      activeTabPath: "first.ts",
    });
    render(<EditorPanel />);
    await screen.findByTestId("monaco-editor");
    fireEvent.click(screen.getByRole("tab", { name: "second.ts", exact: true }));
    act(() => editorMocks.actions[0].run({
      getSelection: () => ({ startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 14 }),
      getModel: () => ({ getValueInRange: () => "selected code" }),
    }));
    expect(useAppStore.getState().sideChatPendingContext).toMatchObject({ text: "selected code", source: "second.ts" });
  });

  it("shows the workspace-relative path for a code file without exposing the workspace root", async () => {
    const path = "C:\\projects\\demo\\src\\components\\App.tsx";
    useAppStore.setState({
      editorTabs: [{ id: "path-fixture", path, content: "export const App = () => null;", original: "export const App = () => null;", loading: false }],
      activeTabPath: path,
    });
    render(<EditorPanel chrome="minimal" />);

    await screen.findByTestId("monaco-editor");
    const breadcrumb = screen.getByRole("navigation", { name: "文件路径" });
    expect(breadcrumb.getAttribute("title")).toBe("src/components/App.tsx");
    expect(breadcrumb.textContent).toBe("srccomponentsApp.tsx");
    expect(breadcrumb.textContent).not.toContain("projects");
  });

  it("offers selection chat only for nonempty code and reads the current selection when clicked", async () => {
    const code = "  const answer = 42;  ";
    useAppStore.setState({
      editorTabs: [{ id: "selection-fixture", path: "src/answer.ts", content: code, original: code, loading: false }],
      activeTabPath: "src/answer.ts",
    });
    render(<EditorPanel />);
    const editor = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;
    expect(screen.queryByRole("button", { name: "询问选区" })).toBeNull();

    act(() => editor.focus());
    fireEvent.select(editor, { target: { selectionStart: 0, selectionEnd: 2 } });
    expect(screen.queryByRole("button", { name: "询问选区" })).toBeNull();
    fireEvent.select(editor, { target: { selectionStart: 2, selectionEnd: 7 } });
    const ask = screen.getByRole("button", { name: "询问选区" });
    editor.setSelectionRange(0, code.length);
    fireEvent.click(ask);

    expect(useAppStore.getState().sideChatPendingContext).toEqual({ text: code, source: "src/answer.ts", workspaceRoot: "C:\\projects\\demo",
      range: { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: code.length + 1 } });
    expect(useAppStore.getState().rightStackTab).toBe("sidechat");
    expect(useAppStore.getState().editorTabs[0].content).toBe(code);
    expect(useAppStore.getState().sideChats).toEqual({});
  });

  it("includes the complete last line when returning to a manually typed line-range reference", async () => {
    const code = "first\nsecond\nthird\nfourth";
    useAppStore.setState({ editorTabs: [{ id: "range-reference", path: "src/range.ts", content: code, original: code, loading: false }], activeTabPath: "src/range.ts" });
    render(<EditorPanel />);
    const editor = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;
    act(() => useAppStore.getState().openEditorFile("src/range.ts", "range.ts", { exact: true, line: 2, endLine: 4 }));
    await waitFor(() => expect([editor.selectionStart, editor.selectionEnd]).toEqual([code.indexOf("second"), code.length]));
    expect(code.slice(editor.selectionStart, editor.selectionEnd)).toBe("second\nthird\nfourth");
  });

  it("follows selection changes across models without reusing the previous file's selection", async () => {
    useAppStore.setState({
      editorTabs: ["first.ts", "second.ts"].map((path) => ({ id: path, path, content: `const ${path.split(".")[0]} = 1;`, original: `const ${path.split(".")[0]} = 1;`, loading: false })),
      activeTabPath: "first.ts",
    });
    render(<EditorPanel />);
    const editor = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;
    act(() => editor.focus());
    fireEvent.select(editor, { target: { selectionStart: 0, selectionEnd: editor.value.length } });
    expect(screen.getByRole("button", { name: "询问选区" })).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "second.ts", exact: true }));
    expect(screen.queryByRole("button", { name: "询问选区" })).toBeNull();
    act(() => editor.focus());
    fireEvent.select(editor, { target: { selectionStart: 0, selectionEnd: editor.value.length } });
    fireEvent.click(screen.getByRole("button", { name: "询问选区" }));
    expect(useAppStore.getState().sideChatPendingContext).toEqual({ text: "const second = 1;", source: "second.ts", workspaceRoot: "C:\\projects\\demo",
      range: { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 18 } });

    fireEvent.click(screen.getByRole("tab", { name: "first.ts", exact: true }));
    expect(screen.getByRole("button", { name: "询问选区" })).toBeTruthy();
    act(() => editor.focus());
    fireEvent.select(editor, { target: { selectionStart: 0, selectionEnd: 0 } });
    expect(screen.queryByRole("button", { name: "询问选区" })).toBeNull();
  });

  it("keeps generated tool results read-only while allowing search and selection questions", async () => {
    const tab = { id: "readonly-selection-fixture", path: ".minicode/tool-result.txt", content: "generated output", original: "generated output", loading: false, readOnly: true };
    useAppStore.setState({ editorTabs: [tab], activeTabPath: tab.path });
    render(<EditorPanel />);
    const editor = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;

    act(() => editor.focus());
    fireEvent.select(editor, { target: { selectionStart: 0, selectionEnd: editor.value.length } });
    expect(editor.readOnly).toBe(true);
    expect(screen.getByRole("button", { name: "询问选区" })).toBeTruthy();
    expect(screen.getByRole("navigation", { name: "文件路径" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "查找" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "询问选区" }));
    expect(useAppStore.getState().sideChatPendingContext).toEqual({ text: "generated output", source: tab.path, workspaceRoot: "C:\\projects\\demo",
      range: { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 17 } });
    expect(useAppStore.getState().editorTabs[0].readOnly).toBe(true);
  });

  it("saves live Markdown source and reuses its selection in side chat", async () => {
    const source = "# Title\n\nOriginal paragraph";
    const updated = "# Updated\n\nChanged **paragraph**";
    const tab = { id: "live-md-save", path: "docs/readme.md", content: source, original: source, loading: false, contentHash: "before" };
    useAppStore.setState({ editorTabs: [tab], activeTabPath: tab.path });
    vi.mocked(compareWriteWorkspaceFile).mockResolvedValue({ ok: true, file: { content: updated, content_hash: "after" } });
    render(<EditorPanel />);
    const input = await screen.findByTestId("monaco-editor") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: updated } });
    act(() => window.dispatchEvent(new Event("editor:save")));
    await waitFor(() => expect(compareWriteWorkspaceFile).toHaveBeenCalledWith(tab.path, "before", updated, "C:\\projects\\demo"));
    await waitFor(() => expect(useAppStore.getState().editorTabs[0].original).toBe(updated));
    act(() => input.focus());
    fireEvent.select(input, { target: { selectionStart: updated.indexOf("Changed"), selectionEnd: updated.length } });
    fireEvent.click(screen.getByRole("button", { name: "询问选区" }));
    expect(useAppStore.getState().sideChatPendingContext).toEqual({ text: "Changed **paragraph**", source: tab.path, workspaceRoot: "C:\\projects\\demo",
      range: { startLineNumber: 3, startColumn: 1, endLineNumber: 3, endColumn: 22 } });
    expect(screen.queryByRole("tablist", { name: "Markdown 视图模式" })).toBeNull();
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
      fileChanges: [{ path: "renamed.txt", event: "create", sequence: 1, timestamp: 1, workspaceRoot: useAppStore.getState().workingDirectory }],
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
    const renamedModelPath = screen.getByTestId("monaco-editor").getAttribute("data-model-path")!;
    expect(renamedModelPath).not.toBe(modelPath);
    expect(decodeURIComponent(new URL(renamedModelPath).pathname)).toBe("/C:/projects/demo/renamed.txt");
    await act(async () => { window.dispatchEvent(new Event("editor:save")); });
    expect(compareWriteWorkspaceFile).toHaveBeenLastCalledWith("renamed.txt", "saved hash", "second edit", "C:\\projects\\demo");
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ content: "second edit", original: "second edit", contentHash: "second hash" });
  });

  it("adopts multi-file model edits without loading or writing files until Save All", async () => {
    useAppStore.setState({
      editorTabs: [{ id: "model-source", path: "src/active.ts", content: "old active", original: "old active", contentHash: "active-hash", loading: false }],
      activeTabPath: "src/active.ts",
    });
    render(<EditorPanel />);
    await screen.findByTestId("monaco-editor");
    act(() => useAppStore.getState().adoptEditorModelChanges([
      { path: "src/active.ts", content: "new active", original: "old active", contentHash: "active-hash" },
      { path: "src/closed.ts", content: "new closed", original: "old closed", contentHash: "closed-hash" },
    ], "C:/projects/demo"));
    expect(useAppStore.getState().activeTabPath).toBe("src/active.ts");
    expect(useAppStore.getState().editorTabs[1]).toMatchObject({ path: "src/closed.ts", content: "new closed", original: "old closed", loading: false });
    expect(readWorkspaceFile).not.toHaveBeenCalled();
    expect(compareWriteWorkspaceFile).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "更多编辑器操作" }));
    const saveAll = screen.getByRole("menuitem", { name: /保存全部（2）/ });
    vi.mocked(compareWriteWorkspaceFile).mockImplementation(async (path, _hash, content) => ({ ok: true, file: { path, content, content_hash: `${path}-saved` } }));
    fireEvent.click(saveAll);
    await waitFor(() => expect(compareWriteWorkspaceFile).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(useAppStore.getState().editorTabs.every((tab) => tab.content === tab.original)).toBe(true));
    expect(useAppStore.getState().activeTabPath).toBe("src/active.ts");
  });

  it("saves unaffected files while retaining conflicting and failed drafts with their full paths", async () => {
    useAppStore.setState({
      editorTabs: ["src/ok.ts", "docs/shared.ts", "src/shared.ts", "readonly.txt"].map((path, index) => ({ id: path, path, content: `draft-${index}`, original: `disk-${index}`, contentHash: `hash-${index}`, loading: false, readOnly: index === 3 })),
      activeTabPath: "src/ok.ts",
    });
    vi.mocked(compareWriteWorkspaceFile).mockImplementation(async (path, _hash, content) => {
      if (path === "docs/shared.ts") return { ok: false, conflict: true, message: "Changed on disk" };
      if (path === "src/shared.ts") return { ok: false, conflict: false, message: "Write unavailable" };
      return { ok: true, file: { path, content, content_hash: "saved-hash" } };
    });
    render(<EditorPanel />);
    await screen.findByTestId("monaco-editor");
    act(() => window.dispatchEvent(new Event("editor:save-all")));
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith("已保存 1 个文件；磁盘冲突：docs/shared.ts；保存失败：src/shared.ts", "error", 6000));
    expect(compareWriteWorkspaceFile).toHaveBeenCalledTimes(3);
    expect(compareWriteWorkspaceFile).toHaveBeenCalledWith("docs/shared.ts", "hash-1", "draft-1", "C:\\projects\\demo");
    const tabs = useAppStore.getState().editorTabs;
    expect(tabs[0]).toMatchObject({ content: "draft-0", original: "draft-0", contentHash: "saved-hash" });
    expect(tabs[1]).toMatchObject({ content: "draft-1", original: "disk-1", contentHash: "hash-1", externalChanged: true });
    expect(tabs[2]).toMatchObject({ content: "draft-2", original: "disk-2", contentHash: "hash-2" });
    expect(tabs[3]).toMatchObject({ content: "draft-3", original: "disk-3", readOnly: true });
  });

  it("keeps Save All attached to stable buffers during a pending directory rename and newer edits", async () => {
    let resolveFirst!: (result: Awaited<ReturnType<typeof compareWriteWorkspaceFile>>) => void;
    useAppStore.setState({
      editorTabs: ["a", "b"].map((name) => ({ id: name, path: `src/${name}.ts`, content: `${name}-draft`, original: `${name}-disk`, contentHash: `${name}-hash`, loading: false })),
      activeTabPath: "src/a.ts",
    });
    vi.mocked(compareWriteWorkspaceFile).mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
      .mockImplementationOnce(async (path, _hash, content) => ({ ok: true, file: { path, content, content_hash: "b-saved" } }));
    render(<EditorPanel />);
    await screen.findByTestId("monaco-editor");
    act(() => window.dispatchEvent(new Event("editor:save-all")));
    expect(compareWriteWorkspaceFile).toHaveBeenCalledWith("src/a.ts", "a-hash", "a-draft", "C:\\projects\\demo");
    act(() => {
      useAppStore.getState().updateTabContent("src/a.ts", "a-newer");
      useAppStore.getState().renameEditorPath("src", "lib", "C:/projects/demo");
    });
    await act(async () => resolveFirst({ ok: true, file: { path: "src/a.ts", content: "a-draft", content_hash: "a-saved" } }));
    await waitFor(() => expect(compareWriteWorkspaceFile).toHaveBeenLastCalledWith("lib/b.ts", "b-hash", "b-draft", "C:\\projects\\demo"));
    await waitFor(() => expect(useAppStore.getState().editorTabs[1].original).toBe("b-draft"));
    expect(useAppStore.getState().editorTabs[0]).toMatchObject({ id: "a", path: "lib/a.ts", content: "a-newer", original: "a-draft", contentHash: "a-saved" });
    expect(useAppStore.getState().activeTabPath).toBe("lib/a.ts");
  });

  it("finishes Save All in its original workspace after switching away", async () => {
    let resolveFirst!: (result: Awaited<ReturnType<typeof compareWriteWorkspaceFile>>) => void;
    useAppStore.setState({
      editorTabs: ["a", "b"].map((name) => ({ id: name, path: `src/${name}.ts`, content: `${name}-draft`, original: `${name}-disk`, contentHash: `${name}-hash`, loading: false })),
      activeTabPath: "src/a.ts",
    });
    vi.mocked(compareWriteWorkspaceFile).mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
      .mockImplementationOnce(async (path, _hash, content) => ({ ok: true, file: { path, content, content_hash: "b-saved" } }));
    render(<EditorPanel />);
    await screen.findByTestId("monaco-editor");
    act(() => window.dispatchEvent(new Event("editor:save-all")));
    act(() => useAppStore.getState().setWorkingDirectory("C:/other"));
    await act(async () => resolveFirst({ ok: true, file: { path: "src/a.ts", content: "a-draft", content_hash: "a-saved" } }));
    await waitFor(() => expect(compareWriteWorkspaceFile).toHaveBeenLastCalledWith("src/b.ts", "b-hash", "b-draft", "C:\\projects\\demo"));
    await waitFor(() => expect(editorStateForWorkspace("C:/projects/demo").editorTabs[1].original).toBe("b-draft"));
    expect(useAppStore.getState().workingDirectory).toBe("C:/other");
    expect(useAppStore.getState().editorTabs).toEqual([]);
    expect(editorStateForWorkspace("C:/projects/demo").editorTabs[0]).toMatchObject({ id: "a", original: "a-draft", contentHash: "a-saved" });
  });

  it("waits for an existing single-file save before saving that buffer's newer draft", async () => {
    let resolveFirst!: (result: Awaited<ReturnType<typeof compareWriteWorkspaceFile>>) => void;
    useAppStore.setState({
      editorTabs: [{ id: "pending-single", path: "src/a.ts", content: "first draft", original: "disk", contentHash: "old-hash", loading: false }],
      activeTabPath: "src/a.ts",
    });
    vi.mocked(compareWriteWorkspaceFile).mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
      .mockImplementationOnce(async (path, _hash, content) => ({ ok: true, file: { path, content, content_hash: "latest-hash" } }));
    render(<EditorPanel />);
    await screen.findByTestId("monaco-editor");
    act(() => window.dispatchEvent(new Event("editor:save")));
    act(() => useAppStore.getState().updateTabContent("src/a.ts", "newer draft"));
    act(() => window.dispatchEvent(new Event("editor:save-all")));
    expect(compareWriteWorkspaceFile).toHaveBeenCalledTimes(1);
    await act(async () => resolveFirst({ ok: true, file: { path: "src/a.ts", content: "first draft", content_hash: "first-hash" } }));
    await waitFor(() => expect(compareWriteWorkspaceFile).toHaveBeenLastCalledWith("src/a.ts", "first-hash", "newer draft", "C:\\projects\\demo"));
    await waitFor(() => expect(useAppStore.getState().editorTabs[0]).toMatchObject({ id: "pending-single", content: "newer draft", original: "newer draft", contentHash: "latest-hash" }));
    expect(compareWriteWorkspaceFile).toHaveBeenCalledTimes(2);
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
    if (kind === "markdown") await screen.findByTestId("live-markdown-editor");
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

  it.each(["image", "pdf"])("retries a workspace %s without rereading text or changing the resource owner", async (kind) => {
    const path = `assets/report.${kind === "pdf" ? "pdf" : "png"}`;
    useAppStore.setState({ editorTabs: [{ id: "retry-media", path, content: "", original: "", loading: false }], activeTabPath: path });
    render(<EditorPanel />);
    if (kind === "pdf") await screen.findByTestId("pdf-preview");
    const before = kind === "pdf" ? screen.getByTestId("pdf-preview").getAttribute("data-url")! : screen.getByRole("img").getAttribute("src")!;
    if (kind === "image") fireEvent.error(screen.getByRole("img"));
    fireEvent.click(screen.getByRole("button", { name: `重试${kind === "pdf" ? " PDF " : "图片"}预览` }));
    const after = new URL(kind === "pdf" ? screen.getByTestId("pdf-preview").getAttribute("data-url")! : screen.getByRole("img").getAttribute("src")!);
    expect(after.toString()).not.toBe(before);
    expect(after.searchParams.get("path")).toBe(path);
    expect(after.searchParams.get("workspace_root")).toBe("C:\\projects\\demo");
    expect(after.searchParams.get("preview_retry")).toBe("1");
    expect(readWorkspaceFile).not.toHaveBeenCalled();
  });

  it("ignores another workspace's retained deletion when the editor remounts", async () => {
    useAppStore.getState().addFileChange({ path: "same.ts", event: "delete", timestamp: 1 });
    useAppStore.getState().setWorkingDirectory("C:/other");
    useAppStore.setState({
      editorTabs: [{ id: "other-workspace-buffer", path: "same.ts", content: "disk", original: "disk", loading: false, externalChanged: false }],
      activeTabPath: "same.ts",
    });
    render(<EditorPanel />);
    await screen.findByTestId("monaco-editor");
    expect(useAppStore.getState().editorTabs[0].externalChanged).toBe(false);
    expect(readWorkspaceFile).not.toHaveBeenCalled();
  });

  it("uses only the media file owner's change sequence when the editor remounts", () => {
    useAppStore.getState().addFileChange({ path: "image.svg", event: "modify", timestamp: 1 });
    useAppStore.getState().setWorkingDirectory("C:/other");
    useAppStore.setState({
      editorTabs: [{ id: "other-workspace-image", path: "image.svg", content: "", original: "", loading: false }],
      activeTabPath: "image.svg",
    });
    render(<EditorPanel />);
    expect(new URL(screen.getByRole("img").getAttribute("src")!).searchParams.get("version")).toBe("0");
    act(() => useAppStore.getState().addFileChange({ path: "image.svg", event: "modify", timestamp: 2 }));
    expect(new URL(screen.getByRole("img").getAttribute("src")!).searchParams.get("version")).toBe("2");
  });
});
