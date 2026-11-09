/* @vitest-environment jsdom */
import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { useComputerUseSettings } from "../hooks/useComputerUseSettings";
import { ComputerUseSettings } from "./ComputerUseSettings";
import type { ComputerUsePluginControl, ComputerUseSettingsSnapshot } from "../protocol/computer-use-settings";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), confirm: vi.fn(), toast: vi.fn(), command: vi.fn(), session: "original-session" }));
vi.mock("../protocol/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../protocol/api")>();
  return { apiBase: () => "http://owner-backend.test", authHeaders: (headers?: HeadersInit) => ({ ...headers, Authorization: "Bearer original-runtime" }),
    fetchWithTimeout: mocks.fetch, errorMessageFromResponseText: (text: string) => text, LONG_HTTP_TIMEOUT_MS: 300_000,
    pluginAssetResourceUrlWithToken: (path: string, variant: "composer" | "logo" | "logo-dark") => actual.pluginAssetResourceUrlWithToken(path, variant, window.location.origin),
  };
});
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: mocks.session }) }));
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: null }) }));
vi.mock("./DialogService", () => ({ showConfirm: mocks.confirm }));
vi.mock("./ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: mocks.command }));

const approved = { app_id: "app-A", display_name: "Actual App A", icon_data_url: null };
const anyApp = (patch: Partial<ComputerUsePluginControl> = {}): ComputerUsePluginControl => ({
  kind: "plugin", id: "any-app", plugin_id: "computer-use@openai-bundled", plugin_name: "computer-use", marketplace: "openai-bundled",
  display_name: "Computer Use", installed: false, enabled: false, can_install: true, can_toggle: false,
  icon_url: null, plugin_path: null, icon_variant: null, icon_variants: [], load_errors: [], ...patch,
});
const snapshot = (patch: Partial<ComputerUseSettingsSnapshot> = {}): ComputerUseSettingsSnapshot => ({
  owner: { session_id: "original-session", conversation_id: "A", workspace_root: "C:/project-A" }, provider_id: "minicode-win32", selection_id: "selected-native-A",
  navigation_visible: true, available: true, status: "ready", reason: null,
  capabilities: { persistent_approvals: true, revoke_app_approval: true, persist_modes: ["session", "always"], sound_mode: false, locked_use: false },
  approved_apps: [approved], controls: [], ...patch,
});
const response = (data: ComputerUseSettingsSnapshot) => new Response(JSON.stringify(data), { status: 200 });
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; };
function Harness({ active = true }: { active?: boolean }) {
  const model = useComputerUseSettings(active);
  return <><span data-testid="navigation">{model.data?.navigation_visible ? "visible" : "hidden"}</span><ComputerUseSettings model={model} /></>;
}

beforeEach(() => {
  vi.clearAllMocks(); mocks.fetch.mockReset(); mocks.session = "original-session"; mocks.confirm.mockResolvedValue(true);
  mocks.fetch.mockImplementation(async (_url: URL, init?: RequestInit) => response(snapshot(init?.method === "POST" ? { approved_apps: [] } : {})));
  useAppStore.setState({ conversationId: "A", workingDirectory: "C:/project-A", isConnected: true, pendingConversationSwitchId: null,
    skillsMarketplaceOpen: false, skillsMarketplaceSelectedPluginId: null, resolvedTheme: "dark",
    runtimeCapabilities: { mcp_registry_version: 1 }, draft: "Keep the unsaved draft", attachments: [{ id: "kept-file", name: "notes.txt", type: "text/plain", size: 0, status: "ready" }],
    quotedMessage: { id: "kept-quote", role: "assistant", content: "Keep this quote" } });
});
afterEach(cleanup);

