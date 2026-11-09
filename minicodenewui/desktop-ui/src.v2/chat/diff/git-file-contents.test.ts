/* @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { sendClientCommandAwaitResult } from "../../protocol/ws-outbox";
import type { GitDiffSnapshot } from "../../stores/types";
import { readGitFileContents } from "./git-file-contents";

vi.mock("../../protocol/ws-outbox", () => ({
  sendClientCommandAwaitResult: vi.fn(),
  createClientCommandId: () => crypto.randomUUID(),
  commandResultSucceeded: (result: { level: string }) => result.level === "success",
}));

const owner: GitDiffSnapshot = { conversationId: "original", workspaceRoot: "C:/view", environmentId: "target",
  cwdUri: "file:///home/owned/repo", source: "unstaged", generation: "snapshot-a", hideWhitespace: false };
beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(sendClientCommandAwaitResult).mockImplementation(async command => ({ type: "command.result", command: command.type,
    level: "success", message: "", data: { source: command.source, snapshot_generation: command.snapshot_generation,
      results: (command.requests as { path: string; side: string }[]).map(request => ({ ...request,
        type: "success", source_kind: "object", lines: [request.path + "\n"] })) } }));
});
afterEach(() => { vi.useRealTimers(); vi.mocked(sendClientCommandAwaitResult).mockReset(); });

describe("Source Git object read batching", () => {
  it("waits forty milliseconds, sends at most four reads, and retains the original owner", async () => {
    const requests = Array.from({ length: 6 }, (_, index) => ({ path: `file-${index}.ts`, side: "new" as const, format: "text" as const }));
    const pending = readGitFileContents(owner, requests);
    await vi.advanceTimersByTimeAsync(39);
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    const results = await pending;
    expect(results.map(result => result.path)).toEqual(requests.map(request => request.path));
    expect(vi.mocked(sendClientCommandAwaitResult).mock.calls.map(([command]) => (command.requests as []).length)).toEqual([4, 2]);
    for (const [command] of vi.mocked(sendClientCommandAwaitResult).mock.calls) expect(command).toMatchObject({
      conversation_id: "original", workspace: "C:/view", environment_id: "target", cwd_uri: "file:///home/owned/repo",
      source: "unstaged", snapshot_generation: "snapshot-a", max_object_bytes: 5 * 1024 * 1024,
    });
  });

  it("never merges a different environment, snapshot, or source", async () => {
    const request = [{ path: "same.ts", side: "old" as const, format: "text" as const }];
    const reads = [owner, { ...owner, environmentId: "other" }, { ...owner, generation: "snapshot-b" },
      { ...owner, source: "staged" as const }].map(owner => readGitFileContents(owner, request));
    await vi.advanceTimersByTimeAsync(40);
    await Promise.all(reads);
    expect(sendClientCommandAwaitResult).toHaveBeenCalledTimes(4);
  });

  it("keeps stale versions unavailable and propagates real command failures without another host", async () => {
    vi.mocked(sendClientCommandAwaitResult).mockResolvedValueOnce({ type: "command.result", command: "diff.git_file_contents",
      level: "success", message: "", data: { status: "stale-snapshot", source: "unstaged", snapshot_generation: "snapshot-a" } });
    const request = [{ path: "same.ts", side: "old" as const, format: "text" as const }];
    const stale = readGitFileContents(owner, request);
    await vi.advanceTimersByTimeAsync(40);
    expect(await stale).toEqual([{ type: "error", path: "same.ts", side: "old", error: { type: "stale-snapshot" } }]);
    vi.mocked(sendClientCommandAwaitResult).mockResolvedValueOnce({ type: "command.result", command: "diff.git_file_contents",
      level: "error", message: "original host disconnected" });
    const failed = readGitFileContents(owner, request);
    const assertion = expect(failed).rejects.toThrow("original host disconnected");
    await vi.advanceTimersByTimeAsync(40);
    await assertion;
    expect(sendClientCommandAwaitResult).toHaveBeenCalledTimes(2);
  });
});
