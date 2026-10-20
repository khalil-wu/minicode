/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ fetch: vi.fn(), toast: vi.fn(), base: "http://owner-a.test", token: "token-a" }));
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { writable: true,
  value: vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) }));
vi.mock("../protocol/api", () => ({ apiBase: () => mocks.base, authHeaders: (headers?: HeadersInit) => {
  const result = new Headers(headers); result.set("X-MiniCode-Token", mocks.token); return result;
}, fetchWithTimeout: mocks.fetch, LONG_HTTP_TIMEOUT_MS: 60_000,
errorMessageFromResponseText: (text: string, fallback: string) => text || fallback,
pluginAssetResourceUrlWithToken: () => "" }));
vi.mock("./ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
vi.mock("../desktop/runtime", () => ({ isDesktop: () => false, pickDirectory: vi.fn() }));
import { PluginsTab } from "./PluginsTab";
import { useAppStore } from "../stores";

const source = { name: "team", status: "ready", source: { source: "git", url: "https://example.test/team.git" }, plugins: [] };
const plugin = (name: string, marketplace = "team") => ({ id: `${name}@${marketplace}`, name, marketplace, path: `C:/plugins/${name}`, enabled: true });
const response = (value: unknown) => new Response(JSON.stringify(value), { status: 200 });
beforeEach(() => {
  vi.clearAllMocks(); mocks.base = "http://owner-a.test"; mocks.token = "token-a";
  useAppStore.setState({ workingDirectory: "C:/A" });
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
  mocks.fetch.mockImplementation(async url => response(String(url).endsWith("/marketplaces") ? { marketplaces: [] } : { plugins: [] }));
  const toolbar = document.createElement("div"); document.body.appendChild(toolbar);
  const view = render(<PluginsTab catalog toolbarHost={toolbar} />);
  fireEvent.click(await screen.findByRole("button", { name: "添加", exact: true }));
  expect(screen.queryByRole("menuitem", { name: "上传插件归档" })).toBeNull();
  expect(screen.queryByText("验证、导入或打包本地插件。")).toBeNull();
  view.unmount(); toolbar.remove();
});
