// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ fetch: vi.fn(), command: vi.fn(), confirm: vi.fn(), open: vi.fn(), openLink: vi.fn() }));
vi.mock("../protocol/api", async load => ({ ...await load<typeof import("../protocol/api")>(),
  apiBase: () => "http://settings.test", authHeaders: () => ({}), fetchWithTimeout: mocks.fetch }));
vi.mock("../protocol/ws-outbox", async load => ({ ...await load<typeof import("../protocol/ws-outbox")>(),
  sendClientCommand: vi.fn(() => true), sendClientCommandAwaitResult: mocks.command }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "session-test" }) }));
vi.mock("../desktop/runtime", async load => ({ ...await load<typeof import("../desktop/runtime")>(), isDesktop: () => false, openPath: mocks.open }));
vi.mock("./DialogService", () => ({ showConfirm: mocks.confirm }));
vi.mock("../lib/open-preferred-target", () => ({ openPreferredWebLink: mocks.openLink }));
import { useAppStore } from "../stores";
import { HooksTab } from "./HooksTab";
import { ConfigTab } from "./ConfigTab";
import { StorageTab } from "./StorageTab";
import { WorktreesTab } from "./WorktreesTab";
import { SidebarLeft } from "../shell/SidebarLeft";

const response = (value: unknown) => new Response(JSON.stringify(value), { status: 200 });
const config = { path: "C:/settings.json", approval_policy: "on-request", sandbox_mode: "workspace-write", network_access: false, web_search: "cached", model_verbosity: "medium", model_reasoning_summary: "auto" };
beforeEach(() => {
  vi.clearAllMocks();
  mocks.confirm.mockResolvedValue(true);
  useAppStore.setState({ settingsOpen: false, settingsTab: "general", workingDirectory: "", recentWorkspaces: [], conversations: [], isConnected: true, projectsOpen: false, projectEditTarget: null, appMode: "cowork", skillsMarketplaceOpen: false, shortcutsHelpOpen: false });
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("trusts the displayed hook hash and reloads the returned effective state", async () => {
  const hook = { key: "config:pre_tool_use:0", event: "pre_tool_use", handler_type: "command", command: "node hook.js", source: "user", source_path: "C:/settings.json", enabled: true, is_managed: false, current_hash: "changed-hash", trust_status: "modified", timeout_seconds: 30 };
  mocks.fetch.mockImplementation(async (_url, init) => response({ hooks: [{ ...hook, ...(init?.method === "PUT" ? { trust_status: "trusted" } : {}) }], warnings: [] }));
  render(<HooksTab />);
  const trust = await screen.findByRole("button", { name: "全部信任" });
  expect(screen.getByRole("switch")).toHaveProperty("disabled", true);
  fireEvent.click(trust);
  await waitFor(() => expect(screen.getByRole("switch")).toHaveProperty("disabled", false));
  const request = mocks.fetch.mock.calls.find(([, init]) => init?.method === "PUT")!;
  expect(JSON.parse(request[1].body)).toEqual({ workspace_root: "", updates: [{ key: hook.key, trusted_hash: "changed-hash" }] });
});

it("saves sandbox selection to the real configuration endpoint", async () => {
  mocks.fetch.mockImplementation(async (_url, init) => response({ ...config, ...(init?.method === "PUT" ? JSON.parse(init.body) : {}) }));
  render(<ConfigTab active />);
  fireEvent.click(await screen.findByRole("button", { name: /^沙箱模式，当前：/ }));
  fireEvent.click(screen.getByRole("option", { name: "只读" }));
  await waitFor(() => expect(screen.getByRole("switch", { name: "允许网络访问" })).toHaveProperty("disabled", true));
  const request = mocks.fetch.mock.calls.find(([, init]) => init?.method === "PUT")!;
  expect(new URL(request[0]).pathname).toBe("/api/settings/agent-config");
  expect(JSON.parse(request[1].body)).toEqual({ workspace_root: "", sandbox_mode: "read-only" });
});

it("does not replace another workspace's Config page with an earlier save response", async () => {
  let finish!: (response: Response) => void;
  mocks.fetch.mockImplementation(async (url, init) => init?.method === "PUT" ? new Promise<Response>(resolve => { finish = resolve; }) : response({ ...config, network_access: new URL(url).searchParams.get("workspace_root") === "C:/B" }));
  useAppStore.setState({ workingDirectory: "C:/A" });
  render(<ConfigTab active />);
  fireEvent.click(await screen.findByRole("switch", { name: "允许网络访问" }));
  act(() => useAppStore.setState({ workingDirectory: "C:/B" }));
  await waitFor(() => expect(screen.getByRole("switch", { name: "允许网络访问" }).getAttribute("aria-checked")).toBe("true"));
  await act(async () => finish(response({ ...config, network_access: false })));
  expect(screen.getByRole("switch", { name: "允许网络访问" }).getAttribute("aria-checked")).toBe("true");
});

it("uses Source worktree confirmation before persisting automatic deletion changes", async () => {
  let prefs = { worktree_root: "", default_worktree_root: "C:/home/worktrees", auto_cleanup_enabled: true, keep_count: 15, upstream_refresh_mode: "never" };
  mocks.fetch.mockImplementation(async (url, init) => {
    if (new URL(url).pathname.endsWith("/managed")) return response({ worktrees: [] });
    if (init?.method === "PUT") prefs = { ...prefs, ...JSON.parse(init.body) };
    return response(prefs);
  });
  mocks.confirm.mockResolvedValueOnce(false).mockResolvedValueOnce(true);
  render(<WorktreesTab />);
  fireEvent.click(await screen.findByRole("switch", { name: "自动删除旧工作树" }));
  await act(async () => {});
  expect(mocks.fetch.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(0);
  fireEvent.click(screen.getByRole("switch", { name: "自动删除旧工作树" }));
  await waitFor(() => expect(screen.getByRole("spinbutton", { name: "自动删除数量上限" })).toHaveProperty("disabled", true));
  expect(JSON.parse(mocks.fetch.mock.calls.find(([, init]) => init?.method === "PUT")![1].body)).toEqual({ auto_cleanup_enabled: false });
});

it("rechecks Storage snapshots through the authoritative delete command and reports skips separately", async () => {
  const chats = ["deleted", "changed", "failed"].map((id, index) => ({ id, title: id, updated_at: "2026-01-01", archived: true, subagent_count: index, snapshot: { revision: index } }));
  mocks.fetch.mockImplementation(async url => response(new URL(url).pathname.endsWith("/review") ? { cutoff: "2026-07-01", days: 90, archived_only: true, chats } : { path: "C:/data", total_bytes: 1024, categories: [{ name: "subagents", bytes: 1024 }] }));
  mocks.command.mockImplementation(async command => ({ level: command.conversation_id === "failed" ? "error" : command.conversation_id === "changed" ? "warning" : "success", data: { deleted: command.conversation_id === "deleted", reason: command.conversation_id === "changed" ? "storage_review_changed" : undefined } }));
  render(<StorageTab />);
  fireEvent.click(screen.getByRole("button", { name: "查看" }));
  const dialog = await screen.findByRole("dialog", { name: "查看旧聊天" });
  await within(dialog).findByText("已选聊天：3 · 子智能体：3");
  fireEvent.click(within(dialog).getByRole("button", { name: "删除所选" }));
  await screen.findByText("已删除：1。已变化并跳过：1。未完成：1。");
  expect(mocks.command.mock.calls.map(([command]) => command.storage_review)).toEqual(chats.map(chat => chat.snapshot));
  expect(mocks.command.mock.calls.every(([command]) => command.client_resource_cleanup === false)).toBe(true);
});

it("opens project creation from the actual sidebar and omits removed global launchers", () => {
  render(<SidebarLeft embedded />);
  expect(screen.queryByRole("button", { name: "Git 与工作树" })).toBeNull();
  expect(screen.queryByRole("button", { name: "探索" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "添加项目" }));
  expect(useAppStore.getState()).toMatchObject({ projectEditTarget: "create", appMode: "cowork" });
});

it("opens the Source Help submenu and routes shortcuts and the documented guide", () => {
  render(<SidebarLeft embedded />);
  fireEvent.click(screen.getByRole("button", { name: "打开个人资料菜单" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "帮助" }));
  expect(within(screen.getByRole("menu", { name: "帮助" })).getAllByRole("menuitem").map(item => item.textContent)).toEqual(["新增内容", "键盘快捷键", "使用指南"]);
  fireEvent.click(screen.getByRole("menuitem", { name: "键盘快捷键" }));
  expect(useAppStore.getState().shortcutsHelpOpen).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "打开个人资料菜单" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "帮助" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "使用指南" }));
  expect(mocks.openLink).toHaveBeenCalledExactlyOnceWith("https://learn.chatgpt.com/docs/quickstart");
});
