/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { GithubConnection } from "./GithubConnection";
import { openExternal } from "../desktop/runtime";
import { copyText } from "../lib/clipboard";

vi.mock("../desktop/runtime", () => ({ isDesktop: () => true, openExternal: vi.fn(async () => true) }));
vi.mock("../lib/clipboard", () => ({ copyText: vi.fn(async () => true) }));
const disconnected = { available: true, authenticated: false, login: null, host: "github.com", message: "尚未连接 GitHub。" };
let stream: ReadableStream<Uint8Array>;
let streamController: ReadableStreamDefaultController<Uint8Array>;
let loginSignal: AbortSignal | undefined;
beforeEach(() => {
  stream = new ReadableStream({ start(controller) { streamController = controller; } });
  loginSignal = undefined;
  vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") {
      loginSignal = init.signal as AbortSignal;
      loginSignal.addEventListener("abort", () => streamController.error(new DOMException("Aborted", "AbortError")), { once: true });
      return new Response(stream, { headers: { "content-type": "application/x-ndjson" } });
    }
    return new Response(JSON.stringify(disconnected));
  }));
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });
const emit = (event: unknown, newline = true) => streamController.enqueue(new TextEncoder().encode(JSON.stringify(event) + (newline ? "\n" : "")));

it("reads status on activation and starts authorization only after a connection click", async () => {
  const { rerender } = render(<GithubConnection active={false} />);
  expect(fetch).not.toHaveBeenCalled();
  rerender(<GithubConnection active />);
  await screen.findByText("尚未连接");
  expect(vi.mocked(fetch).mock.calls).toHaveLength(1);
  expect(vi.mocked(fetch).mock.calls[0][1]?.method).toBeUndefined();
  fireEvent.click(screen.getByRole("button", { name: "连接 GitHub" }));
  await screen.findByRole("button", { name: "取消连接" });
  expect(vi.mocked(fetch).mock.calls[1][1]).toMatchObject({ method: "POST" });
  expect(String(vi.mocked(fetch).mock.calls[1][0])).toContain("/api/github/login");
  fireEvent.click(screen.getByRole("button", { name: "取消连接" }));
  expect(loginSignal?.aborted).toBe(true);
  await screen.findByText("已取消连接。");
});

it("displays the real device code, copies it, opens the authorization page and confirms the returned account", async () => {
  render(<GithubConnection />);
  await screen.findByText("尚未连接");
  fireEvent.click(screen.getByRole("button", { name: "连接 GitHub" }));
  await act(async () => emit({ phase: "authorizing", user_code: "ABCD-1234", verification_uri: "https://github.com/login/device", message: "请完成 GitHub 授权。" }));
  expect(await screen.findByLabelText("GitHub 授权码")).toHaveProperty("textContent", "ABCD-1234");
  fireEvent.click(screen.getByRole("button", { name: "复制授权码" }));
  await screen.findByRole("button", { name: "授权码已复制" });
  expect(copyText).toHaveBeenCalledWith("ABCD-1234", "授权码");
  fireEvent.click(screen.getByRole("button", { name: "打开授权页面" }));
  await waitFor(() => expect(openExternal).toHaveBeenCalledWith("https://github.com/login/device"));
  expect(screen.queryByText("已连接")).toBeNull();
  await act(async () => {
    emit({ phase: "connected", connection: { ...disconnected, authenticated: true, login: "minicode-user", message: "连接成功。" } }, false);
    streamController.close();
  });
  await screen.findByText("已连接");
  expect(screen.getByText("minicode-user")).toBeTruthy();
  expect(screen.queryByLabelText("GitHub 授权码")).toBeNull();
  expect(screen.queryByRole("button", { name: "取消连接" })).toBeNull();
});

it("keeps an authorization alive while a visited settings tab is hidden and aborts on unmount", async () => {
  const { rerender, unmount } = render(<GithubConnection />);
  await screen.findByText("尚未连接");
  fireEvent.click(screen.getByRole("button", { name: "连接 GitHub" }));
  await screen.findByRole("button", { name: "取消连接" });
  rerender(<GithubConnection active={false} />);
  expect(loginSignal?.aborted).toBe(false);
  await act(async () => emit({ phase: "authorizing", user_code: "EFGH-5678", verification_uri: "https://github.com/login/device" }));
  rerender(<GithubConnection active />);
  expect(await screen.findByLabelText("GitHub 授权码")).toHaveProperty("textContent", "EFGH-5678");
  expect(vi.mocked(fetch).mock.calls).toHaveLength(2);
  unmount();
  expect(loginSignal?.aborted).toBe(true);
});