it("reads actual approvals and confirms removal through the original authenticated owner and provider selection", async () => {
  render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: "移除 Actual App A" }));
  await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(2));
  const [readUrl, readOptions] = mocks.fetch.mock.calls[0];
  expect(readUrl.origin).toBe("http://owner-backend.test");
  expect(Object.fromEntries(readUrl.searchParams)).toEqual({ session_id: "original-session", conversation_id: "A", workspace_root: "C:/project-A" });
  expect(readOptions.headers.Authorization).toBe("Bearer original-runtime");
  expect(readOptions.cache).toBe("no-store");
  expect(mocks.confirm).toHaveBeenCalledWith(expect.objectContaining({ title: "要从始终允许的应用中移除“Actual App A”吗？", confirmLabel: "移除", danger: true }));
  const [url, init] = mocks.fetch.mock.calls[1];
  expect(url.pathname).toBe("/api/settings/computer-use/app-approvals/remove");
  expect(JSON.parse(init.body)).toEqual({ session_id: "original-session", conversation_id: "A", workspace_root: "C:/project-A",
    provider_id: "minicode-win32", selection_id: "selected-native-A", app_id: "app-A" });
  await screen.findByText("暂无");
  expect(mocks.toast).toHaveBeenCalledWith("已移除允许的应用", "success");
  expect(useAppStore.getState()).toMatchObject({ draft: "Keep the unsaved draft", attachments: [{ id: "kept-file" }], quotedMessage: { id: "kept-quote" } });
  expect(screen.queryByRole("button", { name: /安装|管理|始终允许$/ })).toBeNull();
});

it("does not remove an app when confirmation is cancelled", async () => {
  mocks.confirm.mockResolvedValue(false);
  render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: "移除 Actual App A" }));
  await waitFor(() => expect(mocks.confirm).toHaveBeenCalledOnce());
  expect(mocks.fetch).toHaveBeenCalledOnce();
  expect(screen.getByText("Actual App A")).toBeTruthy();
});

it("preserves the real list and actual failure when revoke is refused", async () => {
  mocks.fetch.mockImplementation(async (_url: URL, init?: RequestInit) => init?.method === "POST"
    ? new Response("Selected provider changed", { status: 409 }) : response(snapshot()));
  render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: "移除 Actual App A" }));
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Selected provider changed");
  expect(screen.getByText("Actual App A")).toBeTruthy();
  expect(screen.queryByText("暂无")).toBeNull();
  expect(mocks.toast).not.toHaveBeenCalled();
});

it("does not turn a failed initial native store read into None yet", async () => {
  mocks.fetch.mockResolvedValue(new Response("Actual persistent store read failed", { status: 500 }));
  render(<Harness />);
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Actual persistent store read failed");
  expect(screen.getByTestId("navigation").textContent).toBe("hidden");
  expect(screen.queryByText("暂无")).toBeNull();
  expect(screen.queryByRole("button")).toBeNull();
});

it("does not present session grants or a configured policy as permanent approval controls", async () => {
  mocks.fetch.mockResolvedValue(response(snapshot({ provider_id: "installed-source", navigation_visible: false,
    capabilities: { persistent_approvals: false, revoke_app_approval: false, persist_modes: ["session"], sound_mode: false, locked_use: false }, approved_apps: null })));
  render(<Harness />);
  await waitFor(() => expect(screen.queryByText("正在加载允许的应用…")).toBeNull());
  expect(screen.getByTestId("navigation").textContent).toBe("hidden");
  expect(screen.queryByRole("heading", { name: "始终允许的应用" })).toBeNull();
  expect(screen.queryByRole("button")).toBeNull();
});

it("supports actual Home scope without inventing a conversation or host parameter", async () => {
  useAppStore.setState({ conversationId: null, workingDirectory: "" });
  mocks.fetch.mockResolvedValue(response(snapshot({ owner: { session_id: "original-session", conversation_id: null, workspace_root: null } })));
  render(<Harness />);
  await screen.findByRole("button", { name: "移除 Actual App A" });
  expect(Object.fromEntries(mocks.fetch.mock.calls[0][0].searchParams)).toEqual({ session_id: "original-session" });
});

it("ignores an earlier A read after A to B to A and aborts its original request", async () => {
  const first = deferred<Response>();
  mocks.fetch.mockImplementation(async () => response(snapshot({ approved_apps: [{ ...approved, app_id: "fresh-A", display_name: "Fresh App A" }] })))
    .mockImplementationOnce(() => first.promise);
  render(<Harness />);
  await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
  act(() => useAppStore.setState({ conversationId: "B", workingDirectory: "C:/project-B" }));
  act(() => useAppStore.setState({ conversationId: "A", workingDirectory: "C:/project-A" }));
  expect(mocks.fetch.mock.calls[0][1].signal.aborted).toBe(true);
  await screen.findByText("Fresh App A");
  await act(async () => first.resolve(response(snapshot())));
  expect(screen.queryByText("Actual App A")).toBeNull();
  expect(screen.getByText("Fresh App A")).toBeTruthy();
});

