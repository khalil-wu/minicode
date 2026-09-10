/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useEffect, useState } from "react";

const lifecycle = vi.hoisted(() => ({ close: vi.fn() }));
vi.mock("./hooks/useWebSocket", () => ({ useWebSocketConnection: () => {} }));
vi.mock("./hooks/useKeyboardShortcuts", () => ({ useKeyboardShortcuts: () => {} }));
vi.mock("./hooks/useDesktopEvents", () => ({ useDesktopEvents: () => {} }));
vi.mock("./hooks/useWorkspaceGit", () => ({ useWorkspaceGit: () => {} }));
vi.mock("./overlays/QuickOpen", () => ({ QuickOpen: () => null }));
vi.mock("./overlays/ToastContainer", () => ({ ToastContainer: () => null }));
vi.mock("./overlays/SettingsCenter", () => ({ SettingsCenter: () => <main>Settings page</main> }));
vi.mock("./shell/WorkbenchShell", () => ({ WorkbenchShell: () => {
  const [draft, setDraft] = useState("");
  useEffect(() => () => lifecycle.close(), []);
  return <textarea aria-label="side-chat draft" value={draft} onChange={(event) => setDraft(event.target.value)} />;
} }));

import { useAppStore } from "./stores";
import { App } from "./App";

afterEach(() => cleanup());

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
