/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { PluginsTab } from "./PluginsTab";
import { useAppStore } from "../stores";

vi.mock("../protocol/api", () => ({ apiBase: () => "http://test.local", authHeaders: (headers?: HeadersInit) => headers ?? {},
  fetchWithTimeout: (url: string, init?: RequestInit) => fetch(url, init), errorMessageFromResponseText: (text: string) => text,
  LONG_HTTP_TIMEOUT_MS: 60000, pluginAssetResourceUrlWithToken: () => "" }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
vi.mock("../desktop/runtime", () => ({ isDesktop: () => false, pickDirectory: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("uses an installed plugin's actual prompt and registration without replacing a draft", async () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(String(input).includes("/marketplaces")
    ? { marketplaces: [] } : { plugins: [{ id: "notes@local", name: "notes", displayName: "Notes", enabled: true,
      path: "C:/plugins/notes", defaultPrompt: ["Summarize the notes"], skill_count: 1, mcp_server_count: 0 }] }))));
  useAppStore.setState({ workingDirectory: "C:/project", conversationId: "plugin-owner", draft: "Keep my draft", selectedMentions: [],
    availableSkills: [{ name: "review-notes", display_name: "Review notes", description: "Read notes", source_level: "plugin", path: "C:/plugins/notes/skills/review/SKILL.md" }],
    mcpServers: [], panelSlots: [{ id: "chat", kind: "chat", label: "Chat", focused: true }], settingsOpen: true });
  render(<PluginsTab />);
  fireEvent.click(await screen.findByRole("button", { name: "查看插件详情 notes@local" }));
  expect(screen.getByText("Review notes")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Summarize the notes" }));
  expect(useAppStore.getState().draft).toBe("Keep my draft\nSummarize the notes");
  expect(useAppStore.getState().selectedMentions).toMatchObject([{ kind: "plugin", configName: "notes@local", path: "plugin://notes@local" }]);
  expect(useAppStore.getState().conversationId).toBe("plugin-owner");
  expect(useAppStore.getState().settingsOpen).toBe(false);
});
