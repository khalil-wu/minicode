// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SandboxSettings } from "./SandboxSettings";

const mocks = vi.hoisted(() => ({ desktop: vi.fn(), fetch: vi.fn(), setup: vi.fn(), inspect: vi.fn() }));
vi.mock("../desktop/runtime", () => ({ desktop: mocks.desktop }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: mocks.inspect }));
vi.mock("../protocol/api", () => ({
  apiBase: () => "http://sandbox.test",
  authHeaders: () => ({ Authorization: "Bearer runtime-fixture" }),
  errorMessageFromResponseText: (body: string, fallback: string) => body || fallback,
  fetchWithTimeout: mocks.fetch,
}));

const unavailable = {
  available: false, backend: "unavailable", reason: "Windows sandbox setup has not completed.",
  setup_required: true, setup_supported: true, native_available: false, permission_mode: "confirm",
  state_root: "C:/MiniCode/state", sandbox_home: "C:/MiniCode/state/windows-sandbox",
  description: "首次使用默认权限的 Shell 与 Git 工具，需要完成一次系统初始化。",
};
const ready = { ...unavailable, available: true, native_available: true, setup_required: false, backend: "windows-elevated", reason: "", description: "默认权限的工具隔离已就绪。" };
const response = (data: unknown) => new Response(JSON.stringify(data), { status: 200 });
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};

beforeEach(() => {
  vi.resetAllMocks();
  mocks.desktop.mockReturnValue({ platformInfo: { isDesktop: true, platform: "win32", arch: "x64" }, sandbox: { setup: mocks.setup } });
  mocks.fetch.mockImplementation(async () => response(unavailable));
  mocks.setup.mockResolvedValue({ ok: true });
});
afterEach(cleanup);

it.each([undefined, { platformInfo: { isDesktop: true, platform: "linux" } }])("does not expose Windows initialization outside the Windows desktop", (native) => {
  mocks.desktop.mockReturnValue(native);
  const { container } = render(<SandboxSettings />);
  expect(container.textContent).toBe("");
  expect(mocks.fetch).not.toHaveBeenCalled();
  expect(mocks.setup).not.toHaveBeenCalled();
});

it("reads authenticated real status when the settings page becomes active without starting setup", async () => {
  const { rerender } = render(<SandboxSettings active={false} />);
  expect(mocks.fetch).not.toHaveBeenCalled();
  rerender(<SandboxSettings active />);
  await screen.findByText("需要初始化");
  expect(String(mocks.fetch.mock.calls[0][0])).toBe("http://sandbox.test/api/sandbox/status");
  expect(mocks.fetch.mock.calls[0][1]).toMatchObject({ headers: { Authorization: "Bearer runtime-fixture" }, cache: "no-store" });
  expect(mocks.setup).not.toHaveBeenCalled();
});

