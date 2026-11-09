/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { pushToast } from "./ToastContainer";
import { FeatureFlagsTab } from "./FeatureFlagsTab";

vi.mock("./ToastContainer", () => ({
  pushToast: vi.fn(),
}));

vi.mock("../protocol/api", () => ({
  apiBase: () => "http://test.local",
  authHeaders: (headers?: HeadersInit) => headers ?? {},
  fetchWithTimeout: (url: string, init?: RequestInit) => fetch(url, init),
  errorMessageFromResponseText: (text: string, fallback: string) => text || fallback,
}));

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(),
}));

describe("FeatureFlagsTab loading", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("boom")));
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.unstubAllGlobals();
  });

  it("shows initial load errors inline without noisy startup toasts", async () => {
    render(<FeatureFlagsTab />);

    expect(await screen.findByText("功能开关加载失败。")).toBeTruthy();
    expect(screen.getByText("boom")).toBeTruthy();
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("keeps manual retries visible with a toast", async () => {
    render(<FeatureFlagsTab />);
    await screen.findByText("功能开关加载失败。");
    vi.mocked(pushToast).mockClear();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));

    await waitFor(() => expect(pushToast).toHaveBeenCalledWith("功能开关加载失败：boom", "error"));
  });

  it("uses one compact state control without duplicate metadata", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        flags: [{ name: "reactive_compact", default: true, enabled: true, source: "default", override: null }],
      }),
      text: async () => "",
    }));

    render(<FeatureFlagsTab />);

    expect(await screen.findByText("响应式上下文压缩")).toBeTruthy();
    expect((screen.getByLabelText("覆盖 响应式上下文压缩") as HTMLSelectElement).value).toBe("default");
    expect(screen.queryByText("default", { selector: "span" })).toBeNull();
    expect(screen.queryByText(/reactive_compact · default on/)).toBeNull();
    expect(screen.queryByText("Local overrides for experimental runtime and UI surfaces. Environment variables still win.")).toBeNull();
  });
});
