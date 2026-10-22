// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ConfigTab } from "./ConfigTab";
import { HooksTab } from "./HooksTab";
import { StorageTab } from "./StorageTab";
import { useAppStore } from "../stores";
import { bindWorkspaceOwnerRuntime } from "../protocol/workspace-owner";
import { fsReadFileInfo, registeredSettingsEditorOwner, type ManagedSettingsOpenResult, type ManagedSettingsTarget } from "../desktop/runtime";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), openTarget: vi.fn<(request: ManagedSettingsTarget) => Promise<ManagedSettingsOpenResult>>() }));
vi.mock("../protocol/api", async load => ({ ...await load<typeof import("../protocol/api")>(),
  apiBase: () => "http://registered-settings.test", authHeaders: () => ({}), fetchWithTimeout: mocks.fetch }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "registered-session" }) }));

const workspaceRoot = "C:/Current workspace";
const executionOwner = { sessionId: "registered-session", conversationId: "registered-conversation" };
const hook = { key: "catalog-owned-plugin-key", event: "stop", handler_type: "command", command: "node installed-hook.js",
  source: "plugin", source_path: "C:/Installed plugin/.minicode-plugin/plugin.json", plugin_id: "installed-plugin",
  enabled: true, is_managed: false, current_hash: "registered-hash", trust_status: "trusted", timeout_seconds: null };
const configFields = { approval_policy: "on-request", sandbox_mode: "workspace-write",
  network_access: false, web_search: "cached", model_verbosity: "medium", model_reasoning_summary: "auto" };
const config = { path: "C:/Profile/settings.json", default_scope_id: "user", ...configFields, effective: configFields, origins: {}, requirements: {},
  scopes: [{ id: "user", kind: "user", path: "C:/Profile/settings.json", version: "user-v1", values: {}, readonly: false,
    workspace_root: null, disabled_reason: null,
    source: { type: "user", display: "user (C:/Profile/settings.json)" } }] };
const projectOwner = "C:/Project", projectFile = `${projectOwner}/.minicode/config.toml`;
const projectConfig = { ...config, default_scope_id: `project:${projectOwner}`, scopes: [{
  id: `project:${projectOwner}`, kind: "project", path: projectFile, workspace_root: projectOwner, disabled_reason: null,
  version: "project-v1", values: { sandbox_mode: "read-only" }, readonly: false,
  source: { type: "project", display: `project (${projectFile})` },
}, ...config.scopes] };
const usage = { path: "C:/Profile/data", total_bytes: 1024, categories: [
  { name: "chats", bytes: 1024 }, { name: "archived", bytes: 0 }, { name: "subagents", bytes: 0 },
  { name: "unclassified", bytes: 0 }, { name: "worktrees", bytes: 0 }, { name: "other", bytes: 0 },
] };
const pages = [
  { name: "Config", render: () => <ConfigTab active />, request: { kind: "config", workspaceRoot, scopeId: "user", ...executionOwner } as ManagedSettingsTarget,
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
    conversationId: executionOwner.conversationId,
    editorTabs: [], activeTabPath: null, activeEditorPath: null, editorOpenRequests: [], activeEditorOpenRequestId: null,
    attachments: [{ id: "retained-image", name: "draft.png", type: "image/png", size: 12, status: "ready" }] });
  bindWorkspaceOwnerRuntime(() => ({ session_id: executionOwner.sessionId,
    conversation_id: useAppStore.getState().conversationId ?? "", workspace_root: useAppStore.getState().workingDirectory }));
});
afterEach(() => { cleanup(); delete window.__MINICODE_RUNTIME__; bindWorkspaceOwnerRuntime(() => undefined); });

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

it("opens an ancestor project's registered config in the captured editor workspace without changing dirty buffers or drafts", async () => {
  const owner = `${projectOwner}/Feature`;
  useAppStore.setState({ workingDirectory: owner });
  useAppStore.getState().openEditorTab(projectFile);
  useAppStore.getState().markTabLoaded(projectFile, 'web_search = "cached"', null);
  useAppStore.getState().updateTabContent(projectFile, 'web_search = "live" # unsaved');
  const before = useAppStore.getState();
  mocks.fetch.mockImplementation(async () => Response.json(projectConfig));
  mocks.openTarget.mockResolvedValue({ openInEditor: true, path: projectFile, workspaceRoot: projectOwner });
  render(<ConfigTab active />);
  await screen.findByRole("button", { name: "配置作用域，当前：Project" });
  fireEvent.click(screen.getByRole("button", { name: "打开配置文件" }));
  await waitFor(() => expect(useAppStore.getState().editorOpenRequests).toHaveLength(1));
  const after = useAppStore.getState();
  expect(mocks.openTarget).toHaveBeenCalledExactlyOnceWith({ kind: "config", workspaceRoot: owner, scopeId: `project:${projectOwner}`, ...executionOwner });
  expect(mocks.openTarget.mock.calls[0][0]).not.toHaveProperty("path");
  expect(mocks.openTarget.mock.calls[0][0]).not.toHaveProperty("targetId");
  expect(after.workingDirectory).toBe(owner);
  expect(after.editorOpenRequests[0]).toMatchObject({ path: projectFile, exact: true, preview: false });
  expect(after.editorTabs).toEqual(before.editorTabs);
  expect(after.editorTabs[0].content).toBe('web_search = "live" # unsaved');
  expect(after.editorTabs[0].original).toBe('web_search = "cached"');
  expect(after.editorTabs[0].content).not.toBe(after.editorTabs[0].original);
  expect(after.draft).toBe(before.draft);
  expect(after.attachments).toBe(before.attachments);
  expect(after.workspaceComposerDrafts).toBe(before.workspaceComposerDrafts);
});