it("reports server authorization failures and does not mark an incomplete stream connected", async () => {
  render(<GithubConnection />);
  await screen.findByText("尚未连接");
  fireEvent.click(screen.getByRole("button", { name: "连接 GitHub" }));
  await act(async () => { emit({ phase: "error", message: "授权已过期，请重新连接。" }); streamController.close(); });
  expect((await screen.findByRole("alert")).textContent).toBe("授权已过期，请重新连接。");
  expect(screen.queryByText("已连接")).toBeNull();
  expect((screen.getByRole("button", { name: "连接 GitHub" }) as HTMLButtonElement).disabled).toBe(false);
});

it("shows an interrupted authorization as unfinished instead of inferring success", async () => {
  render(<GithubConnection />);
  await screen.findByText("尚未连接");
  fireEvent.click(screen.getByRole("button", { name: "连接 GitHub" }));
  await act(async () => { emit({ phase: "starting", message: "准备授权…" }); streamController.close(); });
  expect((await screen.findByRole("alert")).textContent).toContain("尚未收到 GitHub 的完成状态");
  expect(screen.queryByText("已连接")).toBeNull();
});

it("does not claim a connected account when the runtime is unavailable", async () => {
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ ...disconnected, available: false, authenticated: true, login: "stale-account", message: "GitHub 连接组件未就绪。" })));
  render(<GithubConnection />);
  await screen.findByText("连接组件未就绪");
  expect(screen.queryByText("已连接")).toBeNull();
  expect(screen.queryByText("stale-account")).toBeNull();
  expect((screen.getByRole("button", { name: "连接 GitHub" }) as HTMLButtonElement).disabled).toBe(true);
});

it("shows a failed status refresh as unconfirmed while retaining the last account and actual host", async () => {
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ ...disconnected, authenticated: true, login: "enterprise-user", host: "github.company.example" })));
  render(<GithubConnection />);
  await screen.findByText("已连接");
  expect(screen.getByText(/github.company.example/)).toBeTruthy();
  vi.mocked(fetch).mockRejectedValueOnce(new Error("连接状态暂时无法读取。"));
  fireEvent.click(screen.getByRole("button", { name: "刷新 GitHub 连接状态" }));
  await screen.findByText("状态待确认");
  expect(screen.queryByText("已连接")).toBeNull();
  expect(screen.getByText("enterprise-user")).toBeTruthy();
  expect((screen.getByRole("alert")).textContent).toBe("连接状态暂时无法读取。");
});

it.each(["code-first", "url-first"])("retains device code and the actual URL when authorizing events arrive %s", async (order) => {
  render(<GithubConnection />);
  await screen.findByText("尚未连接");
  fireEvent.click(screen.getByRole("button", { name: "连接 GitHub" }));
  const codeEvent = { phase: "authorizing", user_code: "LINK-1234" };
  const linkEvent = { phase: "authorizing", verification_uri: "https://github.com/login/device?source=desktop" };
  await act(async () => {
    for (const event of order === "code-first" ? [codeEvent, linkEvent] : [linkEvent, codeEvent]) emit(event);
  });
  expect(await screen.findByLabelText("GitHub 授权码")).toHaveProperty("textContent", "LINK-1234");
  fireEvent.click(screen.getByRole("button", { name: "打开授权页面" }));
  await waitFor(() => expect(openExternal).toHaveBeenCalledWith("https://github.com/login/device?source=desktop"));
  fireEvent.click(screen.getByRole("button", { name: "取消连接" }));
});

it("offers the actual web authorization page even when the flow has no device code", async () => {
  render(<GithubConnection />);
  await screen.findByText("尚未连接");
  fireEvent.click(screen.getByRole("button", { name: "连接 GitHub" }));
  const target = "https://github.company.example/login/oauth/authorize?client_id=fixture&state=opaque";
  await act(async () => emit({ phase: "authorizing", verification_uri: target, message: "请打开 GitHub 授权页面确认连接。" }));
  expect(screen.queryByLabelText("GitHub 授权码")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "打开授权页面" }));
  await waitFor(() => expect(openExternal).toHaveBeenCalledWith(target));
  expect(screen.queryByText("已连接")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "取消连接" }));
});

it("aborts its owned authorization request when a malformed stream cannot be consumed", async () => {
  render(<GithubConnection />);
  await screen.findByText("尚未连接");
  fireEvent.click(screen.getByRole("button", { name: "连接 GitHub" }));
  await act(async () => streamController.enqueue(new TextEncoder().encode("<not-json>\n")));
  await screen.findByRole("alert");
  expect(loginSignal?.aborted).toBe(true);
  expect(screen.queryByRole("button", { name: "取消连接" })).toBeNull();
  expect(screen.queryByText("已连接")).toBeNull();
});
