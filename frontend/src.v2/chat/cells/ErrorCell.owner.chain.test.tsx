/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { HistoryCellRenderer } from "../components/ChatTurn";
import type { ErrorCellState } from "./cellTypes";
import { useAppStore } from "../../stores";

const targets = vi.hoisted(() => ({ preferences: vi.fn(), external: vi.fn(), inApp: vi.fn(), toast: vi.fn() }));
vi.mock("../../desktop/runtime", async load => ({
  ...await load<typeof import("../../desktop/runtime")>(), isDesktop: () => true,
  getDesktopPreferences: targets.preferences, openExternal: targets.external,
}));
vi.mock("../openWebInBrowser", () => ({ openWebInBrowser: targets.inApp }));
vi.mock("../../overlays/ToastContainer", async load => ({
  ...await load<typeof import("../../overlays/ToastContainer")>(), pushToast: targets.toast,
}));

const cell: ErrorCellState = {
  kind: "error", id: "terminal-error", title: "Request ended", source: "agent",
  message: "See https://example.com/request for details.", recoverable: false, createdAt: 1,
};

beforeEach(() => {
  vi.clearAllMocks();
  targets.preferences.mockResolvedValue({
    externalBrowserLinkShortcut: "alt", urlOpenTarget: "in-app-browser", localUrlOpenTarget: "in-app-browser",
  });
  targets.inApp.mockReturnValue(true);
  useAppStore.setState({ conversationId: "owner", pendingConversationSwitchId: null, draft: "Keep authored draft" });
});
afterEach(cleanup);

it("passes the owning transcript through the error consumer to the saved web destination", async () => {
  render(<HistoryCellRenderer cell={cell} conversationId="owner" />);
  fireEvent.click(screen.getByRole("link"));
  await waitFor(() => expect(targets.inApp).toHaveBeenCalledExactlyOnceWith("https://example.com/request"));
  expect(targets.external).not.toHaveBeenCalled();
  expect(targets.toast).not.toHaveBeenCalled();
  expect(useAppStore.getState().draft).toBe("Keep authored draft");
});

it("preserves a readonly child transcript owner instead of opening its link for the active parent", async () => {
  render(<HistoryCellRenderer cell={cell} conversationId="child-owner" isTranscriptMode />);
  fireEvent.click(screen.getByRole("link"), { altKey: true });
  await waitFor(() => expect(targets.toast).toHaveBeenCalledExactlyOnceWith("会话已切换，请回到原请求后打开链接。", "error"));
  expect(targets.inApp).not.toHaveBeenCalled();
  expect(targets.external).not.toHaveBeenCalled();
  expect(useAppStore.getState().draft).toBe("Keep authored draft");
});
