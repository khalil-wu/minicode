// @vitest-environment jsdom
import { act, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { handoffConversationWorktree } from "./handoff-worktree";

const mocks = vi.hoisted(() => {
  let release!: () => void;
  const ready = new Promise<void>(resolve => { release = resolve; });
  return { ready, release: () => release(), started: vi.fn(), run: vi.fn(), toast: vi.fn() };
});
vi.mock("./handoff-worktree-action", async () => { mocks.started(); await mocks.ready; return { runHandoff: mocks.run }; });
vi.mock("../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));

it("keeps the synchronous per-owner Promise through a pending operation import and allows independent owners", async () => {
  const completed = new Map<string, () => void>();
  mocks.run.mockImplementation((id: string) => new Promise<void>(resolve => { completed.set(id, resolve); }));
  expect(mocks.started).not.toHaveBeenCalled();
  const a = handoffConversationWorktree("A", "Task A", "worktree");
  expect(handoffConversationWorktree("A", "Changed caller", "local")).toBe(a);
  await waitFor(() => expect(mocks.started).toHaveBeenCalledTimes(1));
  expect(mocks.run).not.toHaveBeenCalled();
  await act(async () => mocks.release());
  await waitFor(() => expect(mocks.run).toHaveBeenCalledTimes(1));
  const b = handoffConversationWorktree("B", "Task B", "local");
  expect(b).not.toBe(a);
  await waitFor(() => expect(mocks.run).toHaveBeenCalledTimes(2));
  expect(mocks.run).toHaveBeenCalledWith("A", "Task A", "worktree");
  expect(mocks.run).toHaveBeenCalledWith("B", "Task B", "local");
  expect(handoffConversationWorktree("A", "Task A", "local")).toBe(a);
  completed.get("A")!(); await a;
  const again = handoffConversationWorktree("A", "Task A", "local");
  expect(again).not.toBe(a);
  expect(handoffConversationWorktree("B", "Task B", "worktree")).toBe(b);
  await waitFor(() => expect(mocks.run).toHaveBeenCalledTimes(3));
  completed.get("A")!(); completed.get("B")!();
  await Promise.all([again, b]);
  expect(mocks.toast).not.toHaveBeenCalled();
});