it("retires an original confirmation across A to B to A", async () => {
  const confirmation = deferred<boolean>();
  mocks.confirm.mockReturnValue(confirmation.promise);
  render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: "移除 Actual App A" }));
  act(() => useAppStore.setState({ conversationId: "B", workingDirectory: "C:/project-B" }));
  act(() => useAppStore.setState({ conversationId: "A", workingDirectory: "C:/project-A" }));
  await screen.findByRole("button", { name: "移除 Actual App A" });
  await act(async () => confirmation.resolve(true));
  expect(mocks.fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
});

it("rejects an open confirmation after an actual provider re-selection is read", async () => {
  const confirmation = deferred<boolean>();
  mocks.confirm.mockReturnValue(confirmation.promise);
  render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: "移除 Actual App A" }));
  mocks.fetch.mockResolvedValue(response(snapshot({ provider_id: "installed-source", selection_id: "new-selection", approved_apps: [{ ...approved, app_id: "new-app", display_name: "New Provider App" }] })));
  act(() => window.dispatchEvent(new Event("focus")));
  await screen.findByText("New Provider App");
  await act(async () => confirmation.resolve(true));
  expect(mocks.fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
});

it("does not project an original pending revoke receipt into the newly selected task", async () => {
  const removing = deferred<Response>();
  mocks.fetch.mockImplementation(async (_url: URL, init?: RequestInit) => init?.method === "POST" ? removing.promise : response(snapshot()));
  render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: "移除 Actual App A" }));
  await waitFor(() => expect(mocks.fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true));
  mocks.fetch.mockResolvedValue(response(snapshot({ owner: { session_id: "original-session", conversation_id: "B", workspace_root: "C:/project-B" },
    selection_id: "selected-B", approved_apps: [{ ...approved, app_id: "app-B", display_name: "Actual App B" }] })));
  act(() => useAppStore.setState({ conversationId: "B", workingDirectory: "C:/project-B", draft: "B unsaved" }));
  await screen.findByText("Actual App B");
  await act(async () => removing.resolve(response(snapshot({ approved_apps: [] }))));
  expect(screen.getByText("Actual App B")).toBeTruthy();
  expect(screen.queryByText("暂无")).toBeNull();
  expect(mocks.toast).not.toHaveBeenCalled();
  expect(useAppStore.getState().draft).toBe("B unsaved");
});

it("lets the real revoke receipt override an older focus read without resurrecting its list", async () => {
  const reading = deferred<Response>();
  const confirmation = deferred<boolean>();
  mocks.confirm.mockReturnValue(confirmation.promise);
  mocks.fetch.mockResolvedValueOnce(response(snapshot())).mockImplementationOnce(() => reading.promise)
    .mockImplementation(async () => response(snapshot({ approved_apps: [] })));
  render(<Harness />);
  const remove = await screen.findByRole("button", { name: "移除 Actual App A" });
  fireEvent.click(remove);
  act(() => window.dispatchEvent(new Event("focus")));
  await act(async () => confirmation.resolve(true));
  await screen.findByText("暂无");
  await act(async () => reading.resolve(response(snapshot())));
  expect(screen.queryByText("Actual App A")).toBeNull();
});

it("remounts the real settings effect under StrictMode without reusing an aborted lifetime", async () => {
  render(<StrictMode><Harness /></StrictMode>);
  await screen.findByRole("button", { name: "移除 Actual App A" });
  expect(mocks.fetch.mock.calls[0][1].signal.aborted).toBe(true);
  expect(mocks.fetch.mock.calls.at(-1)![1].signal.aborted).toBe(false);
});

