/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useEffect, useState } from "react";

const lifecycle = vi.hoisted(() => ({ close: vi.fn(), failSettings: false }));
vi.mock("./hooks/useWebSocket", () => ({ useWebSocketConnection: () => {} }));
vi.mock("./hooks/useKeyboardShortcuts", () => ({ useKeyboardShortcuts: () => {} }));
vi.mock("./hooks/useDesktopEvents", () => ({ useDesktopEvents: () => {} }));
vi.mock("./hooks/useWorkspaceGit", () => ({ useWorkspaceGit: () => {} }));
vi.mock("./overlays/QuickOpen", () => ({ QuickOpen: () => null }));
vi.mock("./overlays/ToastContainer", () => ({ ToastContainer: () => null }));
vi.mock("./overlays/SettingsCenter", () => ({ SettingsCenter: () => {
  const [draft, setDraft] = useState("");
  if (lifecycle.failSettings) throw new Error("fixture settings error");
  return <main>Settings page<textarea aria-label="settings draft" value={draft} onChange={(event) => setDraft(event.target.value)} /></main>;
} }));
vi.mock("./overlays/AgentEditor", () => ({ AgentEditor: () => {
  const [draft, setDraft] = useState("");
  return <textarea aria-label="agent draft" value={draft} onChange={(event) => setDraft(event.target.value)} />;
} }));
vi.mock("./overlays/CommandPalette", () => ({ CommandPalette: () => <main>Command palette page</main> }));
vi.mock("./shell/WorkbenchShell", () => ({ WorkbenchShell: () => {
  const [draft, setDraft] = useState("");
  useEffect(() => () => lifecycle.close(), []);
  return <textarea aria-label="side-chat draft" value={draft} onChange={(event) => setDraft(event.target.value)} />;
} }));

import { useAppStore } from "./stores";
import { App } from "./App";

afterEach(() => { cleanup(); lifecycle.failSettings = false; vi.restoreAllMocks(); });

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
  fireEvent.change(input, { target: { value: "unfinished task" } });
  await act(async () => useAppStore.setState({ settingsOpen: true }));
  expect(await screen.findByText("Settings page")).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: "side-chat draft" })).toBeNull();
  expect(input.isConnected).toBe(true);
  expect(lifecycle.close).not.toHaveBeenCalled();
  act(() => useAppStore.setState({ settingsOpen: false }));
  expect(screen.getByRole("textbox", { name: "side-chat draft" })).toBe(input);
  expect((input as HTMLTextAreaElement).value).toBe("unfinished task");
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
