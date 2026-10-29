// @vitest-environment jsdom
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ClientCommand, CommandResultEvent } from "../protocol/events";
import { registerWebSocketSender, rejectAllPendingCommandResults, resolveClientCommandResult } from "../protocol/ws-outbox";
import { handoffConversationWorktree } from "./handoff-worktree";
import { pushToast } from "../overlays/ToastContainer";

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

let commands: ClientCommand[];
const preflight = { allowed: true, checks: [], fingerprint: "initial", dirty_action: "block",
  reuse_branch: true, destination: { branch: "codex/task" }, local_branches: ["main", "release"], local_checkout_branch: "main" };

function answer(command: ClientCommand, data: Record<string, unknown>, message = "ready", level = "success") {
  expect(resolveClientCommandResult({ type: "command.result", command: command.type, client_command_id: command.client_command_id,
    level, message, data } as CommandResultEvent)).toBe(true);
}

beforeEach(() => {
  commands = [];
  vi.mocked(pushToast).mockReset();
  registerWebSocketSender(command => { commands.push(command); return true; });
});
afterEach(() => { registerWebSocketSender(null); rejectAllPendingCommandResults("test ended"); cleanup(); });

async function start(id = "owner-a", target: "local" | "worktree" = "worktree") {
  let operation!: Promise<void>;
  const previousCount = commands.length;
  act(() => { operation = handoffConversationWorktree(id, "Task A", target); });
  await waitFor(() => expect(commands).toHaveLength(previousCount + 1));
  return { operation };
}

async function openConfirmation() {
  await act(async () => answer(commands.at(-1)!, preflight));
  expect(await screen.findByRole("dialog", { name: "将“Task A”移到隔离工作区？" })).toBeTruthy();
}

it("coalesces repeated owner actions through preflight, confirmation and execution, then allows a new action", async () => {
  const { operation } = await start();
  expect(handoffConversationWorktree("owner-a", "Task A", "worktree")).toBe(operation);
  expect(commands).toHaveLength(1);
  await openConfirmation();
  expect(handoffConversationWorktree("owner-a", "Task A", "local")).toBe(operation);
  fireEvent.click(screen.getByRole("button", { name: "移动任务" }));
  await waitFor(() => expect(commands).toHaveLength(2));
  expect(commands[1]).toMatchObject({ type: "conversation.worktree.handoff.execute", conversation_id: "owner-a",
    fingerprint: "initial", local_branch: "main" });
  expect(handoffConversationWorktree("owner-a", "Task A", "worktree")).toBe(operation);
  await act(async () => { answer(commands[1], { completed: true }, "moved"); await operation; });
  expect(pushToast).toHaveBeenCalledExactlyOnceWith("moved", "success", 5000);
  const retry = await start();
  expect(commands).toHaveLength(3);
  await openConfirmation();
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  await retry.operation;
  expect(commands).toHaveLength(3);
});

it("uses the existing branch menu and obtains a matching fingerprint for an explicit local branch", async () => {
  const { operation } = await start();
  await openConfirmation();
  const trigger = screen.getByRole("button", { name: "本地检出分支，当前：main" });
  trigger.focus();
  fireEvent.keyDown(trigger, { key: "ArrowDown" });
  fireEvent.click(screen.getByRole("option", { name: "release" }));
  expect(screen.getByRole("button", { name: "本地检出分支，当前：release" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "移动任务" }));
  await waitFor(() => expect(commands).toHaveLength(2));
  expect(commands[1]).toMatchObject({ type: "conversation.worktree.handoff.preflight", conversation_id: "owner-a", local_branch: "release" });
  await act(async () => answer(commands[1], { ...preflight, fingerprint: "release-head", local_checkout_branch: "release" }));
  await waitFor(() => expect(commands).toHaveLength(3));
  expect(commands[2]).toMatchObject({ type: "conversation.worktree.handoff.execute", fingerprint: "release-head", local_branch: "release" });
  await act(async () => { answer(commands[2], { completed: true }); await operation; });
});