it("installs only the returned Any App control through its original owner and consumes the real enabled receipt", async () => {
  mocks.fetch.mockImplementation(async (_url: URL, init?: RequestInit) => response(snapshot({ controls: [anyApp(init?.method === "POST"
    ? { installed: true, enabled: true, can_install: false, can_toggle: true } : {})] })));
  render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: "安装 Computer Use" }));
  const toggle = await screen.findByRole("switch", { name: "切换 Computer Use" });
  expect(toggle.getAttribute("aria-checked")).toBe("true");
  const [url, init, options] = mocks.fetch.mock.calls[1];
  expect(url.pathname).toBe("/api/settings/computer-use/controls/plugin");
  expect(options).toEqual({ timeoutMs: 300_000 });
  expect(JSON.parse(init.body)).toEqual({ session_id: "original-session", conversation_id: "A", workspace_root: "C:/project-A",
    provider_id: "minicode-win32", selection_id: "selected-native-A", control_id: "any-app", action: "install" });
  expect(mocks.command.mock.calls.map(([command]) => command.type)).toEqual(["skills.list", "mcp.list", "runtime.capabilities.inspect"]);
  expect(screen.queryByRole("button", { name: "管理" })).toBeNull();
  expect(screen.queryByRole("button", { name: "删除" })).toBeNull();
  expect(useAppStore.getState().draft).toBe("Keep the unsaved draft");
});

it("does not project a requested plugin toggle before its actual mutation result", async () => {
  const changing = deferred<Response>();
  mocks.fetch.mockImplementation(async (_url: URL, init?: RequestInit) => init?.method === "POST" ? changing.promise
    : response(snapshot({ controls: [anyApp({ installed: true, enabled: true, can_toggle: true })] })));
  render(<Harness />);
  const toggle = await screen.findByRole("switch", { name: "切换 Computer Use" });
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-checked")).toBe("true");
  expect((toggle as HTMLButtonElement).disabled).toBe(true);
  const [, init] = mocks.fetch.mock.calls[1];
  expect(JSON.parse(init.body)).toMatchObject({ control_id: "any-app", action: "set_enabled", enabled: false });
  await act(async () => changing.resolve(response(snapshot({ controls: [anyApp({ installed: true, enabled: false, can_toggle: true })] }))));
  expect(toggle.getAttribute("aria-checked")).toBe("false");
});

it("keeps Install text with the existing loading spinner while the actual package operation is pending", async () => {
  const installing = deferred<Response>();
  mocks.fetch.mockImplementation(async (_url: URL, init?: RequestInit) => init?.method === "POST" ? installing.promise
    : response(snapshot({ controls: [anyApp()] })));
  render(<Harness />);
  const install = await screen.findByRole("button", { name: "安装 Computer Use" });
  fireEvent.click(install);
  expect(install.textContent).toBe("安装");
  expect(install.getAttribute("aria-busy")).toBe("true");
  expect((install as HTMLButtonElement).disabled).toBe(true);
  expect(install.querySelector("svg.animate-spin[aria-hidden='true']")).not.toBeNull();
  expect(screen.queryByText("安装中…")).toBeNull();
  await act(async () => installing.resolve(response(snapshot({ controls: [anyApp({ installed: true, enabled: true, can_toggle: true })] }))));
  expect(screen.getByRole("switch", { name: "切换 Computer Use" }).getAttribute("aria-checked")).toBe("true");
});

it("uses real plugin asset URLs for the dark variant and preserves the declared light URL on theme change", async () => {
  mocks.fetch.mockResolvedValue(response(snapshot({ controls: [anyApp({ installed: true, can_toggle: true,
    plugin_path: "C:/真实插件/computer-use", icon_variant: "logo", icon_variants: ["logo", "logo-dark"],
    icon_url: "https://vendor.example/declared-light.png" })] })));
  render(<Harness />);
  const details = await screen.findByRole("button", { name: "查看插件详情 Computer Use" });
  const dark = new URL(details.querySelector("img")!.getAttribute("src")!);
  expect(dark.origin).toBe(window.location.origin);
  expect(dark.pathname).toBe("/api/plugins/asset");
  expect(dark.searchParams.get("plugin_path")).toBe("C:/真实插件/computer-use");
  expect(dark.searchParams.get("variant")).toBe("logo-dark");
  act(() => useAppStore.setState({ resolvedTheme: "light" }));
  expect(details.querySelector("img")!.getAttribute("src")).toBe("https://vendor.example/declared-light.png");
  expect(mocks.fetch).toHaveBeenCalledOnce();
});

