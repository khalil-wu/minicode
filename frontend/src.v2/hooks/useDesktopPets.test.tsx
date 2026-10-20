/* @vitest-environment jsdom */
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { useDesktopPets } from "./useDesktopPets";

const mocks = vi.hoisted(() => ({
  publish: vi.fn(), update: vi.fn(async () => {}), toast: vi.fn(),
  onAction: vi.fn(() => () => {}), complete: vi.fn(async () => {}),
}));
vi.mock("../desktop/runtime", async load => ({ ...await load<Record<string, unknown>>(), isDesktop: () => true,
  desktop: () => ({ pets: { publishActivity: mocks.publish, update: mocks.update, onActionRequested: mocks.onAction, completeAction: mocks.complete } }) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: (...args: unknown[]) => mocks.toast(...args) }));
const Harness = () => { useDesktopPets(); return null; };
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it("keeps a failed initialization visible without retrying on draft edits, and resends real activity after explicit Refresh", async () => {
  mocks.publish.mockRejectedValueOnce(new Error("Pet preferences could not be read")).mockResolvedValue(undefined);
  useAppStore.setState({ conversationId: "actual", conversations: [{ id: "actual", title: "Actual task", updatedAt: "2026-10-20", sessionStatus: "running" }],
    messages: [], conversationStreaming: {}, isStreaming: true, isConnected: true, reducedMotion: false,
    pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [] });
  render(<Harness />);
  await waitFor(() => expect(mocks.toast).toHaveBeenCalledTimes(1));
  act(() => useAppStore.setState({ draft: "Unrelated unsent text" }));
  expect(mocks.publish).toHaveBeenCalledTimes(1);
  act(() => window.dispatchEvent(new Event("pets:refreshed")));
  await waitFor(() => expect(mocks.publish).toHaveBeenCalledTimes(2));
  expect(mocks.publish.mock.calls[1][0].tasks[0]).toMatchObject({ id: "actual", title: "Actual task", status: "running" });
  act(() => useAppStore.setState({ draft: "Keep editing" }));
  expect(mocks.publish).toHaveBeenCalledTimes(2);
  expect(useAppStore.getState().draft).toBe("Keep editing");
});
