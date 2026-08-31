/* @vitest-environment jsdom */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useAppStore } from "../stores";
import { GeneralTab } from "./GeneralTab";

const updateMocks = vi.hoisted(() => ({
  getStatus: vi.fn(),
  onStatus: vi.fn(),
  check: vi.fn(),
  download: vi.fn(),
  preflight: vi.fn(),
  install: vi.fn(),
}));

vi.mock("../desktop/runtime", () => ({
  desktop: () => ({ updates: updateMocks }),
  isDesktop: () => true,
}));

describe("desktop update install preflight", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    updateMocks.getStatus.mockResolvedValue({ status: "ready", sequence: 1, version: "2.0.0" });
    updateMocks.onStatus.mockReturnValue(() => {});
    updateMocks.check.mockResolvedValue(true);
    updateMocks.download.mockResolvedValue(true);
    updateMocks.install.mockResolvedValue({ installed: true });
    useAppStore.setState({ allowedRemoteImageDomains: [] });
  });

  it("shows blocking work and does not invoke install", async () => {
    updateMocks.preflight.mockResolvedValue({
      allowed: false,
      fingerprint: "blocked",
      version: "2.0.0",
      checks: [{ code: "editor.dirty", severity: "blocking", message: "Unsaved editor." }],
    });
    render(<GeneralTab remoteImagePolicy="ask" setRemoteImagePolicy={() => {}} />);

    const installButton = await screen.findByRole("button", { name: "重启并安装" });
    fireEvent.click(installButton);

    await waitFor(() => {
      expect(screen.getByText(/暂不能安装：编辑器中仍有未保存内容/)).toBeTruthy();
    });
    expect(updateMocks.install).not.toHaveBeenCalled();
  });

  it("passes the reviewed fingerprint into install", async () => {
    updateMocks.preflight.mockResolvedValue({
      allowed: true,
      fingerprint: "reviewed-fingerprint",
      version: "2.0.0",
      checks: [],
    });
    render(<GeneralTab remoteImagePolicy="ask" setRemoteImagePolicy={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: "重启并安装" }));

    await waitFor(() => {
      expect(updateMocks.install).toHaveBeenCalledWith({ fingerprint: "reviewed-fingerprint" });
    });
  });
});
