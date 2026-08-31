import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "./index";

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(),
}));

const storage = new Map<string, string>();

beforeEach(() => {
  storage.clear();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => {
      storage.set(key, value);
    },
    removeItem: (key: string) => {
      storage.delete(key);
    },
  });
  useAppStore.setState({
    workingDirectory: "",
    workspaceGit: null,
    editorTabs: [],
    activeTabPath: null,
    activeEditorPath: null,
    editorOpenRequests: [],
    conversations: [],
    conversationId: null,
    messages: [],
    conversationMessages: {},
    conversationStreaming: {},
    isStreaming: false,
  });
});

describe("editor workspace isolation", () => {
  it("keeps open editor tabs scoped to the active workspace", () => {
    useAppStore.getState().setWorkingDirectory("C:\\projects\\alpha");
    useAppStore.getState().openEditorTab("src/alpha.ts");
    useAppStore.setState({ activeEditorPath: "src/alpha.ts" });

    expect(useAppStore.getState().editorTabs.map((tab) => tab.path)).toEqual(["src/alpha.ts"]);
    expect(storage.get("minicode.editor.tabs:c:/projects/alpha")).toBe(JSON.stringify(["src/alpha.ts"]));

    useAppStore.getState().setWorkingDirectory("C:\\projects\\beta");

    expect(useAppStore.getState().editorTabs).toEqual([]);
    expect(useAppStore.getState().activeTabPath).toBeNull();
    expect(useAppStore.getState().activeEditorPath).toBeNull();

    useAppStore.getState().openEditorTab("src/beta.ts");

    expect(useAppStore.getState().editorTabs.map((tab) => tab.path)).toEqual(["src/beta.ts"]);
    expect(storage.get("minicode.editor.tabs:c:/projects/beta")).toBe(JSON.stringify(["src/beta.ts"]));

    useAppStore.getState().setWorkingDirectory("C:\\projects\\alpha");

    expect(useAppStore.getState().editorTabs.map((tab) => tab.path)).toEqual(["src/alpha.ts"]);
    expect(useAppStore.getState().activeTabPath).toBe("src/alpha.ts");
    expect(useAppStore.getState().activeEditorPath).toBeNull();
  });

  it("migrates persisted workspace-local absolute tab paths", () => {
    storage.set(
      "minicode.editor.tabs:C:\\Desktop\\MiniCode",
      JSON.stringify(["C:\\Desktop\\MiniCode\\README.md"]),
    );

    useAppStore.getState().setWorkingDirectory("C:\\Desktop\\MiniCode");

    expect(useAppStore.getState().editorTabs.map((tab) => tab.path)).toEqual(["README.md"]);
    expect(storage.get("minicode.editor.tabs:c:/desktop/minicode")).toBe(JSON.stringify(["README.md"]));
  });

  it("preserves editor state across equivalent Windows workspace spellings", () => {
    useAppStore.getState().setWorkingDirectory("C:\\Projects\\Demo");
    useAppStore.getState().openEditorTab("src/app.ts");
    useAppStore.setState({ workspaceGit: { branch: "main", isRepo: true } as never });

    useAppStore.getState().setWorkingDirectory("c:/projects/demo/");

    expect(useAppStore.getState().editorTabs.map((tab) => tab.path)).toEqual(["src/app.ts"]);
    expect(useAppStore.getState().workspaceGit).toMatchObject({ branch: "main", isRepo: true });
    expect(storage.get("minicode.editor.tabs:c:/projects/demo")).toBe(JSON.stringify(["src/app.ts"]));
  });

  it("resets editor state when POSIX workspace case changes", () => {
    useAppStore.getState().setWorkingDirectory("/tmp/Project");
    useAppStore.getState().openEditorTab("src/app.ts");

    useAppStore.getState().setWorkingDirectory("/tmp/project");

    expect(useAppStore.getState().editorTabs).toEqual([]);
    expect(useAppStore.getState().activeTabPath).toBeNull();
  });

  it("normalizes workspace-local absolute paths when opening files in cowork", () => {
    useAppStore.setState({
      workingDirectory: "C:\\Desktop\\MiniCode",
      panelSlots: [{ id: "main-chat", kind: "chat", label: "Chat", focused: true }],
    });

    useAppStore.getState().openEditorFile("C:\\Desktop\\MiniCode\\src\\app.ts", undefined, { line: 12 });
    const request = useAppStore.getState().editorOpenRequests[0];

    expect(request).toMatchObject({ path: "src/app.ts", line: 12 });
    expect(useAppStore.getState().activeEditorPath).toBe("src/app.ts");

    useAppStore.getState().openEditorTab("C:\\Desktop\\MiniCode\\src\\app.ts");

    expect(useAppStore.getState().editorTabs.map((tab) => tab.path)).toEqual(["src/app.ts"]);
    expect(useAppStore.getState().activeTabPath).toBe("src/app.ts");
  });

  it("does not treat a differently-cased POSIX path as workspace-local", () => {
    useAppStore.setState({
      workingDirectory: "/tmp/Project",
      panelSlots: [{ id: "main-chat", kind: "chat", label: "Chat", focused: true }],
    });

    useAppStore.getState().openEditorFile("/tmp/project/src/app.ts");

    expect(useAppStore.getState().editorOpenRequests[0]?.path).toBe("/tmp/project/src/app.ts");
    expect(useAppStore.getState().activeEditorPath).toBe("/tmp/project/src/app.ts");
  });

  it("switches editor tabs when switching conversations from different workspaces", () => {
    useAppStore.setState({
      conversations: [
        { id: "conv-alpha", title: "Alpha", updatedAt: "2026-05-25T00:00:00.000Z", workspaceRoot: "C:\\projects\\alpha" },
        { id: "conv-beta", title: "Beta", updatedAt: "2026-05-25T00:00:01.000Z", workspaceRoot: "C:\\projects\\beta" },
      ],
      conversationId: "conv-alpha",
      workingDirectory: "C:\\projects\\alpha",
    });

    useAppStore.getState().openEditorTab("src/alpha.ts");
    useAppStore.getState().switchConversation("conv-beta");

    expect(useAppStore.getState().workingDirectory).toBe("C:\\projects\\beta");
    expect(useAppStore.getState().editorTabs).toEqual([]);

    useAppStore.getState().openEditorTab("src/beta.ts");
    useAppStore.getState().switchConversation("conv-alpha");

    expect(useAppStore.getState().workingDirectory).toBe("C:\\projects\\alpha");
    expect(useAppStore.getState().editorTabs.map((tab) => tab.path)).toEqual(["src/alpha.ts"]);
  });

  it("clears the active workspace when switching to a conversation without a workspace", () => {
    useAppStore.setState({
      conversations: [
        { id: "conv-alpha", title: "Alpha", updatedAt: "2026-05-25T00:00:00.000Z", workspaceRoot: "C:\\projects\\alpha" },
        { id: "conv-floating", title: "Floating", updatedAt: "2026-05-25T00:00:01.000Z" },
      ],
      conversationId: "conv-alpha",
      workingDirectory: "C:\\projects\\alpha",
    });
    useAppStore.getState().openEditorTab("src/alpha.ts");

    useAppStore.getState().switchConversation("conv-floating");

    expect(useAppStore.getState().workingDirectory).toBe("");
    expect(useAppStore.getState().workspaceGit).toBeNull();
    expect(useAppStore.getState().editorTabs).toEqual([]);
    expect(useAppStore.getState().activeTabPath).toBeNull();
    expect(useAppStore.getState().activeEditorPath).toBeNull();
  });

  it("inserts generated code into the active editor tab without touching unloaded tabs", () => {
    useAppStore.setState({
      editorTabs: [
        { path: "src/active.ts", content: "const a = 1;", original: "const a = 1;", loading: false, error: null },
        { path: "src/loading.ts", content: "", original: "", loading: true, error: null },
      ],
      activeTabPath: "src/active.ts",
      activeEditorPath: "src/active.ts",
    });

    const inserted = useAppStore.getState().insertIntoActiveEditor("const b = 2;");

    expect(inserted).toBe(true);
    expect(useAppStore.getState().editorTabs.find((tab) => tab.path === "src/active.ts")?.content)
      .toBe("const a = 1;\nconst b = 2;");
    expect(useAppStore.getState().editorTabs.find((tab) => tab.path === "src/loading.ts")?.content).toBe("");
  });

  it("keeps editor tabs visible when a file load fails", () => {
    useAppStore.setState({
      workingDirectory: "C:\\projects\\demo",
      editorTabs: [{ path: "missing.txt", content: "", original: "", loading: true, error: null }],
      activeTabPath: "missing.txt",
      activeEditorPath: "missing.txt",
    });

    useAppStore.getState().markTabLoaded("missing.txt", "", "Could not read missing.txt");

    const state = useAppStore.getState();
    expect(state.editorTabs).toHaveLength(1);
    expect(state.editorTabs[0]).toMatchObject({
      path: "missing.txt",
      loading: false,
      error: "Could not read missing.txt",
    });
    expect(state.activeTabPath).toBe("missing.txt");
    expect(state.activeEditorPath).toBe("missing.txt");
  });

  it("keeps oversized editor tabs open but blocks generated inserts into them", () => {
    useAppStore.getState().openEditorTab("data/images.md");
    useAppStore.getState().markTabLoaded("data/images.md", "", null, undefined, {
      largeFile: true,
      loadWarning: "This file is too large to render safely in the editor.",
      sizeBytes: 3 * 1024 * 1024,
    });

    const inserted = useAppStore.getState().insertIntoActiveEditor("new content");
    const tab = useAppStore.getState().editorTabs.find((item) => item.path === "data/images.md");

    expect(inserted).toBe(false);
    expect(tab).toMatchObject({
      path: "data/images.md",
      content: "",
      original: "",
      loading: false,
      error: null,
      largeFile: true,
      sizeBytes: 3 * 1024 * 1024,
    });
  });

  it("clears a stale external-change marker when reopening a media tab", () => {
    useAppStore.setState({
      workingDirectory: "C:\\projects\\demo",
      editorTabs: [{
        path: "assets/preview.PNG",
        content: "",
        original: "",
        loading: false,
        error: null,
        externalChanged: true,
      }],
      activeTabPath: null,
    });

    useAppStore.getState().openEditorTab("assets/preview.PNG");

    expect(useAppStore.getState().activeTabPath).toBe("assets/preview.PNG");
    expect(useAppStore.getState().editorTabs[0]?.externalChanged).toBe(false);
  });

  it("does not clear an external-change marker for a text tab when reopening it", () => {
    useAppStore.setState({
      workingDirectory: "C:\\projects\\demo",
      editorTabs: [{
        path: "src/app.ts",
        content: "local",
        original: "disk",
        loading: false,
        error: null,
        externalChanged: true,
      }],
      activeTabPath: null,
    });

    useAppStore.getState().openEditorTab("src/app.ts");

    expect(useAppStore.getState().editorTabs[0]?.externalChanged).toBe(true);
  });

  it("returns the main canvas to chat after closing the last editor tab", () => {
    useAppStore.setState({
      appMode: "code",
      editorTabs: [
        { path: "README.md", content: "", original: "", loading: false, error: null },
      ],
      activeTabPath: "README.md",
      activeEditorPath: "README.md",
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: false },
        { id: "editor-readme", kind: "editor", label: "README.md", focused: true },
      ],
    });

    useAppStore.getState().closeEditorTab("README.md");

    const state = useAppStore.getState();
    expect(state.editorTabs).toEqual([]);
    expect(state.activeTabPath).toBeNull();
    expect(state.activeEditorPath).toBeNull();
    expect(state.panelSlots).toEqual([
      expect.objectContaining({ id: "main-chat", kind: "chat", focused: true }),
      expect.objectContaining({ kind: "editor", label: "File", focused: false }),
    ]);
  });

  it("persists clamped right sidebar width for side panel resizing", () => {
    useAppStore.getState().setRightSidebarWidth(999);

    expect(useAppStore.getState().rightSidebarWidth).toBe(999);
    expect(storage.get("minicode.layout.right-width")).toBe("999");

    useAppStore.getState().setRightSidebarWidth(1200);

    expect(useAppStore.getState().rightSidebarWidth).toBe(1040);
    expect(storage.get("minicode.layout.right-width")).toBe("1040");

    useAppStore.getState().setRightSidebarWidth(100);

    expect(useAppStore.getState().rightSidebarWidth).toBe(320);
    expect(storage.get("minicode.layout.right-width")).toBe("320");
  });

  it("persists left sidebar width with collapse, minimum, and max bounds", () => {
    useAppStore.getState().setLeftSidebarWidth(0);

    expect(useAppStore.getState().leftSidebarWidth).toBe(0);
    expect(storage.get("minicode.layout.left-width")).toBe("0");

    useAppStore.getState().setLeftSidebarWidth(50);

    expect(useAppStore.getState().leftSidebarWidth).toBe(272);
    expect(storage.get("minicode.layout.left-width")).toBe("272");

    useAppStore.getState().setLeftSidebarWidth(500);

    expect(useAppStore.getState().leftSidebarWidth).toBe(400);
    expect(storage.get("minicode.layout.left-width")).toBe("400");
  });

  it("persists right sidebar open state when toggled or opened by a tab", () => {
    useAppStore.setState({ rightPanelOpen: false, rightStackTabLocked: false });

    useAppStore.getState().toggleRightPanel();

    expect(useAppStore.getState().rightPanelOpen).toBe(true);
    expect(storage.get("minicode.layout.right-open")).toBe("1");

    useAppStore.getState().toggleRightPanel();

    expect(useAppStore.getState().rightPanelOpen).toBe(false);
    expect(storage.get("minicode.layout.right-open")).toBe("0");

    useAppStore.getState().setRightStackTab("preview");

    expect(useAppStore.getState().rightPanelOpen).toBe(true);
    expect(storage.get("minicode.layout.right-open")).toBe("1");
  });

  it("routes legacy terminal tab requests to the bottom drawer", () => {
    useAppStore.setState({ rightPanelOpen: false, dockCollapsed: true, activeBottomTab: "git" });

    useAppStore.getState().setRightStackTab("terminal");

    expect(useAppStore.getState()).toMatchObject({
      rightPanelOpen: false,
      activeBottomTab: "terminal",
      dockCollapsed: false,
    });
    expect(storage.get("minicode.layout.dock-tab")).toBe("terminal");
    expect(storage.get("minicode.layout.dock-collapsed")).toBe("0");
  });

  it("routes legacy plan tab requests to the canonical task activity panel", () => {
    useAppStore.setState({ rightPanelOpen: false, rightStackTab: "preview", rightStackTabLocked: false });

    useAppStore.getState().setRightStackTab("plan");

    expect(useAppStore.getState()).toMatchObject({
      rightPanelOpen: true,
      rightStackTab: "tasks",
      rightStackTabLocked: true,
    });
  });
});
