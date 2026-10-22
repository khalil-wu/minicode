/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { HistoryCellRenderer } from "../components/ChatTurn";
import type { ErrorCellState } from "./cellTypes";
import { useAppStore } from "../../stores";
import { hydrateMessages } from "../transcriptHydration";
import { projectMessagesToTurns } from "../chatSurfaceState";

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
  message: "401 Unauthorized: the published failure is at https://example.com/request for details.", recoverable: false, createdAt: 1,
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
  expect(screen.getByRole("alert").textContent).toContain(cell.message);
  fireEvent.click(screen.getByRole("link"));
  await waitFor(() => expect(targets.inApp).toHaveBeenCalledExactlyOnceWith("https://example.com/request"));
  expect(targets.external).not.toHaveBeenCalled();
  expect(targets.toast).not.toHaveBeenCalled();
  expect(useAppStore.getState().draft).toBe("Keep authored draft");
});

it("preserves a readonly child transcript owner instead of opening its link for the active parent", async () => {
  render(<HistoryCellRenderer cell={cell} conversationId="child-owner" isTranscriptMode />);
  expect(screen.getByRole("alert").textContent).toContain(cell.message);
  fireEvent.click(screen.getByRole("link"), { altKey: true });
  await waitFor(() => expect(targets.toast).toHaveBeenCalledExactlyOnceWith("会话已切换，请回到原请求后打开链接。", "error"));
  expect(targets.inApp).not.toHaveBeenCalled();
  expect(targets.external).not.toHaveBeenCalled();
  expect(useAppStore.getState().draft).toBe("Keep authored draft");
});

it.each(["owner", "child-owner"])("retains the original public failure through cold hydration and the %s fallback renderer", (owner) => {
  const failure = "401 Unauthorized: missing project role.\n  Public help: https://example.com/request";
  const restored = hydrateMessages([{ id: `restored-${owner}`, role: "assistant", content: "", timestamp: 1000,
    terminal_status: "failed", failure_message: failure, completed_at: 1200, turn_id: `turn-${owner}`, blocks: [] }]);
  const cells = projectMessagesToTurns(restored, false)[0].committedCells;
  expect(cells).toHaveLength(1);
  expect(cells[0]).toMatchObject({ kind: "error", source: "agent", message: failure });
  render(<HistoryCellRenderer cell={cells[0]} conversationId={owner} isTranscriptMode />);
  expect(screen.getByRole("alert").textContent).toContain(failure);
  expect(screen.getByRole("link").getAttribute("href")).toBe("https://example.com/request");
  expect(useAppStore.getState().draft).toBe("Keep authored draft");
});
