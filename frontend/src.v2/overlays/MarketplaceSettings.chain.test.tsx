/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { DraftModelSelection } from "../protocol/api";
const mocks = vi.hoisted(() => ({ fetch: vi.fn(), toast: vi.fn(), base: "http://owner-a.test", token: "token-a" }));
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { writable: true,
  value: vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) }));
vi.mock("../protocol/api", () => ({ apiBase: () => mocks.base, authHeaders: (headers?: HeadersInit) => {
  const result = new Headers(headers); result.set("X-MiniCode-Token", mocks.token); return result;
}, fetchWithTimeout: mocks.fetch, LONG_HTTP_TIMEOUT_MS: 60_000,
errorMessageFromResponseText: (text: string, fallback: string) => text || fallback,
pluginAssetResourceUrlWithToken: () => "",
fetchDraftModelSelection: async (root: string) => ({ current_model: "", available_models: [], models_source: "configured",
  available_permission_modes: ["confirm"], config_version: "marketplace-fixture", config_cwd: root,
  feature_flags: {}, composer_commands: [] } satisfies DraftModelSelection) }));
vi.mock("./ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
vi.mock("../desktop/runtime", () => ({ desktop: () => undefined, isDesktop: () => false, pickDirectory: vi.fn() }));
import { PluginsTab } from "./PluginsTab";
import { useAppStore, waitComposerDraftRecovery } from "../stores";

const source = { name: "team", status: "ready", source: { source: "git", url: "https://example.test/team.git" }, plugins: [] };
const plugin = (name: string, marketplace = "team") => ({ id: `${name}@${marketplace}`, name, marketplace, path: `C:/plugins/${name}`, enabled: true });
const response = (value: unknown) => new Response(JSON.stringify(value), { status: 200 });
beforeEach(async () => {
  await waitComposerDraftRecovery();
  vi.clearAllMocks(); mocks.base = "http://owner-a.test"; mocks.token = "token-a";
  useAppStore.setState({ workingDirectory: "C:/A", conversationId: "marketplace-owner", activeConversationDraftId: null });
});
afterEach(cleanup);

it("uses the existing uninstall and registry endpoints with the captured backend owner through retry", async () => {
  let finishFirst!: (response: Response) => void;
  let firstDeleted = false;
  const counts = vi.fn();
  mocks.fetch.mockImplementation(async (url: string, init?: RequestInit) => {
    const parsed = new URL(url);
    if (init?.method === "DELETE" && parsed.pathname.endsWith("/first%40team")) {
      return new Promise<Response>(resolve => { finishFirst = resolve; });
    }
    if (init?.method === "DELETE" && parsed.pathname.endsWith("/second%40team")) return response({ plugins: [], runtime_refresh: { ok: true } });
    if (init?.method === "DELETE" && parsed.pathname.endsWith("/marketplaces/team")) return response({ marketplaces: [] });
    if (parsed.pathname.endsWith("/runtime/refresh")) return response({ runtime_refresh: { ok: true } });
    if (parsed.pathname.endsWith("/marketplaces")) return response({ marketplaces: parsed.hostname === "owner-b.test" ? [source, { ...source, name: "other" }] : [source] });
    return response({ plugins: firstDeleted ? [] : [plugin("first"), plugin("second"), plugin("unrelated", "other")] });
  });
  render(<PluginsTab managedToolbar managedView="marketplaces" onMarketplacesChange={counts} />);
  fireEvent.click(await screen.findByRole("button", { name: "移除插件市场 team" }));
  fireEvent.click(await screen.findByRole("button", { name: "卸载并移除" }));
  await waitFor(() => expect(finishFirst).toBeTypeOf("function"));
  mocks.base = "http://owner-b.test"; mocks.token = "token-b";
  act(() => useAppStore.setState({ workingDirectory: "C:/B" }));
  await waitFor(() => expect(counts).toHaveBeenLastCalledWith(2));
  firstDeleted = true;
  await act(async () => finishFirst(response({ plugins: [], runtime_refresh: { ok: false, errors: ["old runtime active"] } })));
  expect(await screen.findByText("old runtime active")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  const mutations = mocks.fetch.mock.calls.filter(([, init]) => init?.method === "DELETE" || init?.method === "POST");
  expect(mutations.map(([url]) => new URL(url).pathname)).toEqual([
    "/api/plugins/first%40team", "/api/plugins/runtime/refresh", "/api/plugins/second%40team", "/api/plugins/marketplaces/team",
  ]);
  for (const [url, init] of mutations) {
    expect(new URL(url).origin).toBe("http://owner-a.test");
    expect(new Headers(init.headers).get("X-MiniCode-Token")).toBe("token-a");
  }
  expect(counts).toHaveBeenLastCalledWith(2);
});

it("does not offer a hosted archive upload without the hosted service consumer", async () => {
  mocks.fetch.mockImplementation(async url => response(new URL(String(url)).pathname.endsWith("/marketplaces") ? { marketplaces: [] } : { plugins: [] }));
  const toolbar = document.createElement("div"); document.body.appendChild(toolbar);
  const view = render(<PluginsTab catalog toolbarHost={toolbar} />);
  fireEvent.click(await screen.findByRole("button", { name: "添加", exact: true }));
  expect(screen.queryByRole("menuitem", { name: "上传插件归档" })).toBeNull();
  expect(screen.queryByText("验证、导入或打包本地插件。")).toBeNull();
  view.unmount(); toolbar.remove();
});

it.each(["old-before", "old-after", "return-c"])("rebuilds the current scoped inventory after marketplace uninstall (%s)", async order => {
  const pending = () => { let resolve!: (value: Response) => void; const promise = new Promise<Response>(done => { resolve = done; }); return { promise, resolve }; };
  const deleted = pending(), oldB = pending(), newB = pending(), oldC = pending(), newC = pending();
  const removed = plugin("removed"), keptB = plugin("kept-b", "other"), keptC = plugin("kept-c", "other");
  let bReads = 0, cReads = 0, deletes = 0;
  const counts = vi.fn();
  mocks.fetch.mockImplementation((input: string, init?: RequestInit) => {
    const url = new URL(input);
    if (init?.method === "DELETE") {
      deletes += 1;
      return url.pathname.endsWith("/marketplaces/team") ? Promise.resolve(response({ marketplaces: [] })) : deleted.promise;
    }
    if (url.pathname.endsWith("/marketplaces")) return Promise.resolve(response({ marketplaces: [source] }));
    if (url.searchParams.get("workspace_root") === "C:/B") return ++bReads === 1 ? oldB.promise : newB.promise;
    if (url.searchParams.get("workspace_root") === "C:/C") return ++cReads === 1 ? oldC.promise : newC.promise;
    return Promise.resolve(response({ plugins: [removed] }));
  });
  const view = render(<PluginsTab managedToolbar managedView="marketplaces" onPluginsChange={counts} />);
  fireEvent.click(await screen.findByRole("button", { name: "移除插件市场 team" }));
  fireEvent.click(await screen.findByRole("button", { name: "卸载并移除" }));
  await waitFor(() => expect(deletes).toBe(1));
  if (order === "return-c") {
    act(() => useAppStore.setState({ workingDirectory: "C:/C" }));
    await waitFor(() => expect(cReads).toBe(1));
  }
  act(() => useAppStore.setState({ workingDirectory: "C:/B" }));
  await waitFor(() => expect(bReads).toBe(1));
  if (order === "old-before") {
    await act(async () => oldB.resolve(response({ plugins: [removed, keptB] })));
    await waitFor(() => expect(counts).toHaveBeenLastCalledWith(2));
  }
  await act(async () => deleted.resolve(response({ plugins: [], runtime_refresh: { ok: true } })));
  await waitFor(() => expect(bReads).toBe(2));
  // Inventory reads do not hold the committed uninstall or repeat its DELETE.
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(deletes).toBe(2);
  if (order === "return-c") {
    act(() => useAppStore.setState({ workingDirectory: "C:/C" }));
    await waitFor(() => expect(cReads).toBe(2));
    await act(async () => newC.resolve(response({ plugins: [keptC] })));
    view.rerender(<PluginsTab managedToolbar managedView="plugins" onPluginsChange={counts} />);
    await screen.findByRole("button", { name: `查看插件详情 ${keptC.id}` });
    await act(async () => {
      oldC.resolve(response({ plugins: [removed] }));
      oldB.resolve(response({ plugins: [removed] }));
      newB.resolve(response({ plugins: [keptB] }));
    });
    expect(screen.queryByRole("button", { name: `查看插件详情 ${removed.id}` })).toBeNull();
    expect(screen.getByRole("button", { name: `查看插件详情 ${keptC.id}` })).toBeTruthy();
  } else {
    await act(async () => newB.resolve(response({ plugins: [keptB] })));
    view.rerender(<PluginsTab managedToolbar managedView="plugins" onPluginsChange={counts} />);
    await screen.findByRole("button", { name: `查看插件详情 ${keptB.id}` });
    if (order === "old-after") await act(async () => oldB.resolve(response({ plugins: [removed] })));
    expect(screen.queryByRole("button", { name: `查看插件详情 ${removed.id}` })).toBeNull();
    expect(screen.getByRole("button", { name: `查看插件详情 ${keptB.id}` })).toBeTruthy();
  }
  expect(counts).toHaveBeenLastCalledWith(1);
  expect(deletes).toBe(2);
});

it("keeps a committed marketplace uninstall separate from a failed inventory read and retries only the read", async () => {
  const removed = plugin("removed"), kept = plugin("kept", "other");
  let committed = false, inventoryFailed = false, deletes = 0;
  mocks.fetch.mockImplementation(async (input: string, init?: RequestInit) => {
    const url = new URL(input);
    if (init?.method === "DELETE") {
      deletes += 1;
      if (url.pathname.endsWith("/marketplaces/team")) return response({ marketplaces: [] });
      committed = true;
      return response({ plugins: [kept], runtime_refresh: { ok: true } });
    }
    if (url.pathname.endsWith("/marketplaces")) return response({ marketplaces: [source] });
    if (committed && !inventoryFailed) { inventoryFailed = true; return new Response("Inventory denied", { status: 403 }); }
    return response({ plugins: committed ? [kept] : [removed, kept] });
  });
  const view = render(<PluginsTab managedToolbar managedView="marketplaces" />);
  fireEvent.click(await screen.findByRole("button", { name: "移除插件市场 team" }));
  fireEvent.click(await screen.findByRole("button", { name: "卸载并移除" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith("插件已卸载，但状态刷新失败：Inventory denied", "warning"));
  expect(deletes).toBe(2);
  view.rerender(<PluginsTab managedToolbar managedView="plugins" />);
  fireEvent.click(within(await screen.findByRole("alert")).getByRole("button", { name: "重试" }));
  await screen.findByRole("button", { name: `查看插件详情 ${kept.id}` });
  expect(screen.queryByRole("button", { name: `查看插件详情 ${removed.id}` })).toBeNull();
  expect(deletes).toBe(2);
});

it.each([200, 403])("does not revive a removed marketplace from an older directory read (%s)", async status => {
  let finishOld!: (value: Response) => void;
  let oldSignal!: AbortSignal;
  let directoryReads = 0, deletes = 0;
  const counts = vi.fn();
  mocks.fetch.mockImplementation((input: string, init?: RequestInit) => {
    const url = new URL(input);
    if (init?.method === "DELETE") { deletes += 1; return Promise.resolve(response({ marketplaces: [] })); }
    if (url.pathname.endsWith("/marketplaces")) {
      directoryReads += 1;
      if (directoryReads === 2) {
        oldSignal = init!.signal!;
        return new Promise<Response>(resolve => { finishOld = resolve; });
      }
      return Promise.resolve(response({ marketplaces: deletes ? [] : [source] }));
    }
    return Promise.resolve(response({ plugins: [] }));
  });
  render(<PluginsTab managedToolbar managedView="marketplaces" onMarketplacesChange={counts} />);
  fireEvent.click(await screen.findByRole("button", { name: "移除插件市场 team" }));
  await screen.findByRole("button", { name: "移除", exact: true });
  act(() => useAppStore.setState({ workingDirectory: "C:/B" }));
  await waitFor(() => expect(directoryReads).toBe(2));
  fireEvent.click(screen.getByRole("button", { name: "移除", exact: true }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(oldSignal.aborted).toBe(true);
  expect(counts).toHaveBeenLastCalledWith(0);
  await act(async () => finishOld(new Response(status === 200 ? JSON.stringify({ marketplaces: [source] }) : "Old directory denied", { status })));
  expect(screen.queryByRole("button", { name: "移除插件市场 team" })).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByText("没有找到插件市场")).toBeTruthy();
  expect(counts).toHaveBeenLastCalledWith(0);
  expect(deletes).toBe(1);
  expect(directoryReads).toBe(3);
});

it("rebuilds the current project's directory after removing a source requested from another project", async () => {
  let finishRemoval!: (value: Response) => void;
  const projectSource = { ...source, source: { source: "directory", path: "C:/B" },
    isConfigured: false, canManage: false, plugins: [{ id: "project@team", name: "project" }] };
  let removed = false;
  mocks.fetch.mockImplementation((input: string, init?: RequestInit) => {
    const url = new URL(input);
    if (init?.method === "DELETE") return new Promise<Response>(resolve => { finishRemoval = resolve; });
    if (url.pathname.endsWith("/marketplaces")) {
      const current = url.searchParams.get("workspace_root");
      return Promise.resolve(response({ marketplaces: current === "C:/B" && removed ? [projectSource] : [source] }));
    }
    return Promise.resolve(response({ plugins: [] }));
  });
  render(<PluginsTab catalog />);
  fireEvent.click(screen.getByRole("tab", { name: "个人", exact: true }));
  const view = render(<PluginsTab managedToolbar managedView="marketplaces" />);
  fireEvent.click(await screen.findByRole("button", { name: "移除插件市场 team" }));
  fireEvent.click(await screen.findByRole("button", { name: "移除", exact: true }));
  await waitFor(() => expect(finishRemoval).toBeTypeOf("function"));
  act(() => useAppStore.setState({ workingDirectory: "C:/B" }));
  removed = true;
  await act(async () => finishRemoval(response({ marketplaces: [] })));
  await waitFor(() => expect(screen.getByRole("button", { name: "查看插件详情 project@team" })).toBeTruthy());
  expect(useAppStore.getState().workingDirectory).toBe("C:/B");
  view.unmount();
});