it("keeps a late registered-project open result and error with the original settings workspace", async () => {
  const owner = `${projectOwner}/Feature`;
  useAppStore.setState({ workingDirectory: owner });
  let finish!: (result: ManagedSettingsOpenResult) => void;
  mocks.fetch.mockImplementation(async url => Response.json(new URL(url).searchParams.get("workspace_root") === owner ? projectConfig : config));
  mocks.openTarget.mockImplementation(() => new Promise<ManagedSettingsOpenResult>(resolve => { finish = resolve; }));
  render(<ConfigTab active />);
  await screen.findByRole("button", { name: "配置作用域，当前：Project" });
  fireEvent.click(screen.getByRole("button", { name: "打开配置文件" }));
  await waitFor(() => expect(mocks.openTarget).toHaveBeenCalledTimes(1));
  act(() => useAppStore.setState({ workingDirectory: "C:/Other", draft: "Other workspace unsent text" }));
  await screen.findByRole("button", { name: "配置作用域，当前：用户" });
  const other = useAppStore.getState();
  await act(async () => finish({ openInEditor: true, path: projectFile, workspaceRoot: projectOwner }));
  expect(useAppStore.getState().workingDirectory).toBe("C:/Other");
  expect(useAppStore.getState().editorOpenRequests).toBe(other.editorOpenRequests);
  expect(useAppStore.getState().editorTabs).toBe(other.editorTabs);
  expect(useAppStore.getState().draft).toBe("Other workspace unsent text");
  expect(screen.queryByRole("alert")).toBeNull();
  act(() => useAppStore.setState({ workingDirectory: owner }));
  expect((await screen.findByRole("alert")).textContent).toBe("工作区已切换，请返回原工作区后打开配置文件。");
});

it("routes registered project files through the real session editor context without creating a chat or changing ordinary files", async () => {
  const owner = `${projectOwner}/Feature`;
  useAppStore.setState({ workingDirectory: owner, conversationId: null });
  const nativeRead = vi.fn().mockResolvedValue({ content: "ordinary native file" });
  window.__MINICODE_RUNTIME__!.desktop!.fs = { readFile: nativeRead } as never;
  mocks.fetch.mockImplementation(async url => Response.json(new URL(url).pathname === "/api/workspace/file"
    ? { path: projectFile, content: "# registered config", content_hash: "actual-file-hash", session_id: executionOwner.sessionId, conversation_id: "" }
    : projectConfig));
  mocks.openTarget.mockResolvedValue({ openInEditor: true, path: projectFile, workspaceRoot: projectOwner });
  render(<ConfigTab active />);
  await screen.findByRole("button", { name: "配置作用域，当前：Project" });
  fireEvent.click(screen.getByRole("button", { name: "打开配置文件" }));
  await waitFor(() => expect(useAppStore.getState().editorOpenRequests).toHaveLength(1));
  expect(mocks.openTarget).toHaveBeenCalledExactlyOnceWith({ kind: "config", workspaceRoot: owner,
    scopeId: `project:${projectOwner}`, sessionId: executionOwner.sessionId, conversationId: "" });
  expect(useAppStore.getState().conversationId).toBeNull();
  expect(registeredSettingsEditorOwner(projectFile, owner)).toEqual({ session_id: executionOwner.sessionId, conversation_id: "" });
  expect((await fsReadFileInfo(projectFile))?.content).toBe("# registered config");
  const read = new URL(mocks.fetch.mock.calls.find(([url]) => new URL(url).pathname === "/api/workspace/file")![0]);
  expect(read.searchParams.get("workspace_root")).toBe(owner);
  expect(read.searchParams.get("session_id")).toBe(executionOwner.sessionId);
  expect(read.searchParams.get("conversation_id")).toBe("");
  expect(registeredSettingsEditorOwner(`${projectOwner}/ordinary.txt`, owner)).toBeUndefined();
  expect((await fsReadFileInfo(`${projectOwner}/ordinary.txt`))?.content).toBe("ordinary native file");
  expect(nativeRead).toHaveBeenCalledExactlyOnceWith(`${projectOwner}/ordinary.txt`);
});
