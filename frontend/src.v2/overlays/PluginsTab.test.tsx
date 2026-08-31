/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { pushToast } from "./ToastContainer";
import { PluginsTab } from "./PluginsTab";

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
  pluginAssetResourceUrlWithToken: () => "",
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
    expect(screen.getByText("boom")).toBeTruthy();
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
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ plugins: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    })));
    render(<PluginsTab />);
    await screen.findByText("还没有本地插件");

    fireEvent.click(screen.getByRole("button", { name: "填写插件路径" }));

    expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "插件文件夹或安装包路径" }));
  });

});
