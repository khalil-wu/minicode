/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fsReadFileInfo, fsSearchFiles, isDesktop } from "../desktop/runtime";
import { compareWriteWorkspaceFile, searchWorkspaceFiles } from "../protocol/workspace";
import { useAppStore } from "../stores";
import { EditorPanel } from "./EditorPanel";

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
    default: ({ value, onChange, options }: { value: string; onChange?: (value: string) => void; options?: { readOnly?: boolean } }) =>
      ReactModule.createElement("textarea", {
        "data-testid": "monaco-editor",
        value,
        readOnly: options?.readOnly,
        onChange: (event: React.ChangeEvent<HTMLTextAreaElement>) => onChange?.(event.currentTarget.value),
      }),
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

describe("EditorPanel", () => {
  beforeEach(() => {
    vi.mocked(isDesktop).mockReturnValue(false);
    useAppStore.setState({
      themeMode: "dark",
      workingDirectory: "C:\\projects\\demo",
      editorOpenRequests: [],
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

  it("uses localized guidance when Code mode has no open file", () => {
    const { container } = render(<EditorPanel />);

    expect(container.querySelector(".editor-empty-file-icon svg")).toBeTruthy();
    expect(screen.getAllByText("未打开文件").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("从左侧项目文件或搜索中打开工作区文件。")).toBeTruthy();
  });

  it("opens Markdown files in edit mode by default and renders through the Markdown mode switch", async () => {
    useAppStore.setState({
      editorTabs: [{
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
    expect(screen.getByRole("tab", { name: "编辑" }).getAttribute("aria-selected")).toBe("false");
    expect(screen.getByRole("tab", { name: "预览" }).getAttribute("aria-selected")).toBe("true");
  });

  it("renders workspace-local absolute Markdown assets through relative raw URLs", async () => {
    useAppStore.setState({
      editorTabs: [{
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
    expect(image.getAttribute("src")).not.toContain("C%3A");
  });

  it("can hide its own file tab chrome when hosted by the main work area", () => {
    useAppStore.setState({
      editorTabs: [{
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

  it("renders PDF files directly in the editor pane", () => {
    useAppStore.setState({
      editorTabs: [{
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

    const frame = screen.getByTitle("report.pdf");
    expect(frame.tagName.toLowerCase()).toBe("iframe");
    expect(frame.getAttribute("src")).toContain("docs%2Freport.pdf");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts allow-same-origin");
    expect(frame.getAttribute("referrerpolicy")).toBe("no-referrer");
    expect(screen.queryByTestId("monaco-editor")).toBeNull();
  });

  it("renders SVG image files directly in the editor pane", () => {
    useAppStore.setState({
      editorTabs: [{
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

  it("normalizes absolute PDF and SVG tab paths through the active workspace", () => {
    useAppStore.setState({
      editorTabs: [{
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

    const frame = screen.getByTitle("report.pdf");
    const frameUrl = new URL(frame.getAttribute("src")!);
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
      file: { content: string; content_hash: string };
    }) => void) | undefined;
    vi.mocked(compareWriteWorkspaceFile).mockImplementation(() => new Promise((resolve) => {
      resolveSave = resolve;
    }));
    useAppStore.setState({
      editorTabs: [{
        path: "src/app.ts",
        content: "first edit",
        original: "disk baseline",
        contentHash: "hash-before",
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
        file: { content: "first edit", content_hash: "hash-first-edit" },
      });
      await Promise.resolve();
    });

    const tab = useAppStore.getState().editorTabs.find((item) => item.path === "src/app.ts");
    expect(tab?.content).toBe("second edit while saving");
    expect(tab?.original).toBe("first edit");
    expect(tab?.contentHash).toBe("hash-first-edit");
    expect(tab?.content).not.toBe(tab?.original);
  });
});
