import { afterEach, expect, it, vi } from "vitest";
import { copyGitApplyCommand } from "./copy-git-apply-command";
import { copyText } from "../../lib/clipboard";
import { sendClientCommandAwaitResult } from "../../protocol/ws-outbox";

vi.mock("../../lib/clipboard", () => ({ copyText: vi.fn(async () => true) }));
vi.mock("../../protocol/ws-outbox", () => ({
  sendClientCommandAwaitResult: vi.fn(), commandResultSucceeded: (value: { level: string }) => value.level === "success",
}));
afterEach(() => vi.clearAllMocks());

it("copies the full selected source, including binary patch data, without executing it", async () => {
  const patch = "diff --git a/image.bin b/image.bin\nGIT binary patch\nliteral 3\nabc\n";
  vi.mocked(sendClientCommandAwaitResult).mockResolvedValue({ type: "command.result", command: "diff.git_review_patch", level: "success", message: "", data: { unified_diff: patch } });
  await copyGitApplyCommand("owner-A", "/workspace-A", { source: "branch", baseBranch: "origin/main" }, "Copied");
  expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({ type: "diff.git_review_patch",
    conversation_id: "owner-A", workspace: "/workspace-A", source: "branch", base_branch: "origin/main" }), "diff.git_review_patch", { silent: true });
  expect(copyText).toHaveBeenCalledWith(expect.stringContaining(`git apply --3way <<'EOF'\n${patch}\nEOF`), "git apply", "Copied");
});

it("keeps the clipboard unchanged when the selected owner cannot produce its patch", async () => {
  vi.mocked(sendClientCommandAwaitResult).mockResolvedValue({ type: "command.result", command: "diff.git_review_patch", level: "error", message: "Executor disconnected" });
  await expect(copyGitApplyCommand("owner-A", "/workspace-A", { source: "commit", commitSha: "abc" }, "Copied")).rejects.toThrow("Executor disconnected");
  expect(copyText).not.toHaveBeenCalled();
});
