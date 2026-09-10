/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { pushToast } from "./ToastContainer";
import { PluginsTab } from "./PluginsTab";
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
  fetchWithTimeout: (url: string, init?: RequestInit) => fetch(url, init),
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

describe("PluginsTab loading", () => {
  beforeEach(() => {
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

  it("labels the empty-state action by what it actually does", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ plugins: [], marketplaces: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }))));
    render(<PluginsTab />);
    await screen.findByText("还没有本地插件");

    fireEvent.click(screen.getByRole("button", { name: "填写插件路径" }));

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "插件文件夹或安装包路径" })));
  });

  it("installs by canonical marketplace identity and reports runtime refresh failure", async () => {
    const response = (payload: unknown) => new Response(JSON.stringify(payload), { status: 200 });
    const installed = { id: "review@team-a", name: "review", path: "/plugins/a", enabled: true };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/marketplaces")) return Promise.resolve(response({ marketplaces: [{ name: "team-b", status: "ready", source: { source: "github", repo: "org/b" }, plugins: [{ id: "review@team-b", name: "review", description: "Team B review" }] }] }));
      if (url.endsWith("/install")) return Promise.resolve(response({ plugins: [installed, { ...installed, id: "review@team-b", path: "/plugins/b" }], runtime_refresh: { ok: false, warnings: ["MCP unavailable"] } }));
      return Promise.resolve(response({ plugins: [installed] }));
    }));
    render(<PluginsTab />);
    const install = await screen.findByRole("button", { name: "安装插件 review@team-b" });
    await waitFor(() => expect((install as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(install);
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("http://test.local/api/plugins/install", expect.objectContaining({ method: "POST", body: JSON.stringify({ plugin_name: "review", marketplace: "team-b", refresh_marketplace: false }) })));
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith(expect.stringContaining("MCP unavailable"), "warning"));
    expect(pushToast).not.toHaveBeenCalledWith("已安装插件：review", "success");
    expect((screen.getByRole("button", { name: "安装插件 review@team-b" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("registers a source then synchronizes its real catalog", async () => {
    let registered = false;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/marketplaces") && init?.method === "POST") registered = true;
      return Promise.resolve(new Response(JSON.stringify({ plugins: [], marketplaces: registered ? [{ name: "team", status: "registered", source: { source: "github", repo: "org/plugins" }, plugins: [] }] : [] }), { status: 200 }));
    }));
    render(<PluginsTab />);
    await screen.findByText("还没有本地插件");
    fireEvent.change(screen.getByRole("textbox", { name: "来源名称" }), { target: { value: "team" } });
    fireEvent.change(screen.getByRole("textbox", { name: "来源地址" }), { target: { value: "org/plugins" } });
    fireEvent.click(screen.getByRole("button", { name: "添加来源", exact: true }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("http://test.local/api/plugins/marketplaces", expect.objectContaining({ method: "POST", body: JSON.stringify({ name: "team", source: { source: "github", repo: "org/plugins" } }) })));
    const sync = await screen.findByRole("button", { name: "同步来源 team" });
    await waitFor(() => expect((sync as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(sync);
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("http://test.local/api/plugins/marketplaces/team/refresh", expect.objectContaining({ method: "POST" })));
  });

  it("reports the actual activation outcome when dependencies keep a plugin disabled", async () => {
    const plugin = { id: "review@team", name: "review", path: "/plugins/review", enabled: false, load_errors: ["missing dependency: parser@team"] };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => Promise.resolve(new Response(JSON.stringify(String(input).endsWith("/marketplaces") ? { marketplaces: [] } : { plugins: [plugin], runtime_refresh: { ok: true } }), { status: 200 }))));
    render(<PluginsTab />);
    const toggle = await screen.findByRole("checkbox", { name: "启用插件 review@team" });
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
    expect(screen.queryByRole("checkbox", { name: "启用插件 review@team" })).toBeNull();
    await waitFor(() => expect((icon as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(icon);
    expect(screen.getByRole("checkbox", { name: "启用插件 review@team" })).toBeTruthy();
    expect(screen.getByText("插件依赖尚未满足，请检查：parser@team")).toBeTruthy();
  });

  it("changes to the declared dark logo when the theme changes", async () => {
    useAppStore.setState({ resolvedTheme: "light" });
    const plugin = { id: "github-helper@team", name: "github-helper", path: "/plugins/review", enabled: true, iconVariant: "logo", iconVariants: ["logo", "logo-dark"] };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => Promise.resolve(new Response(JSON.stringify(String(input).endsWith("/marketplaces") ? { marketplaces: [] } : { plugins: [plugin] }), { status: 200 }))));
    const { container } = render(<PluginsTab />);
    await screen.findByRole("checkbox", { name: "停用插件 github-helper@team" });
    expect(container.querySelector("img")?.getAttribute("src")).toBe("https://test.local/icon?variant=logo");
    act(() => useAppStore.setState({ resolvedTheme: "dark" }));
    expect(container.querySelector("img")?.getAttribute("src")).toBe("https://test.local/icon?variant=logo-dark");
  });

  it("does not let an older source snapshot overwrite a completed source addition", async () => {
    let resolveOld!: (response: Response) => void;
    let reads = 0;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/marketplaces") && !init?.method && reads++ === 0) return new Promise<Response>((resolve) => { resolveOld = resolve; });
      return Promise.resolve(new Response(JSON.stringify({ plugins: [], marketplaces: [{ name: "team", status: "registered", source: {}, plugins: [] }] }), { status: 200 }));
    }));
    render(<PluginsTab />);
    await screen.findByText("还没有本地插件");
    fireEvent.change(screen.getByRole("textbox", { name: "来源名称" }), { target: { value: "team" } });
    fireEvent.change(screen.getByRole("textbox", { name: "来源地址" }), { target: { value: "org/repo" } });
    fireEvent.click(screen.getByRole("button", { name: "添加来源", exact: true }));
    await screen.findByRole("button", { name: "同步来源 team" });
    await act(async () => resolveOld(new Response(JSON.stringify({ marketplaces: [] }), { status: 200 })));
    expect(screen.getByRole("button", { name: "同步来源 team" })).toBeTruthy();
  });

});
