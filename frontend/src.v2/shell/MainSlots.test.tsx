/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

vi.mock("../chat/ChatPane", () => ({
  ChatPane: () => <div>Chat pane</div>,
}));

vi.mock("../panels/EditorPanel", () => ({
  EditorPanel: () => <div>Editor pane</div>,
}));

import { useAppStore } from "../stores";
import { MainSlots } from "./MainSlots";

describe("MainSlots", () => {
  beforeEach(() => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1200,
    });
    useAppStore.setState({
      panelSlots: [{ id: "main-chat", kind: "chat", label: "Chat", focused: true }],
      editorTabs: [],
      activeTabPath: null,
      rightPanelOpen: false,
      rightStackTab: "preview",
    });
  });

  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  it("does not duplicate right side panel controls inside the workbench canvas", () => {
    render(<MainSlots />);

    expect(screen.queryByRole("button", { name: "Preview" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Activity" })).toBeNull();
    expect(screen.queryByRole("button", { name: /side panel/i })).toBeNull();
  });

  it("shows chat and an opened editor side by side on wide workbench windows", async () => {
    useAppStore.setState({
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: false, size: 1 },
        { id: "editor-readme", kind: "editor", label: "README.md", focused: true, size: 1 },
      ],
      editorTabs: [{ path: "README.md", content: "", original: "", loading: false, error: null }],
      activeTabPath: "README.md",
    });

    render(<MainSlots />);

    expect(await screen.findByText("Editor pane")).toBeTruthy();
    expect(screen.getByText("Chat pane")).toBeTruthy();
    expect(screen.getByRole("separator", { name: "调整主面板宽度" })).toBeTruthy();
    expect(screen.queryByRole("tab", { name: "对话" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "README.md" })).toBeNull();
    expect(useAppStore.getState().panelSlots.some((slot) => slot.kind === "editor")).toBe(true);
    const handle = screen.getByRole("separator", { name: "调整主面板宽度" });
    expect(handle.getAttribute("aria-valuenow")).toBe("50");
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect((useAppStore.getState().panelSlots[0].size ?? 0)).toBeGreaterThan(1);
    fireEvent.keyDown(handle, { key: "Enter" });
    expect(useAppStore.getState().panelSlots[0].size).toBeCloseTo(useAppStore.getState().panelSlots[1].size ?? 0, 5);
  });

  it("keeps compact windows on a single active slot with the switcher", async () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 700,
    });
    useAppStore.setState({
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: false, size: 1 },
        { id: "editor-readme", kind: "editor", label: "README.md", focused: true, size: 1 },
      ],
      editorTabs: [{ path: "README.md", content: "", original: "", loading: false, error: null }],
      activeTabPath: "README.md",
    });

    render(<MainSlots />);

    expect(await screen.findByText("Editor pane")).toBeTruthy();
    expect(screen.queryByText("Chat pane")).toBeNull();
    expect(screen.getByRole("tab", { name: "对话" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "文件" })).toBeTruthy();
    const switcher = screen.getByRole("tablist", { name: "主工作区" });
    expect(switcher.style.width).toBe("164px");
    expect(screen.getByRole("tab", { name: "对话" }).style.whiteSpace).toBe("nowrap");

    fireEvent.click(screen.getByRole("tab", { name: "对话" }));

    expect(screen.getByText("Chat pane")).toBeTruthy();
    expect(screen.queryByText("Editor pane")).toBeNull();
    expect(useAppStore.getState().panelSlots.some((slot) => slot.kind === "editor")).toBe(true);
  });

  it("keeps Code mode on one main slot with Chat and File tabs on wide windows", async () => {
    useAppStore.setState({
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: false, size: 1 },
        { id: "main-editor", kind: "editor", label: "File", focused: true, size: 1 },
      ],
      editorTabs: [{ path: "README.md", content: "", original: "", loading: false, error: null }],
      activeTabPath: "README.md",
    });

    render(<MainSlots mode="tabs" />);

    expect(await screen.findByText("Editor pane")).toBeTruthy();
    expect(screen.getByText("Chat pane").closest<HTMLElement>('[data-panel-slot-kind="chat"]')?.style.display).toBe("none");
    expect(screen.getByRole("tab", { name: "对话" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "文件" })).toBeTruthy();
    expect(screen.queryByRole("separator", { name: "调整主面板宽度" })).toBeNull();

    fireEvent.click(screen.getByRole("tab", { name: "对话" }));

    expect(screen.getByText("Chat pane")).toBeTruthy();
    expect(screen.getByText("Editor pane").closest<HTMLElement>('[data-panel-slot-kind="editor"]')?.style.display).toBe("none");
  });

  it("keeps the chat tree mounted when switching between Cowork chat and a Code editor", async () => {
    useAppStore.setState({
      appMode: "cowork",
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: false, size: 1 },
        { id: "main-editor", kind: "editor", label: "File", focused: true, size: 1 },
      ],
      editorTabs: [{ path: "README.md", content: "", original: "", loading: false, error: null }],
      activeTabPath: "README.md",
    });

    const { rerender } = render(<MainSlots mode="tabs" forceChat />);
    const chatPane = screen.getByText("Chat pane");

    fireEvent.click(screen.getByRole("tab", { name: "文件" }));
    rerender(<MainSlots mode="tabs" />);

    expect(await screen.findByText("Editor pane")).toBeTruthy();
    expect(screen.getByText("Chat pane")).toBe(chatPane);
    expect(useAppStore.getState().appMode).toBe("code");
  });

  it("lets a single Code tab fill the canvas even when a persisted split size is below one", () => {
    useAppStore.setState({
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: true, size: 0.45 },
        { id: "main-editor", kind: "editor", label: "File", focused: false, size: 1.55 },
      ],
    });

    const { container } = render(<MainSlots mode="tabs" />);

    const frame = container.querySelector<HTMLElement>('[data-panel-slot-kind="chat"]');
    expect(frame?.style.flex).toBe("1 1 0px");
  });

  it("keeps the Chat and File switcher when no editor tabs are open", () => {
    useAppStore.setState({
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: true, size: 1 },
        { id: "main-editor", kind: "editor", label: "File", focused: false, size: 1 },
      ],
      editorTabs: [],
      activeTabPath: null,
    });

    render(<MainSlots mode="tabs" />);

    expect(screen.getByText("Chat pane")).toBeTruthy();
    expect(screen.queryByText("Editor pane")).toBeNull();
    expect(screen.getByRole("tab", { name: "对话" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "文件" })).toBeTruthy();
  });

  it("opens files from Cowork by switching into Code mode", () => {
    useAppStore.setState({
      appMode: "cowork",
      panelSlots: [{ id: "main-chat", kind: "chat", label: "Chat", focused: true }],
      editorOpenRequests: [],
      activeEditorPath: null,
    });

    useAppStore.getState().openEditorFile("README.md", "README.md");

    const state = useAppStore.getState();
    expect(state.appMode).toBe("code");
    expect(state.panelSlots.some((slot) => slot.kind === "chat")).toBe(true);
    expect(state.panelSlots.some((slot) => slot.kind === "editor")).toBe(true);
    expect(state.activeEditorPath).toBe("README.md");
  });
});
