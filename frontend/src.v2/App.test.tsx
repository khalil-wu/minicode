/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useEffect, useState } from "react";

const lifecycle = vi.hoisted(() => ({ close: vi.fn(), failSettings: false, pendingSettings: null as Promise<void> | null }));
vi.mock("./hooks/useWebSocket", () => ({ useWebSocketConnection: () => {} }));
vi.mock("./hooks/useKeyboardShortcuts", () => ({ useKeyboardShortcuts: () => {} }));
vi.mock("./hooks/useDesktopEvents", () => ({ useDesktopEvents: () => {} }));
vi.mock("./hooks/useWorkspaceGit", () => ({ useWorkspaceGit: () => {} }));
vi.mock("./overlays/QuickOpen", () => ({ QuickOpen: () => null }));
vi.mock("./overlays/ToastContainer", () => ({ ToastContainer: () => null }));
vi.mock("./overlays/SettingsCenter", () => ({ SettingsCenter: () => {
  const [draft, setDraft] = useState("");
  if (lifecycle.failSettings) throw new Error("fixture settings error");
  if (lifecycle.pendingSettings) throw lifecycle.pendingSettings;
  return <main>Settings page<textarea aria-label="settings draft" value={draft} onChange={(event) => setDraft(event.target.value)} /></main>;
} }));
vi.mock("./overlays/AgentEditor", () => ({ AgentEditor: () => {
  const [draft, setDraft] = useState("");
  return <textarea aria-label="agent draft" value={draft} onChange={(event) => setDraft(event.target.value)} />;
} }));
vi.mock("./overlays/CommandPalette", () => ({ CommandPalette: () => <main>Command palette page</main> }));
vi.mock("./shell/WorkbenchShell", () => ({ WorkbenchShell: () => {
  const [draft, setDraft] = useState("");
  const settingsOpen = useAppStore((state) => state.settingsOpen);
  useEffect(() => () => lifecycle.close(), []);
  return <>
    <header aria-label="应用标题栏"><button type="button">原生窗口控制</button></header>
    <nav aria-label="应用导航"><button type="button">设置入口</button></nav>
    <div hidden={settingsOpen} style={{ display: settingsOpen ? "none" : "contents" }}><textarea aria-label="side-chat draft" value={draft} onChange={(event) => setDraft(event.target.value)} /></div>
  </>;
} }));

import { useAppStore } from "./stores";
import { App } from "./App";

afterEach(() => { cleanup(); lifecycle.failSettings = false; lifecycle.pendingSettings = null; vi.restoreAllMocks(); });

it("lets a different overlay open after a previous route's rendering failure", async () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  useAppStore.setState({ settingsOpen: true, commandPaletteOpen: false, automationsOpen: false,
    shortcutsHelpOpen: false, liveArtifactsOpen: false, agentEditorOpen: false });
  lifecycle.failSettings = true;
  render(<App />);
  expect(await screen.findByText("fixture settings error")).toBeTruthy();
  await act(async () => useAppStore.getState().toggleCommandPalette());
  expect(await screen.findByText("Command palette page")).toBeTruthy();
  expect(screen.getByText("fixture settings error").closest("[hidden]")).toBeTruthy();
});

it("keeps workbench drafts and resources alive while visiting settings", async () => {
  useAppStore.setState({ settingsOpen: false, commandPaletteOpen: false, automationsOpen: false,
    shortcutsHelpOpen: false, skillsMarketplaceOpen: false, liveArtifactsOpen: false, agentEditorOpen: false });
  lifecycle.close.mockClear();
  render(<App />);
  const input = screen.getByRole("textbox", { name: "side-chat draft" });
  const titlebar = screen.getByRole("banner", { name: "应用标题栏" });
  const rail = screen.getByRole("navigation", { name: "应用导航" });
  fireEvent.change(input, { target: { value: "unfinished task" } });
  await act(async () => useAppStore.setState({ settingsOpen: true }));
  expect(await screen.findByText("Settings page")).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: "side-chat draft" })).toBeNull();
  expect(input.isConnected).toBe(true);
  expect(lifecycle.close).not.toHaveBeenCalled();
  expect(screen.getByRole("banner", { name: "应用标题栏" })).toBe(titlebar);
  expect(screen.getByRole("navigation", { name: "应用导航" })).toBe(rail);
  expect(screen.getByRole("button", { name: "原生窗口控制" }).closest("[hidden]")).toBeNull();
  act(() => useAppStore.setState({ settingsOpen: false }));
  expect(screen.getByRole("textbox", { name: "side-chat draft" })).toBe(input);
  expect((input as HTMLTextAreaElement).value).toBe("unfinished task");
});

it("limits settings loading to its workspace stage while preserving global chrome", async () => {
  let complete!: () => void;
  lifecycle.pendingSettings = new Promise<void>((resolve) => { complete = resolve; });
  useAppStore.setState({ settingsOpen: true, commandPaletteOpen: false, automationsOpen: false,
    shortcutsHelpOpen: false, skillsMarketplaceOpen: false, liveArtifactsOpen: false, agentEditorOpen: false });
  render(<App />);
  const loading = await screen.findByRole("status", { name: "正在加载设置" });
  expect(loading.getAttribute("data-scope")).toBe("settings");
  expect(screen.getAllByRole("banner", { name: "应用标题栏" })).toHaveLength(1);
  expect(screen.getAllByRole("navigation", { name: "应用导航" })).toHaveLength(1);
  expect(screen.getByRole("button", { name: "原生窗口控制" }).closest("[hidden]")).toBeNull();
  expect(screen.queryByRole("textbox", { name: "side-chat draft" })).toBeNull();
  await act(async () => { lifecycle.pendingSettings = null; complete(); });
  await screen.findByText("Settings page");
});

it("retains visited settings and Agent drafts through closing and another overlay", async () => {
  useAppStore.setState({ settingsOpen: true, commandPaletteOpen: false, shortcutsHelpOpen: false,
    liveArtifactsOpen: false, agentEditorOpen: false, runtimeCapabilities: null });
  render(<App />);
  const settings = await screen.findByRole("textbox", { name: "settings draft" });
  fireEvent.change(settings, { target: { value: "unsaved configuration" } });
  act(() => useAppStore.setState({ settingsOpen: false, agentEditorOpen: true }));
  const agent = await screen.findByRole("textbox", { name: "agent draft" });
  fireEvent.change(agent, { target: { value: "unfinished agent" } });
  expect(settings.isConnected).toBe(true);
  expect(screen.queryByRole("textbox", { name: "settings draft" })).toBeNull();
  act(() => useAppStore.setState({ agentEditorOpen: false, commandPaletteOpen: true }));
  await screen.findByText("Command palette page");
  act(() => useAppStore.setState({ commandPaletteOpen: false, settingsOpen: true }));
  expect(screen.getByRole("textbox", { name: "settings draft" })).toBe(settings);
  expect((settings as HTMLTextAreaElement).value).toBe("unsaved configuration");
  act(() => useAppStore.setState({ settingsOpen: false, agentEditorOpen: true }));
  expect(screen.getByRole("textbox", { name: "agent draft" })).toBe(agent);
  expect((agent as HTMLTextAreaElement).value).toBe("unfinished agent");
});
