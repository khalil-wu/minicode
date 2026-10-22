/* @vitest-environment jsdom */
import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { useComputerUseSettings } from "../hooks/useComputerUseSettings";
import { ComputerUseSettings } from "./ComputerUseSettings";
import type { ComputerUseSettingsSnapshot } from "../protocol/computer-use-settings";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), confirm: vi.fn(), toast: vi.fn(), session: "original-session" }));
vi.mock("../protocol/api", () => ({ apiBase: () => "http://owner-backend.test", authHeaders: (headers?: HeadersInit) => ({ ...headers, Authorization: "Bearer original-runtime" }),
  fetchWithTimeout: mocks.fetch, errorMessageFromResponseText: (text: string) => text }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: mocks.session }) }));
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: null }) }));
vi.mock("./DialogService", () => ({ showConfirm: mocks.confirm }));
vi.mock("./ToastContainer", () => ({ pushToast: mocks.toast }));

const approved = { app_id: "app-A", display_name: "Actual App A", icon_data_url: null };
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
