/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { pushToast } from "./ToastContainer";
import { PluginsTab, PluginSettingsToolbar } from "./PluginsTab";
import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";
import type { DraftModelSelection } from "../protocol/api";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
});

vi.mock("./ToastContainer", () => ({
  pushToast: vi.fn(),
}));

vi.mock("../protocol/api", () => ({
  apiBase: () => "http://test.local",
  authHeaders: (headers?: HeadersInit) => headers ?? {},
  fetchWithTimeout: (url: string, init?: RequestInit) => new URL(url).pathname === "/api/skills/settings"
    ? Promise.resolve(new Response(JSON.stringify({ skills: [], plugin_skills: [], available_skills: [], workspace_root: new URL(url).searchParams.get("workspace_root") || "" }))) : fetch(url, init),
  errorMessageFromResponseText: (text: string, fallback: string) => text || fallback,
  LONG_HTTP_TIMEOUT_MS: 60_000,
  fetchDraftModelSelection: async () => ({ current_model: "", available_models: [], models_source: "configured",
    available_permission_modes: ["confirm"], config_version: "plugin-test", config_cwd: "",
    feature_flags: {}, composer_commands: [] } satisfies DraftModelSelection),
  pluginAssetResourceUrlWithToken: (_path: string, variant: string) => `https://test.local/icon?variant=${variant}`,
}));

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(),
}));

vi.mock("../desktop/runtime", () => ({
  desktop: () => undefined,
  isDesktop: () => false,
  pickDirectory: vi.fn(),
}));
vi.mock("../chat/messages/MarkdownRenderer", () => ({ MarkdownRenderer: ({ content }: { content: string }) => <p>{content}</p> }));