it("cancels branch selection without a new preflight or mutation", async () => {
  const { operation } = await start();
  await openConfirmation();
  fireEvent.click(screen.getByRole("button", { name: "本地检出分支，当前：main" }));
  fireEvent.click(screen.getByRole("option", { name: "release" }));
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  await operation;
  expect(commands).toHaveLength(1);
  expect(pushToast).not.toHaveBeenCalled();
});

it("reports an unavailable selected branch and never executes its obsolete fingerprint", async () => {
  const { operation } = await start();
  await openConfirmation();
  fireEvent.click(screen.getByRole("button", { name: "本地检出分支，当前：main" }));
  fireEvent.click(screen.getByRole("option", { name: "release" }));
  fireEvent.click(screen.getByRole("button", { name: "移动任务" }));
  await waitFor(() => expect(commands).toHaveLength(2));
  await act(async () => {
    answer(commands[1], { allowed: false, checks: [{ severity: "blocking", message: "Branch was checked out elsewhere" }] }, "blocked", "warning");
    await operation;
  });
  expect(commands).toHaveLength(2);
  expect(pushToast).toHaveBeenCalledExactlyOnceWith("Branch was checked out elsewhere", "warning", 6000);
});

it("keeps dirty return-local on the existing stash and confirmation chain without branch inventory", async () => {
  const { operation } = await start("owner-a", "local");
  await act(async () => answer(commands[0], { allowed: false, checks: [{ code: "source.dirty", severity: "blocking", message: "dirty" }] }, "blocked", "warning"));
  fireEvent.click(await screen.findByRole("button", { name: "暂存后继续" }));
  await waitFor(() => expect(commands).toHaveLength(2));
  expect(commands[1]).toMatchObject({ type: "conversation.worktree.handoff.preflight", target: "local", dirty_action: "stash" });
  await act(async () => answer(commands[1], { allowed: true, fingerprint: "stash", checks: [], dirty_action: "stash", reuse_branch: false }));
  expect(await screen.findByRole("dialog", { name: "将“Task A”移到本地检出？" })).toBeTruthy();
  expect(screen.queryByText("本地工作区将切换到")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "移动任务" }));
  await waitFor(() => expect(commands).toHaveLength(3));
  expect(commands[2]).toMatchObject({ type: "conversation.worktree.handoff.execute", target: "local", fingerprint: "stash", dirty_action: "stash" });
  expect(commands[2]).not.toHaveProperty("local_branch");
  await act(async () => { answer(commands[2], { completed: true }); await operation; });
});

it("releases the owner after an execution error so retry starts a fresh real preflight", async () => {
  const { operation } = await start();
  await openConfirmation();
  fireEvent.click(screen.getByRole("button", { name: "移动任务" }));
  await waitFor(() => expect(commands).toHaveLength(2));
  await act(async () => { answer(commands[1], { recovery_required: false }, "index locked", "error"); await operation; });
  expect(pushToast).toHaveBeenCalledExactlyOnceWith("index locked", "error", 6000);
  const retry = await start();
  expect(commands).toHaveLength(3);
  await act(async () => { answer(commands[2], { allowed: false, checks: [] }, "still running", "warning"); await retry.operation; });
  expect(commands).toHaveLength(3);
});

it("coalesces disconnected waiters and clears the operation for reconnection", async () => {
  const { operation } = await start();
  expect(handoffConversationWorktree("owner-a", "Task A", "worktree")).toBe(operation);
  await act(async () => { expect(rejectAllPendingCommandResults("connection closed")).toBe(1); await operation; });
  expect(pushToast).toHaveBeenCalledExactlyOnceWith("connection closed", "error", 6000);
  const retry = await start();
  await act(async () => { answer(commands[1], { allowed: false, checks: [] }, "not ready", "warning"); await retry.operation; });
  expect(commands).toHaveLength(2);
});

it("keeps action ownership per conversation instead of serializing independent tasks", async () => {
  const a = await start("owner-a");
  const b = await start("owner-b");
  expect(commands.map(command => "conversation_id" in command ? command.conversation_id : null)).toEqual(["owner-a", "owner-b"]);
  await act(async () => {
    answer(commands[0], { allowed: false, checks: [] }, "a running", "warning");
    answer(commands[1], { allowed: false, checks: [] }, "b running", "warning");
    await Promise.all([a.operation, b.operation]);
  });
});
