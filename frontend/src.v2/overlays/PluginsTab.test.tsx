/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { pushToast } from "./ToastContainer";
import { PluginsTab, PluginSettingsToolbar } from "./PluginsTab";
import { useAppStore } from "../stores";

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
  pluginAssetResourceUrlWithToken: (_path: string, variant: string) => `https://test.local/icon?variant=${variant}`,
}));

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(),
}));

vi.mock("../desktop/runtime", () => ({
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
    await screen.findByText("还没有本地插件");

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
    const install = await screen.findByRole("button", { name: "安装插件 review@team-b" });
    await waitFor(() => expect((install as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(install);
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("http://test.local/api/plugins/install", expect.objectContaining({ method: "POST", body: JSON.stringify({ plugin_name: "review", marketplace: "team-b", refresh_marketplace: false }) })));
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith(expect.stringContaining("MCP unavailable"), "warning"));
    expect(pushToast).not.toHaveBeenCalledWith("已安装插件：review", "success");
    expect((screen.getByRole("button", { name: "安装插件 review@team-b" }) as HTMLButtonElement).disabled).toBe(true);
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
      const marketplace = { name: "team", status: "ready", source: {}, plugins: [] };
      return Promise.resolve(new Response(JSON.stringify({ plugins: [], marketplace, marketplaces: [marketplace] }), { status: 200 }));
    }));
    const toolbar = document.createElement("div");
    const { container } = render(<PluginsTab catalog toolbarHost={toolbar} />);
    container.appendChild(toolbar);
    await screen.findByText("还没有本地插件");
    fireEvent.click(screen.getByRole("button", { name: "添加", exact: true }));
    fireEvent.click(screen.getByRole("menuitem", { name: "添加插件市场", exact: true }));
    fireEvent.change(screen.getByRole("textbox", { name: "来源", exact: true }), { target: { value: "org/repo" } });
    fireEvent.click(screen.getByRole("button", { name: "添加市场", exact: true }));
    await screen.findByRole("button", { name: "同步来源 team" });
    await act(async () => resolveOld(new Response(JSON.stringify({ marketplaces: [] }), { status: 200 })));
    expect(screen.getByRole("button", { name: "同步来源 team" })).toBeTruthy();
  });

  it("opens the complete catalog description before installing to the actual supported scope", async () => {
    const plugin = { id: "build@team", name: "build", description: "完整说明：用于项目构建和分析。", interface: { displayName: "构建助手", developerName: "Team" } };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => Promise.resolve(new Response(JSON.stringify(String(input).endsWith("/marketplaces") ? { marketplaces: [{ name: "team", status: "ready", source: { repo: "org/tools" }, plugins: [plugin] }] } : { plugins: [] }), { status: 200 }))));
    render(<PluginsTab catalog />);
    fireEvent.click(await screen.findByRole("button", { name: "查看插件详情 build@team" }));
    const detail = screen.getByRole("region", { name: "插件详情" });
    expect(detail.textContent).toContain("完整说明：用于项目构建和分析。");
    expect(within(detail).getByRole("region", { name: "信息" }).textContent).toContain("Team");
    expect(screen.getByRole("button", { name: "安装到用户级配置" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /安装到项目/ })).toBeNull();
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

});
