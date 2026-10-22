// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ConfigTab } from "./ConfigTab";
import { HooksTab } from "./HooksTab";
import { StorageTab } from "./StorageTab";
import { useAppStore } from "../stores";
import type { ManagedSettingsTarget } from "../desktop/runtime";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), openTarget: vi.fn<(request: ManagedSettingsTarget) => Promise<true>>() }));
vi.mock("../protocol/api", async load => ({ ...await load<typeof import("../protocol/api")>(),
  apiBase: () => "http://registered-settings.test", authHeaders: () => ({}), fetchWithTimeout: mocks.fetch }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => null }));

const workspaceRoot = "C:/Current workspace";
const hook = { key: "catalog-owned-plugin-key", event: "stop", handler_type: "command", command: "node installed-hook.js",
  source: "plugin", source_path: "C:/Installed plugin/.minicode-plugin/plugin.json", plugin_id: "installed-plugin",
  enabled: true, is_managed: false, current_hash: "registered-hash", trust_status: "trusted", timeout_seconds: null };
const configFields = { approval_policy: "on-request", sandbox_mode: "workspace-write",
  network_access: false, web_search: "cached", model_verbosity: "medium", model_reasoning_summary: "auto" };
const config = { path: "C:/Profile/settings.json", ...configFields, effective: configFields, origins: {}, requirements: {},
  scopes: [{ id: "user", kind: "user", path: "C:/Profile/settings.json", version: "user-v1", values: {}, readonly: false,
    source: { type: "user", display: "user (C:/Profile/settings.json)" } }] };
const usage = { path: "C:/Profile/data", total_bytes: 1024, categories: [
  { name: "chats", bytes: 1024 }, { name: "archived", bytes: 0 }, { name: "subagents", bytes: 0 },
  { name: "unclassified", bytes: 0 }, { name: "worktrees", bytes: 0 }, { name: "other", bytes: 0 },
] };
const pages = [
  { name: "Config", render: () => <ConfigTab active />, request: { kind: "config", workspaceRoot, scopeId: "user" } as ManagedSettingsTarget,
    button: async () => { const button = screen.getByRole("button", { name: "打开配置文件" }); await waitFor(() => expect(button).toHaveProperty("disabled", false)); return button; } },
  { name: "Hooks", render: () => <HooksTab />, request: { kind: "hook", hookKey: hook.key } as ManagedSettingsTarget,
    button: async () => { const description = await screen.findByText(hook.command, { selector: ".settings-row-description" }); const details = description.closest("details")!; fireEvent.click(details.querySelector("summary")!); return within(details).getByRole("button", { name: "打开配置文件" }); } },
  { name: "Storage", render: () => <StorageTab />, request: { kind: "data" } as ManagedSettingsTarget,
    button: async () => screen.findByRole("button", { name: `应用数据文件夹: ${usage.path}` }) },
];

beforeEach(() => {
  vi.clearAllMocks();
  mocks.openTarget.mockResolvedValue(true);
  mocks.fetch.mockImplementation(async url => Response.json(new URL(url).pathname.endsWith("/hooks") ? { hooks: [hook], warnings: [] }
    : new URL(url).pathname.endsWith("/storage") ? usage : config));
  window.__MINICODE_RUNTIME__ = { desktop: {
    platformInfo: { isDesktop: true, platform: "win32", arch: "x64" }, settings: { openTarget: mocks.openTarget },
  } } as never;
  useAppStore.setState({ workingDirectory: workspaceRoot, isConnected: false, draft: "Keep unsent text",
    attachments: [{ id: "retained-image", name: "draft.png", type: "image/png", size: 12, status: "ready" }] });
});
afterEach(() => { cleanup(); delete window.__MINICODE_RUNTIME__; });

it.each(pages)("$name uses the real runtime helper with only its registered action identity", async page => {
  const before = useAppStore.getState();
  render(page.render());
  fireEvent.click(await page.button());
  await waitFor(() => expect(mocks.openTarget).toHaveBeenCalledExactlyOnceWith(page.request));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(useAppStore.getState()).toMatchObject({ workingDirectory: workspaceRoot, draft: before.draft, attachments: before.attachments });
  expect(mocks.openTarget.mock.calls[0][0]).not.toHaveProperty("path");
});

it.each(pages)("$name displays the real native shell rejection", async page => {
  mocks.openTarget.mockRejectedValueOnce(new Error("Windows failed to open the registered target"));
  render(page.render());
  fireEvent.click(await page.button());
  expect((await screen.findByRole("alert")).textContent).toContain("Windows failed to open the registered target");
  expect(mocks.openTarget).toHaveBeenCalledExactlyOnceWith(page.request);
});

it.each(pages)("$name retains the settings service or remote-host rejection detail", async page => {
  const message = page.name === "Hooks" ? "Hook is no longer registered; reload its configuration."
    : "Remote settings paths cannot be opened by the local file manager.";
  mocks.openTarget.mockRejectedValueOnce(new Error(message));
  render(page.render());
  fireEvent.click(await page.button());
  expect((await screen.findByRole("alert")).textContent).toContain(message);
  expect(mocks.openTarget).toHaveBeenCalledExactlyOnceWith(page.request);
});