it("requires a setup click, suppresses duplicates and waits for backend confirmation before reporting ready", async () => {
  const setup = deferred<{ ok: boolean }>();
  const confirmation = deferred<Response>();
  mocks.setup.mockReturnValue(setup.promise);
  mocks.fetch.mockResolvedValueOnce(response(unavailable)).mockReturnValueOnce(confirmation.promise);
  render(<SandboxSettings />);
  const button = await screen.findByRole("button", { name: "初始化" });
  fireEvent.click(button);
  fireEvent.click(button);
  expect(mocks.setup).toHaveBeenCalledTimes(1);
  expect(mocks.setup).toHaveBeenCalledWith();
  expect((screen.getByRole("button", { name: "正在初始化…" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "重新检测 Windows 工具沙箱" }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => setup.resolve({ ok: true }));
  expect(mocks.fetch).toHaveBeenCalledTimes(2);
  expect(screen.queryByText("已就绪")).toBeNull();
  await act(async () => confirmation.resolve(response(ready)));
  await screen.findByText("已就绪");
  expect(screen.queryByRole("button", { name: "初始化" })).toBeNull();
  expect(mocks.inspect).toHaveBeenCalledExactlyOnceWith({ type: "runtime.capabilities.inspect", source: "sandbox.setup" });
});

it("does not treat a successful setup process as an available sandbox", async () => {
  render(<SandboxSettings />);
  fireEvent.click(await screen.findByRole("button", { name: "初始化" }));
  await screen.findByText("初始化步骤已结束，Windows 系统隔离尚未确认可用。请重新检测或查看检测详情。");
  expect(screen.getByText("需要初始化")).toBeTruthy();
  expect(screen.queryByText("已就绪")).toBeNull();
  expect(mocks.fetch).toHaveBeenCalledTimes(2);
  expect(mocks.inspect).not.toHaveBeenCalled();
});

it("keeps managed-tool availability separate from native Windows setup confirmation", async () => {
  mocks.fetch.mockResolvedValueOnce(response(unavailable)).mockResolvedValueOnce(response({
    ...unavailable, available: true, setup_required: false, native_available: false, backend: "docker",
  }));
  render(<SandboxSettings />);
  fireEvent.click(await screen.findByRole("button", { name: "初始化" }));
  await screen.findByText("初始化步骤已结束，Windows 系统隔离尚未确认可用。请重新检测或查看检测详情。");
  expect(screen.getByText("已就绪")).toBeTruthy();
  expect(screen.queryByText("Windows 系统初始化完成，默认权限的工具已可使用。")).toBeNull();
  expect(mocks.inspect).not.toHaveBeenCalled();
});

it.each([
  [{ ok: false, cancelled: true }, "已取消初始化，可以稍后重试。"],
  [{ ok: false, error: "无法完成系统初始化。" }, "无法完成系统初始化。"],
] as const)("preserves the real status after setup cancellation or failure", async (result, message) => {
  mocks.setup.mockResolvedValue(result);
  render(<SandboxSettings />);
  fireEvent.click(await screen.findByRole("button", { name: "初始化" }));
  await screen.findByText(message);
  await waitFor(() => expect((screen.getByRole("button", { name: "初始化" }) as HTMLButtonElement).disabled).toBe(false));
  expect(screen.getByText("需要初始化")).toBeTruthy();
  expect(mocks.fetch).toHaveBeenCalledTimes(2);
});

it("shows a failed status read as unconfirmed while retaining the last diagnostic snapshot", async () => {
  mocks.fetch.mockResolvedValueOnce(response(ready)).mockRejectedValueOnce(new Error("后端连接已断开。"));
  render(<SandboxSettings />);
  await screen.findByText("已就绪");
  fireEvent.click(screen.getByRole("button", { name: "重新检测 Windows 工具沙箱" }));
  await screen.findByText("状态待确认");
  expect(screen.getByRole("alert").textContent).toBe("检测失败：后端连接已断开。");
  expect(screen.getByText(ready.sandbox_home)).toBeTruthy();
  expect(screen.queryByText("已就绪")).toBeNull();
});

it("reports a desktop setup failure and rechecks availability", async () => {
  mocks.setup.mockRejectedValueOnce(new Error("系统授权窗口未能打开。"));
  render(<SandboxSettings />);
  fireEvent.click(await screen.findByRole("button", { name: "初始化" }));
  await screen.findByText("系统授权窗口未能打开。");
  await waitFor(() => expect((screen.getByRole("button", { name: "初始化" }) as HTMLButtonElement).disabled).toBe(false));
  expect(mocks.fetch).toHaveBeenCalledTimes(2);
  expect(mocks.inspect).not.toHaveBeenCalled();
  expect(screen.getByText("需要初始化")).toBeTruthy();
});

it("does not offer an unusable setup action when the installed initializer is missing", async () => {
  mocks.fetch.mockResolvedValueOnce(response({ ...unavailable, setup_supported: false }));
  render(<SandboxSettings />);
  const button = await screen.findByRole("button", { name: "初始化" });
  expect((button as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(button);
  expect(mocks.setup).not.toHaveBeenCalled();
});

it("does not start another status request after unmounting during system authorization", async () => {
  const setup = deferred<{ ok: boolean }>();
  mocks.setup.mockReturnValue(setup.promise);
  const view = render(<SandboxSettings />);
  fireEvent.click(await screen.findByRole("button", { name: "初始化" }));
  view.unmount();
  await act(async () => setup.resolve({ ok: true }));
  expect(mocks.fetch).toHaveBeenCalledTimes(1);
});
