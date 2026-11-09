// @vitest-environment jsdom
import { act, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { handoffConversationWorktree } from "./handoff-worktree";

const mocks = vi.hoisted(() => {
  let fail!: () => void;
  const pending = new Promise<void>((_resolve, reject) => { fail = () => reject(new Error("Handoff chunk unavailable")); });
  return { pending, fail: () => fail(), started: vi.fn(), toast: vi.fn() };
});
vi.mock("./handoff-worktree-action", async () => { mocks.started(); await mocks.pending; return { runHandoff: vi.fn() }; });
vi.mock("../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));

it("reports a failed operation import once to all same-owner waiters and releases that owner for another action", async () => {
  const first = handoffConversationWorktree("A", "Task A", "worktree");
  expect(handoffConversationWorktree("A", "Task A", "worktree")).toBe(first);
  await waitFor(() => expect(mocks.started).toHaveBeenCalledTimes(1));
  await act(async () => { mocks.fail(); await first; });
  const error = await import("./handoff-worktree-action").then(() => { throw new Error("Expected operation import failure"); }, cause => cause as Error);
  expect(mocks.toast).toHaveBeenCalledExactlyOnceWith(error.message, "error", 6000);
  const retry = handoffConversationWorktree("A", "Task A", "worktree");
  expect(retry).not.toBe(first);
  await retry;
  expect(mocks.toast).toHaveBeenCalledTimes(2);
  expect(mocks.toast).toHaveBeenLastCalledWith(error.message, "error", 6000);
});