describe("PluginsTab loading", () => {
  beforeEach(() => {
    useAppStore.setState({ workingDirectory: "", mcpServers: [] });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("boom")));
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.unstubAllGlobals();
  });

  it("shows initial load errors inline without noisy startup toasts", async () => {
    render(<PluginsTab />);

    expect(await screen.findByText("插件设置加载失败。")).toBeTruthy();
    expect(screen.getAllByText("boom").length).toBe(2);
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("keeps manual retries visible with a toast", async () => {
    render(<PluginsTab />);
    await screen.findByText("插件设置加载失败。");
    vi.mocked(pushToast).mockClear();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));

    await waitFor(() => expect(pushToast).toHaveBeenCalledWith("插件设置加载失败：boom", "error"));
  });

  it("keeps the empty catalog free of an unsupported developer/import panel", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ plugins: [], marketplaces: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }))));
    render(<PluginsTab catalog />);
    await screen.findByText("没有找到插件");

    expect(screen.queryByRole("button", { name: "填写插件路径" })).toBeNull();
    expect(screen.queryByRole("button", { name: "选择插件文件夹" })).toBeNull();
    expect(screen.queryByRole("textbox", { name: "插件文件夹或安装包路径" })).toBeNull();
    expect(screen.queryByText("插件开发", { exact: true })).toBeNull();
  });

  it("installs by canonical marketplace identity and reports runtime refresh failure", async () => {
    const response = (payload: unknown) => new Response(JSON.stringify(payload), { status: 200 });
    const installed = { id: "review@team-a", name: "review", path: "/plugins/a", enabled: true };
    let installedPlugins = [installed];
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/marketplaces")) return Promise.resolve(response({ marketplaces: [{ name: "team-b", status: "ready", source: { source: "github", repo: "org/b" }, plugins: [{ id: "review@team-b", name: "review", description: "Team B review" }] }] }));
      if (url.endsWith("/install")) {
        installedPlugins = [installed, { ...installed, id: "review@team-b", path: "/plugins/b" }];
        return Promise.resolve(response({ plugins: installedPlugins, runtime_refresh: { ok: false, warnings: ["MCP unavailable"] } }));
      }
      return Promise.resolve(response({ plugins: installedPlugins }));
    }));
    render(<PluginsTab catalog />);
    fireEvent.click(screen.getByRole("tab", { name: "个人", exact: true }));
    const install = await screen.findByRole("button", { name: "安装插件 review@team-b" });
    await waitFor(() => expect((install as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(install);
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("http://test.local/api/plugins/install", expect.objectContaining({ method: "POST", body: JSON.stringify({ plugin_name: "review", marketplace: "team-b", refresh_marketplace: false }) })));
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith(expect.stringContaining("MCP unavailable"), "warning"));
    expect(pushToast).not.toHaveBeenCalledWith("已安装插件：review", "success");
    expect((screen.getByRole("button", { name: "安装插件 review@team-b" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it.each(["install", "state"] as const)("keeps a committed %s distinct from a failed inventory refresh and retries only the read", async operation => {
    const plugin = { id: "review@team", name: "review", path: "/plugins/review", enabled: false };
    const marketplace = { name: "team", status: "ready", source: { source: "github", repo: "org/plugins" },
      plugins: [{ id: plugin.id, name: plugin.name }] };
    let committed = false;
    let failInventory = true;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname === "/api/plugins/marketplaces") return Response.json({ marketplaces: [marketplace] });
      if (init?.method === "POST" || init?.method === "PUT") {
        committed = true;
        return Response.json({ plugins: [{ ...plugin, enabled: true }], runtime_refresh: { ok: false, errors: ["MCP unavailable"] } });
      }
      if (committed && failInventory) return new Response("inventory refresh failed", { status: 500 });
      return Response.json({ plugins: committed ? [{ ...plugin, enabled: true }] : operation === "state" ? [plugin] : [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<PluginsTab catalog={operation === "install"} />);
    if (operation === "install") fireEvent.click(screen.getByRole("tab", { name: "个人", exact: true }));
    const action = await screen.findByRole(operation === "install" ? "button" : "switch", {
      name: operation === "install" ? `安装插件 ${plugin.id}` : `启用插件 ${plugin.id}`,
    });
    await waitFor(() => expect(action).toHaveProperty("disabled", false));
    fireEvent.click(action);
    const failurePrefix = operation === "install" ? "插件已安装，但状态刷新失败" : "插件设置已保存，但状态刷新失败";
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith(`${failurePrefix}：inventory refresh failed`, "warning"));
    expect(pushToast).toHaveBeenCalledWith(expect.stringContaining("MCP unavailable"), "warning");
    expect(pushToast).not.toHaveBeenCalledWith(expect.anything(), "error");
    expect(sendClientCommand).toHaveBeenCalledWith({ type: "skills.list" }, { silent: true });
    expect(sendClientCommand).toHaveBeenCalledWith({ type: "mcp.list" }, { silent: true });
    expect(sendClientCommand).toHaveBeenCalledWith(expect.objectContaining({ type: "runtime.capabilities.inspect" }), { silent: true });
    expect(await screen.findByText("插件设置加载失败。")).toBeTruthy();

    failInventory = false;
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    if (operation === "install") await waitFor(() => expect(screen.getByRole("button", { name: `安装插件 ${plugin.id}` })).toHaveProperty("disabled", true));
    else await waitFor(() => expect(screen.getByRole("switch", { name: `停用插件 ${plugin.id}` }).getAttribute("aria-checked")).toBe("true"));
    expect(screen.queryByText("插件设置加载失败。")).toBeNull();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST" || init?.method === "PUT")).toHaveLength(1);
  });

  it("keeps an uncommitted install rejection as a failed mutation", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") return new Response("install rejected", { status: 409 });
      return Response.json(new URL(String(input)).pathname.endsWith("/marketplaces") ? {
        marketplaces: [{ name: "team", status: "ready", source: { source: "github", repo: "org/plugins" }, plugins: [{ id: "review@team", name: "review" }] }],
      } : { plugins: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<PluginsTab catalog />);
    fireEvent.click(screen.getByRole("tab", { name: "个人", exact: true }));
    const install = await screen.findByRole("button", { name: "安装插件 review@team" });
    await waitFor(() => expect(install).toHaveProperty("disabled", false));
    fireEvent.click(install);
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith("插件安装失败：install rejected", "error"));
    expect(pushToast).not.toHaveBeenCalledWith(expect.stringContaining("插件已安装"), "warning");
    expect(sendClientCommand).not.toHaveBeenCalled();
  });

  it.each((["install", "state", "remove"] as const).flatMap(operation =>
    (["old-before", "old-after", "return-c"] as const).map(order => ({ operation, order }))))(
    "rebuilds the current global inventory for $operation with $order without repeating the write", async ({ operation, order }) => {
    const rootA = "C:/project-a", rootB = "C:/project-b", rootC = "C:/project-c";
    const pluginA = { id: "review@team", name: "review", path: "/plugins/review", enabled: operation !== "state", managed: true };
    const pluginB = { id: "build@team", name: "build", path: "/plugins/build", enabled: true };
    const pluginC = { ...pluginB, id: "notes@team", name: "notes", path: "/plugins/notes" };
    const globalAfter = operation === "remove" ? [] : [{ ...pluginA, enabled: true }];
    const bAfter = operation === "remove" ? [pluginB] : [pluginB, { ...pluginA, enabled: operation !== "install", policy_managed: operation === "install" }];
    const oldInventory = operation === "install" ? [] : [pluginA];
    const pending = () => { let resolve!: (value: Response) => void; const promise = new Promise<Response>(done => { resolve = done; }); return { promise, resolve }; };
    const mutation = pending(), oldB = pending(), freshB = pending(), oldC = pending(), freshC = pending();
    let aReads = 0, bReads = 0, cReads = 0, writes = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/marketplaces")) return Promise.resolve(Response.json({ marketplaces: [
        { name: "team", status: "ready", source: { source: "github", repo: "org/plugins" }, plugins: [{ id: pluginA.id, name: pluginA.name }] },
      ] }));
      if (["POST", "PUT", "DELETE"].includes(init?.method || "")) { writes += 1; return mutation.promise; }
      if (url.searchParams.get("workspace_root") === rootB) { bReads += 1; return bReads === 1 ? oldB.promise : freshB.promise; }
      if (url.searchParams.get("workspace_root") === rootC) { cReads += 1; return cReads === 1 ? oldC.promise : freshC.promise; }
      aReads += 1;
      return Promise.resolve(Response.json({ plugins: oldInventory }));
    });
    vi.stubGlobal("fetch", fetchMock);
    useAppStore.setState({ workingDirectory: rootA });
    render(<PluginsTab catalog />);
    fireEvent.click(screen.getByRole("tab", { name: "个人", exact: true }));
    const entry = await screen.findByRole("button", { name: `${operation === "install" ? "安装插件" : "管理插件"} ${pluginA.id}` });
    await waitFor(() => expect(entry).toHaveProperty("disabled", false));
    fireEvent.click(entry);
    if (operation !== "install") {
      fireEvent.click(await screen.findByRole("button", { name: operation === "state" ? "启用插件" : "卸载插件", exact: true }));
      if (operation === "remove") fireEvent.click(within(await screen.findByRole("dialog", { name: "卸载插件" })).getByRole("button", { name: "卸载", exact: true }));
    }
    await waitFor(() => expect(writes).toBe(1));
    if (order === "return-c") {
      act(() => useAppStore.setState({ workingDirectory: rootC }));
      await waitFor(() => expect(cReads).toBe(1));
    }
    act(() => useAppStore.setState({ workingDirectory: rootB }));
    await waitFor(() => expect(bReads).toBe(1));
    if (order === "old-before") {
      await act(async () => { oldB.resolve(Response.json({ plugins: [pluginB, ...oldInventory] })); });
      await screen.findByRole("button", { name: `管理插件 ${pluginB.id}` });
    }
    await act(async () => { mutation.resolve(Response.json({ plugins: globalAfter })); });
    await waitFor(() => expect(bReads).toBe(2));
    if (order === "return-c") {
      act(() => useAppStore.setState({ workingDirectory: rootC }));
      await waitFor(() => expect(cReads).toBe(2));
      await act(async () => { freshC.resolve(Response.json({ plugins: [pluginC, ...globalAfter] })); });
      await screen.findByRole("button", { name: `管理插件 ${pluginC.id}` });
    }
    await act(async () => { freshB.resolve(Response.json({ plugins: bAfter })); });
    if (order !== "old-before") await act(async () => { oldB.resolve(Response.json({ plugins: [pluginB, ...oldInventory] })); });
    if (order === "return-c") await act(async () => { oldC.resolve(Response.json({ plugins: [pluginC, ...oldInventory] })); });
    const selected = order === "return-c" ? pluginC : pluginB;
    expect(await screen.findByRole("button", { name: `管理插件 ${selected.id}` })).toBeTruthy();
    if (operation === "remove") expect(screen.queryByRole("button", { name: `管理插件 ${pluginA.id}` })).toBeNull();
    else expect(screen.getByRole("button", { name: `管理插件 ${pluginA.id}` }).getAttribute("data-enabled"))
      .toBe(String(order === "return-c" || operation !== "install"));
    if (order === "return-c") expect(screen.queryByRole("button", { name: `管理插件 ${pluginB.id}` })).toBeNull();
    expect(screen.queryByText("插件设置加载失败。")).toBeNull();
    expect(useAppStore.getState().workingDirectory).toBe(order === "return-c" ? rootC : rootB);
    expect(aReads).toBe(1);
    expect(bReads).toBe(2);
    expect(writes).toBe(1);
  });

  it("confirms a real uninstall, retains its committed outcome on read failure, and retries without deleting twice", async () => {
    const plugin = { id: "review@team", name: "review", path: "/plugins/review", enabled: true, managed: true };
    let removed = false, failInventory = true;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/marketplaces")) return Response.json({ marketplaces: [] });
      if (init?.method === "DELETE") { removed = true; return Response.json({ plugins: [] }); }
      if (removed && failInventory) return new Response("inventory refresh failed", { status: 500 });
      return Response.json({ plugins: removed ? [] : [plugin] });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<PluginsTab />);
    fireEvent.click(await screen.findByRole("button", { name: `查看插件详情 ${plugin.id}` }));
    const remove = await screen.findByRole("button", { name: "卸载插件", exact: true });
    await waitFor(() => expect(remove).toHaveProperty("disabled", false));
    fireEvent.click(remove);
    const dialog = await screen.findByRole("dialog", { name: "卸载插件" });
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: "卸载", exact: true }));
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith("插件已卸载，但状态刷新失败：inventory refresh failed", "warning"));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(sendClientCommand).toHaveBeenCalledWith({ type: "mcp.list" }, { silent: true });
    failInventory = false;
    fireEvent.click(screen.getByRole("button", { name: "返回插件列表" }));
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: `查看插件详情 ${plugin.id}` })).toBeNull());
    expect(screen.queryByText("插件设置加载失败。")).toBeNull();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(1);
  });

  it("adds a marketplace with its ref and sparse paths through the source dialog", async () => {
    let registered = false;
    const marketplace = { name: "team", status: "ready", source: { source: "github", repo: "org/plugins" }, plugins: [] };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/marketplaces") && init?.method === "POST") registered = true;
      return Promise.resolve(new Response(JSON.stringify({ plugins: [], marketplace, marketplaces: registered ? [marketplace] : [] }), { status: 200 }));
    }));
    render(<PluginsTab />);
    await screen.findByText("还没有本地插件");
    fireEvent.click(screen.getByRole("button", { name: "添加", exact: true }));
    fireEvent.click(screen.getByRole("menuitem", { name: "添加插件市场", exact: true }));
    fireEvent.change(screen.getByRole("textbox", { name: "来源", exact: true }), { target: { value: " org/plugins " } });
    fireEvent.change(screen.getByRole("textbox", { name: "Git 引用" }), { target: { value: " release " } });
    fireEvent.change(screen.getByRole("textbox", { name: "稀疏路径" }), { target: { value: " .agents/plugins, plugins/review\nplugins/build " } });
    fireEvent.click(screen.getByRole("button", { name: "添加市场", exact: true }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("http://test.local/api/plugins/marketplaces", expect.objectContaining({ method: "POST", body: JSON.stringify({ source: "org/plugins", ref_name: "release", sparse_paths: [".agents/plugins", "plugins/review", "plugins/build"] }) })));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(pushToast).toHaveBeenCalledWith("已添加插件市场：team", "success");
  });

  it("reports the actual activation outcome when dependencies keep a plugin disabled", async () => {
    const plugin = { id: "review@team", name: "review", path: "/plugins/review", enabled: false, load_errors: ["missing dependency: parser@team"] };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => Promise.resolve(new Response(JSON.stringify(String(input).endsWith("/marketplaces") ? { marketplaces: [] } : { plugins: [plugin], runtime_refresh: { ok: true } }), { status: 200 }))));
    render(<PluginsTab />);
    const toggle = await screen.findByRole("switch", { name: "启用插件 review@team" });
    await waitFor(() => expect((toggle as HTMLInputElement).disabled).toBe(false));
    fireEvent.click(toggle);
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith(expect.stringContaining("missing dependency"), "warning"));
    expect(pushToast).not.toHaveBeenCalledWith("已启用插件：review", "success");
  });

  it("opens management from the installed icon strip without inventing activation", async () => {
    const plugin = { id: "review@team", name: "review", path: "/plugins/review", enabled: false, dependencies: ["parser@team"], load_errors: ["dependency-unsatisfied"] };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => Promise.resolve(new Response(JSON.stringify(String(input).endsWith("/marketplaces") ? { marketplaces: [] } : { plugins: [plugin] }), { status: 200 }))));
    render(<PluginsTab catalog />);
    const icon = await screen.findByRole("button", { name: "管理插件 review@team" });
    expect(screen.queryByRole("switch", { name: "启用插件 review@team" })).toBeNull();
    await waitFor(() => expect((icon as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(icon);
    expect(screen.getByRole("region", { name: "插件详情" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "启用插件", exact: true })).toBeTruthy();
    expect(screen.getByText("插件依赖尚未满足：parser@team")).toBeTruthy();
    expect(vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false);
  });

  it("changes to the declared dark logo when the theme changes", async () => {
    useAppStore.setState({ resolvedTheme: "light" });
    const plugin = { id: "github-helper@team", name: "github-helper", path: "/plugins/review", enabled: true, iconVariant: "logo", iconVariants: ["logo", "logo-dark"] };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => Promise.resolve(new Response(JSON.stringify(String(input).endsWith("/marketplaces") ? { marketplaces: [] } : { plugins: [plugin] }), { status: 200 }))));
    const { container } = render(<PluginsTab />);
    await screen.findByRole("switch", { name: "停用插件 github-helper@team" });
    expect(container.querySelector("img")?.getAttribute("src")).toBe("https://test.local/icon?variant=logo");
    act(() => useAppStore.setState({ resolvedTheme: "dark" }));
    expect(container.querySelector("img")?.getAttribute("src")).toBe("https://test.local/icon?variant=logo-dark");
  });

  it("does not let an older source snapshot overwrite a completed source addition", async () => {
    let resolveOld!: (response: Response) => void;
    let reads = 0;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/marketplaces") && !init?.method && reads++ === 0) return new Promise<Response>((resolve) => { resolveOld = resolve; });
      const marketplace = { name: "team", directory: "personal", status: "ready", source: {}, plugins: [{ id: "actual@team", name: "actual" }] };
      return Promise.resolve(new Response(JSON.stringify({ plugins: [], marketplace, marketplaces: [marketplace] }), { status: 200 }));
    }));
    const toolbar = document.createElement("div");
    const { container } = render(<PluginsTab catalog toolbarHost={toolbar} />);
    container.appendChild(toolbar);
    expect(screen.getByRole("tab", { name: "公开", exact: true }).getAttribute("aria-selected")).toBe("true");
    await waitFor(() => expect((screen.getByRole("button", { name: "添加", exact: true }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "添加", exact: true }));
    fireEvent.click(screen.getByRole("menuitem", { name: "添加插件市场", exact: true }));
    fireEvent.change(screen.getByRole("textbox", { name: "来源", exact: true }), { target: { value: "org/repo" } });
    fireEvent.click(screen.getByRole("button", { name: "添加市场", exact: true }));
    await screen.findByRole("button", { name: "查看插件详情 actual@team" });
    await act(async () => resolveOld(new Response(JSON.stringify({ marketplaces: [] }), { status: 200 })));
    expect(screen.getByRole("button", { name: "查看插件详情 actual@team" })).toBeTruthy();
  });

  it("opens the complete catalog description before installing to the actual supported scope", async () => {
    const plugin = { id: "build@team", name: "build", description: "完整说明：用于项目构建和分析。", interface: { displayName: "构建助手", developerName: "Team" } };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => Promise.resolve(new Response(JSON.stringify(String(input).endsWith("/marketplaces") ? { marketplaces: [{ name: "team", status: "ready", source: { repo: "org/tools" }, plugins: [plugin] }] } : { plugins: [] }), { status: 200 }))));
    render(<PluginsTab catalog />);
    fireEvent.click(screen.getByRole("tab", { name: "个人", exact: true }));
    fireEvent.click(await screen.findByRole("button", { name: "查看插件详情 build@team" }));
    const detail = screen.getByRole("region", { name: "插件详情" });
    expect(detail.textContent).toContain("完整说明：用于项目构建和分析。");
    expect(within(detail).getByRole("region", { name: "信息" }).textContent).toContain("Team");
    expect(screen.getByRole("button", { name: "安装插件", exact: true })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /安装到项目/ })).toBeNull();
  });

  it("opens an uninstalled requested plugin from its actual marketplace record", async () => {
    const plugin = { id: "computer-use@openai-bundled", name: "computer-use", description: "Actual computer control package",
      interface: { displayName: "Computer Use" }, longDescription: "Original marketplace package detail" };
    useAppStore.setState({ skillsMarketplaceSelectedPluginId: plugin.id });
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => Promise.resolve(new Response(JSON.stringify(String(input).endsWith("/marketplaces")
      ? { marketplaces: [{ name: "openai-bundled", status: "ready", source: { source: "local", path: "C:/actual-marketplace" }, plugins: [plugin] }] }
      : { plugins: [] }), { status: 200 }))));
    render(<PluginsTab catalog />);
    const detail = await screen.findByRole("region", { name: "插件详情" });
    expect(within(detail).getByText("Original marketplace package detail")).toBeTruthy();
    expect(within(detail).getByRole("button", { name: "安装插件", exact: true })).toBeTruthy();
    expect(useAppStore.getState().skillsMarketplaceSelectedPluginId).toBeNull();
    expect(vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === "POST" || init?.method === "PUT")).toBe(false);
  });

  it("groups the public directory by actual categories and keeps configured marketplaces in Personal without refetching", async () => {
    const marketplaces = [
      { name: "public-a", directory: "public", isBuiltIn: true, status: "ready", source: {}, plugins: [
        { id: "computer-use@minicode-bundled", name: "computer-use", interface: { displayName: "Computer Use", category: "Developer Tools", shortDescription: "Declared local computer tools" } },
      ] },
      { name: "public-b", directory: "public", isBuiltIn: true, status: "ready", source: {}, plugins: [
        { id: "build@public-b", name: "build", interface: { displayName: "Build", category: "Developer Tools", shortDescription: "Real build workflow" } },
        { id: "design@public-b", name: "design", interface: { displayName: "Design", category: "Creativity" } },
      ] },
      { name: "my-market", directory: "personal", status: "ready", source: {}, plugins: [{ id: "local@my-market", name: "local", interface: { displayName: "Personal local" } }] },
    ];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => Response.json(String(input).endsWith("/marketplaces") ? { marketplaces } : { plugins: [] })));
    render(<PluginsTab catalog />);
    const tools = await screen.findByRole("region", { name: "Developer Tools" });
    expect(within(tools).getAllByRole("button", { name: /查看插件详情/ })).toHaveLength(2);
    expect(screen.getAllByRole("heading", { name: "Developer Tools" })).toHaveLength(1);
    expect(screen.queryByRole("heading", { name: "public-a" })).toBeNull();
    expect(screen.queryByRole("button", { name: "查看插件详情 local@my-market" })).toBeNull();
    await waitFor(() => expect(screen.getByRole("button", { name: "刷新插件" })).toHaveProperty("disabled", false));
    const reads = vi.mocked(fetch).mock.calls.length;
    fireEvent.change(screen.getByRole("textbox", { name: "搜索插件" }), { target: { value: "Real build" } });
    expect(screen.getByRole("button", { name: "查看插件详情 build@public-b" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "查看插件详情 computer-use@minicode-bundled" })).toBeNull();
    fireEvent.change(screen.getByRole("textbox", { name: "搜索插件" }), { target: { value: "" } });
    const publicTab = screen.getByRole("tab", { name: "公开", exact: true }); publicTab.focus();
    fireEvent.keyDown(publicTab, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "个人", exact: true }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("button", { name: "查看插件详情 local@my-market" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Developer Tools" })).toBeNull();
    expect(vi.mocked(fetch).mock.calls.length).toBe(reads);
  });

  it("uses declared catalog logos and component presence without inventing unloaded component counts", async () => {
    const entry = { id: "tools@public", name: "tools", iconUrl: "https://assets.example/actual-logo.png", iconDarkUrl: "https://assets.example/actual-dark.png",
      skills: "./skills", mcpServers: "./.mcp.json", interface: { displayName: "Actual tools", category: "Developer Tools" },
      installation: { available: false, reason: "This package is unavailable on the current platform" } };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => Response.json(String(input).endsWith("/marketplaces")
      ? { marketplaces: [{ name: "public", directory: "public", status: "ready", isBuiltIn: true, source: {}, plugins: [entry] }] } : { plugins: [] })));
    useAppStore.setState({ resolvedTheme: "light" });
    const view = render(<PluginsTab catalog />);
    const install = await screen.findByRole("button", { name: "安装插件 tools@public" });
    expect(install).toHaveProperty("disabled", true);
    expect(install.title).toBe(entry.installation.reason);
    expect(view.container.querySelector("img")?.src).toBe(entry.iconUrl);
    act(() => useAppStore.setState({ resolvedTheme: "dark" }));
    expect(view.container.querySelector("img")?.src).toBe(entry.iconDarkUrl);
    fireEvent.click(screen.getByRole("button", { name: "查看插件详情 tools@public" }));
    const detail = screen.getByRole("region", { name: "插件详情" });
    expect(within(detail).getByText("MCP 服务器", { exact: true })).toBeTruthy();
    expect(within(detail).getByText("技能", { exact: true })).toBeTruthy();
    expect(within(detail).getByRole("button", { name: "安装插件", exact: true })).toHaveProperty("disabled", true);
    expect(within(detail).getByText(entry.installation.reason)).toBeTruthy();
    expect(vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it("shows public installation progress once and waits for the real scoped inventory before marking installed", async () => {
    const plugin = { id: "review@public", name: "review", interface: { displayName: "Public Review", category: "Developer Tools" } };
    const installed = { id: plugin.id, name: plugin.name, path: "C:/plugins/review", enabled: true };
    let commit!: (value: Response) => void, finishInventory!: (value: Response) => void;
    let committed = false, writes = 0;
    useAppStore.setState({ conversationId: "keep-owner", draft: "Keep unsent message" });
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/marketplaces")) return Promise.resolve(Response.json({ marketplaces: [
        { name: "public", directory: "public", isBuiltIn: true, status: "ready", source: {}, plugins: [plugin] },
      ] }));
      if (init?.method === "POST") { writes++; return new Promise<Response>(resolve => { commit = resolve; }); }
      if (committed) return new Promise<Response>(resolve => { finishInventory = resolve; });
      return Promise.resolve(Response.json({ plugins: [] }));
    }));
    render(<PluginsTab catalog />);
    const install = await screen.findByRole("button", { name: "安装插件 review@public" });
    await waitFor(() => expect(install).toHaveProperty("disabled", false));
    fireEvent.click(install); fireEvent.click(install);
    expect(writes).toBe(1);
    expect(install.querySelector(".settings-spin")).toBeTruthy();
    committed = true; await act(async () => commit(Response.json({ plugins: [installed] })));
    await waitFor(() => expect(finishInventory).toBeTypeOf("function"));
    expect(screen.queryByRole("button", { name: "管理插件 review@public" })).toBeNull();
    await act(async () => finishInventory(Response.json({ plugins: [installed] })));
    await screen.findByRole("button", { name: "管理插件 review@public" });
    expect(install).toHaveProperty("disabled", true);
    expect(install.querySelector(".settings-spin")).toBeNull();
    expect(install.title).toBe("已安装");
    expect(useAppStore.getState()).toMatchObject({ conversationId: "keep-owner", draft: "Keep unsent message" });
    expect(fetch).toHaveBeenCalledWith("http://test.local/api/plugins/install", expect.objectContaining({ body: JSON.stringify({ plugin_name: "review", marketplace: "public", refresh_marketplace: false }) }));
  });

  it("opens the existing plugin settings and keeps Add separate from filtering", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => Response.json(String(input).endsWith("/marketplaces") ? { marketplaces: [] } : { plugins: [] })));
    render(<PluginsTab catalog />);
    await screen.findByText("没有找到插件");
    fireEvent.change(screen.getByRole("textbox", { name: "搜索插件" }), { target: { value: "kept query" } });
    fireEvent.click(screen.getByRole("button", { name: "添加", exact: true }));
    expect(screen.getByRole("menuitem", { name: "添加插件市场" })).toBeTruthy();
    expect((screen.getByRole("textbox", { name: "搜索插件" }) as HTMLInputElement).value).toBe("kept query");
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: "插件设置", exact: true }));
    expect(useAppStore.getState()).toMatchObject({ settingsOpen: true, settingsTab: "plugins" });
  });

  it("connects installed MCP components to their real login state and configuration", async () => {
    useAppStore.setState({ mcpServers: [{ name: "tool_server", status: "connected", tools: 1 }, { name: "plugin:tools@local:tool_server", status: "error", phase: "auth_required", lastError: "login expired", tools: 0 }] });
    const plugin = { id: "tools@local", name: "tools", displayName: "工具包", path: "C:/plugins/tools", enabled: true, mcp_server_count: 1, mcp_server_names: ["tool_server"] };
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify({ plugins: [plugin], marketplaces: [] }), { status: 200 }))));
    render(<PluginsTab />);
    fireEvent.click(await screen.findByRole("button", { name: "查看插件详情 tools@local" }));
    expect(screen.getByText("需要连接或登录")).toBeTruthy();
    expect(screen.getByText(/tool_server · 需要登录/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "配置 MCP 服务" }));
    expect(useAppStore.getState().settingsTab).toBe("connectors");
  });

  it("keeps a declared MCP component unknown until its actual status is received", async () => {
    useAppStore.setState({ mcpServers: [{ name: "later-server", status: "connected", tools: 1 }] });
    const plugin = { id: "pending@local", name: "pending", path: "C:/plugins/pending", enabled: true, mcp_server_count: 1, mcp_server_names: ["later-server"] };
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify({ plugins: [plugin], marketplaces: [] }), { status: 200 }))));
    render(<PluginsTab />);
    fireEvent.click(await screen.findByRole("button", { name: "查看插件详情 pending@local" }));
    expect(screen.getByText("组件状态待同步")).toBeTruthy();
    expect(screen.getByText("later-server · 状态未知（待同步）")).toBeTruthy();
    expect(vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === "POST" || init?.method === "PUT")).toBe(false);
    act(() => useAppStore.setState({ mcpServers: [{ name: "plugin:pending@local:later-server", status: "connected", tools: 1 }] }));
    expect(screen.queryByText("组件状态待同步")).toBeNull();
    expect(screen.getByText("later-server · 已连接")).toBeTruthy();
  });

  it("loads the current workspace and ignores a late response from the previous workspace", async () => {
    let finishOld!: (response: Response) => void;
    useAppStore.setState({ workingDirectory: "C:/A" });
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/marketplaces")) return Promise.resolve(new Response(JSON.stringify({ marketplaces: [] })));
      if (url.searchParams.get("workspace_root") === "C:/A") return new Promise<Response>((resolve) => { finishOld = resolve; });
      return Promise.resolve(new Response(JSON.stringify({ plugins: [{ id: "current@local", name: "current", path: "C:/plugins/current", enabled: true }] })));
    }));
    render(<PluginsTab />);
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("http://test.local/api/plugins?workspace_root=C%3A%2FA", expect.anything()));
    act(() => useAppStore.setState({ workingDirectory: "C:/B" }));
    await screen.findByRole("button", { name: "查看插件详情 current@local" });
    await act(async () => finishOld(new Response(JSON.stringify({ plugins: [{ id: "old@local", name: "old", path: "C:/plugins/old", enabled: true }] }))));
    expect(screen.queryByRole("button", { name: "查看插件详情 old@local" })).toBeNull();
    expect(screen.getByRole("button", { name: "查看插件详情 current@local" })).toBeTruthy();
  });

  it("re-reads effective workspace state after saving user plugin state", async () => {
    const plugin = { id: "tools@local", name: "tools", path: "C:/plugins/tools", enabled: false };
    useAppStore.setState({ workingDirectory: "C:/project" });
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/marketplaces")) return Promise.resolve(new Response(JSON.stringify({ marketplaces: [] })));
      return Promise.resolve(new Response(JSON.stringify({ plugins: [{ ...plugin, enabled: init?.method === "PUT" }], runtime_refresh: { ok: true } })));
    }));
    render(<PluginsTab />);
    const toggle = await screen.findByRole("switch", { name: "启用插件 tools@local" });
    await waitFor(() => expect(toggle).toHaveProperty("disabled", false));
    fireEvent.click(toggle);
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith(expect.stringContaining("尚未启用"), "warning"));
    expect(toggle).toHaveProperty("checked", false);
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    expect(vi.mocked(fetch).mock.calls.filter(([input]) => String(input) === "http://test.local/api/plugins?workspace_root=C%3A%2Fproject")).toHaveLength(2);
  });

  it("keeps the shared toolbar limited to source actions and routes each enabled menu item", () => {
    const marketplace = vi.fn();
    const mcp = vi.fn();
    render(<PluginSettingsToolbar onAddMarketplace={marketplace} onAddMcpServer={mcp} />);
    expect(screen.getAllByRole("button")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "添加", exact: true }));
    expect(screen.getAllByRole("menuitem")).toHaveLength(3);
    expect(screen.getByRole("menuitem", { name: "创建插件" })).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByRole("menuitem", { name: "添加插件市场" }));
    expect(marketplace).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "添加", exact: true }));
    fireEvent.click(screen.getByRole("menuitem", { name: "添加 MCP 服务器" }));
    expect(mcp).toHaveBeenCalledOnce();
  });

  it("retains marketplace fields and shows the actual add error", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => Promise.resolve(new Response(
      init?.method === "POST" ? "marketplace manifest not found" : JSON.stringify({ plugins: [], marketplaces: [] }),
      { status: init?.method === "POST" ? 400 : 200 },
    ))));
    render(<PluginsTab managedToolbar marketplaceRequest={1} />);
    fireEvent.change(await screen.findByRole("textbox", { name: "来源", exact: true }), { target: { value: "C:/plugins/invalid" } });
    fireEvent.click(screen.getByRole("button", { name: "添加市场", exact: true }));
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "marketplace manifest not found");
    expect(screen.getByRole("textbox", { name: "来源", exact: true })).toHaveProperty("value", "C:/plugins/invalid");
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(pushToast).not.toHaveBeenCalledWith(expect.anything(), "success");
  });

  it("uses the shared search without rendering a second toolbar or search box", async () => {
    const plugin = { id: "review@team", name: "review", path: "/plugins/review", enabled: true };
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify({ plugins: [plugin], marketplaces: [] })))));
    const changed = vi.fn();
    const { rerender } = render(<PluginsTab managedToolbar search="review" onPluginsChange={changed} />);
    await screen.findByRole("button", { name: "查看插件详情 review@team" });
    expect(changed).toHaveBeenCalledWith(1);
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "添加", exact: true })).toBeNull();
    rerender(<PluginsTab managedToolbar search="missing" onPluginsChange={changed} />);
    expect(screen.queryByRole("button", { name: "查看插件详情 review@team" })).toBeNull();
  });

  it.each(["old-before", "old-after"])("refreshes another mounted settings consumer after a catalog commit (%s)", async order => {
    const installed = { id: "review@team", name: "review", path: "C:/plugins/review", enabled: true };
    let commit!: (value: Response) => void, finishOld!: (value: Response) => void;
    let committed = false, reads = 0, writes = 0;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/marketplaces")) return Promise.resolve(Response.json({ marketplaces: [
        { name: "team", status: "ready", source: { source: "git", url: "https://example.test/team.git" }, plugins: [{ id: installed.id, name: installed.name }] },
      ] }));
      if (init?.method === "POST") { writes += 1; return new Promise<Response>(resolve => { commit = resolve; }); }
      reads += 1;
      if (reads === 2) return new Promise<Response>(resolve => { finishOld = resolve; });
      return Promise.resolve(Response.json({ plugins: committed ? [installed] : [] }));
    }));
    const consumers = (settings: boolean) => <>
      <section data-testid="catalog" hidden={settings}><PluginsTab catalog active={!settings} loadInventory={!settings} /></section>
      {settings && <section data-testid="settings"><PluginsTab /></section>}
    </>;
    const view = render(consumers(false));
    fireEvent.click(screen.getByRole("tab", { name: "个人", exact: true }));
    const install = await screen.findByRole("button", { name: "安装插件 review@team" });
    await waitFor(() => expect(install).toHaveProperty("disabled", false));
    fireEvent.click(install);
    await waitFor(() => expect(writes).toBe(1));
    view.rerender(consumers(true));
    await waitFor(() => expect(reads).toBe(2));
    if (order === "old-before") await act(async () => finishOld(Response.json({ plugins: [] })));
    committed = true;
    await act(async () => commit(Response.json({ plugins: [installed] })));
    const settings = within(screen.getByTestId("settings"));
    await settings.findByRole("button", { name: "查看插件详情 review@team" });
    if (order === "old-after") await act(async () => finishOld(Response.json({ plugins: [] })));
    expect(settings.getByRole("button", { name: "查看插件详情 review@team" })).toBeTruthy();
    expect(reads).toBe(4);
    expect(writes).toBe(1);
  });

});