it("uses the actual path and composer variant helper when no URL is declared and leaves catalog placeholders generic", async () => {
  mocks.fetch.mockResolvedValue(response(snapshot({ controls: [anyApp({ installed: true, plugin_path: "C:/plugins/actual-app",
    icon_variant: "composer", icon_variants: ["composer"], icon_url: null })] })));
  render(<Harness />);
  const details = await screen.findByRole("button", { name: "查看插件详情 Computer Use" });
  const icon = new URL(details.querySelector("img")!.getAttribute("src")!);
  expect(icon.pathname).toBe("/api/plugins/asset");
  expect(icon.searchParams.get("plugin_path")).toBe("C:/plugins/actual-app");
  expect(icon.searchParams.get("variant")).toBe("composer");
  mocks.fetch.mockResolvedValue(response(snapshot({ controls: [anyApp()] })));
  act(() => window.dispatchEvent(new Event("focus")));
  await screen.findByRole("button", { name: "安装 Computer Use" });
  expect(details.querySelector("img")).toBeNull();
  expect(details.querySelector("[data-brand='generic']")).not.toBeNull();
});

it("opens the existing marketplace details for the actual canonical plugin without activating it", async () => {
  mocks.fetch.mockResolvedValue(response(snapshot({ controls: [anyApp()] })));
  render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: "查看插件详情 Computer Use" }));
  expect(useAppStore.getState()).toMatchObject({ skillsMarketplaceOpen: true, skillsMarketplaceTab: "plugins",
    skillsMarketplaceReturnTarget: "settings", skillsMarketplaceSelectedPluginId: "computer-use@openai-bundled",
    draft: "Keep the unsaved draft", attachments: [{ id: "kept-file" }] });
  expect(mocks.fetch).toHaveBeenCalledOnce();
  expect(mocks.command).not.toHaveBeenCalled();
});

it("preserves the original control and true error when plugin mutation is refused", async () => {
  mocks.fetch.mockImplementation(async (_url: URL, init?: RequestInit) => init?.method === "POST"
    ? new Response("Original plugin inventory changed", { status: 409 }) : response(snapshot({ controls: [anyApp()] })));
  render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: "安装 Computer Use" }));
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Original plugin inventory changed");
  expect(screen.getByRole("button", { name: "安装 Computer Use" })).toBeTruthy();
  expect(screen.queryByRole("switch")).toBeNull();
  expect(mocks.command).not.toHaveBeenCalled();
});

it("keeps the saved plugin state and actual runtime failure distinct from successful activation", async () => {
  mocks.fetch.mockImplementation(async (_url: URL, init?: RequestInit) => response(snapshot({
    controls: [anyApp({ installed: true, enabled: init?.method === "POST", can_toggle: true })],
    ...(init?.method === "POST" ? { runtime_refresh: { ok: false, errors: ["Actual CUA runtime failed to load"] } } : {}),
  })));
  render(<Harness />);
  fireEvent.click(await screen.findByRole("switch", { name: "切换 Computer Use" }));
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Actual CUA runtime failed to load");
  expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true");
  expect(mocks.toast).not.toHaveBeenCalled();
});

it("does not send late plugin results or refresh commands into a newly selected task", async () => {
  const changing = deferred<Response>();
  mocks.fetch.mockImplementation(async (_url: URL, init?: RequestInit) => init?.method === "POST" ? changing.promise
    : response(snapshot({ controls: [anyApp()] })));
  render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: "安装 Computer Use" }));
  mocks.fetch.mockResolvedValue(response(snapshot({ owner: { session_id: "original-session", conversation_id: "B", workspace_root: "C:/project-B" }, controls: [] })));
  act(() => useAppStore.setState({ conversationId: "B", workingDirectory: "C:/project-B", draft: "B unsaved" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "安装 Computer Use" })).toBeNull());
  await act(async () => changing.resolve(response(snapshot({ controls: [anyApp({ installed: true, enabled: true })] }))));
  expect(screen.queryByRole("switch")).toBeNull();
  expect(mocks.command).not.toHaveBeenCalled();
  expect(useAppStore.getState().draft).toBe("B unsaved");
});
