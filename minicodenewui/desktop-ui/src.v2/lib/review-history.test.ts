import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../stores/types";
import { buildReviewHistory, createReviewTranscriptSelector } from "./review-history";
import * as turnDiff from "./turn-diff";

const patch = (path: string, value: string) => `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n@@ -100 +100 @@\n-old\n+${value}`;
const user = (id: string): ChatMessage => ({ id: `user-${id}`, role: "user", content: `请求 ${id}`, artifacts: [], timestamp: 1 });
const assistant = (id: string, turnId: string | undefined, edits: { path: string; value: string; turnId?: string }[]): ChatMessage => ({
  id, turnId, role: "assistant", content: "", artifacts: [], timestamp: 2,
  blocks: edits.map((edit, index) => ({ type: "tool_call", record: {
    id: `${id}-${index}`, name: "edit_file", args: { path: edit.path, patch: patch(edit.path, edit.value) }, turnId: edit.turnId, status: "success", startedAt: 1,
  } })),
});

afterEach(() => vi.restoreAllMocks());

describe("review transcript subscription", () => {
  it("keeps historical review facts stable throughout text, thinking and stdout streaming", () => {
    const select = createReviewTranscriptSelector();
    const past = Array.from({ length: 80 }, (_, index) => [user(String(index)), assistant(`a-${index}`, `t-${index}`,
      [{ path: `file-${index}.ts`, value: `change-${index}` }])]).flat();
    let live: ChatMessage = { ...assistant("live", "live-turn", [{ path: "live.ts", value: "already applied" }]), isStreaming: true };
    const original = select([...past, user("live"), live]);
    const liveUser = original.historyMessages.at(-2)!;
    for (let index = 0; index < 100; index += 1) {
      const tool = live.blocks![0] as Extract<NonNullable<ChatMessage["blocks"]>[number], { type: "tool_call" }>;
      live = { ...live, content: `streaming reply ${index}`, blocks: [
        { ...tool, record: { ...tool.record, stdoutPreview: `output ${index}`, seq: index } },
        { type: "thinking", content: `thinking ${index}` },
        { type: "text", content: `streaming reply ${index}`, source: "reply", isStreaming: true },
      ] };
      expect(select([...past, liveUser, live])).toBe(original);
    }
    const finished = select([...past, liveUser, { ...live, isStreaming: false }]);
    expect(finished.historyMessages).toBe(original.historyMessages);
    expect(finished.commentMessages.at(-1)?.content).toBe("streaming reply 99");
    expect(finished.commentMessages).toHaveLength(81);
  });

  it("projects every actual tool change and authoritative aggregate with the same complete history", () => {
    const select = createReviewTranscriptSelector();
    const first = assistant("first", "first-turn", [{ path: "first.ts", value: "first" }]);
    let live = assistant("live", "live-turn", [{ path: "old.ts", value: "proposed" }]);
    const initial = live.blocks![0] as Extract<NonNullable<ChatMessage["blocks"]>[number], { type: "tool_call" }>;
    live = { ...live, blocks: [{ ...initial, record: { ...initial.record, status: "running" } }] };
    const messages = () => [user("first"), first, user("live"), live];
    let previous = select(messages());
    expect(buildReviewHistory(previous.historyMessages, undefined, "conv")[0].files).toEqual([]);
    const snapshots = [
      { ...initial.record, status: "success" as const },
      { ...initial.record, args: { patch: patch("renamed.ts", "corrected") } },
      { ...initial.record, turnId: "first-turn", diff: { plus: 1, minus: 1, files: [{ path: "receipt.ts", patch: "@@ -1 +1 @@\n-a\n+b", plus: 1, minus: 1 }] }, args: {} },
      { ...initial.record, temporaryRemoved: true },
      { ...initial.record, status: "failed" as const, diff: { plus: 1, minus: 1, patch: patch("partial.ts", "actual partial change") } },
    ];
    for (const record of snapshots) {
      live = { ...live, blocks: [{ ...initial, record }] };
      const next = select(messages());
      expect(next.historyMessages).not.toBe(previous.historyMessages);
      expect(buildReviewHistory(next.historyMessages, undefined, "conv")).toEqual(buildReviewHistory(messages(), undefined, "conv"));
      previous = next;
    }
    for (const diff of [patch("aggregate.ts", "final"), "", null]) {
      live = { ...live, turnDiff: { threadId: "conv", turnId: "live-turn", messageId: "live", diff, updatedAt: 1 } };
      const next = select(messages());
      expect(next.historyMessages).not.toBe(previous.historyMessages);
      expect(buildReviewHistory(next.historyMessages, undefined, "conv")).toEqual(buildReviewHistory(messages(), undefined, "conv"));
      previous = next;
    }
  });

  it("retains review input identity while observing edited scopes, pagination, removal and reordered ownership", () => {
    const select = createReviewTranscriptSelector();
    const firstUser = { ...user("first"), codeReview: { mode: "uncommitted" as const, cwd: "C:/first", gitRoot: "C:/first" } };
    const first = assistant("first", undefined, [{ path: "first.ts", value: "first" }]);
    const original = select([firstUser, first]);
    expect(select([{ ...firstUser, deliveryStatus: "sent" }, first])).toBe(original);
    const changedScope = { ...firstUser, codeReview: { ...firstUser.codeReview, cwd: "C:/next", gitRoot: "C:/next" } };
    expect(select([changedScope, first]).latestReviewInput).toBe(changedScope);
    const older = [user("older"), assistant("older", "older-turn", [{ path: "older.ts", value: "older" }])];
    const next = select([...older, { ...changedScope, content: "Changed user label" }, first]);
    expect(buildReviewHistory(next.historyMessages).map(turn => turn.label)).toEqual(["Changed user label", "请求 older"]);
    expect(next.commentMessages).toHaveLength(2);
    const trimmed = select([first, older[0], older[1]]);
    expect(trimmed.latestReviewInput).toBeUndefined();
    expect(buildReviewHistory(trimmed.historyMessages)).toEqual(buildReviewHistory([first, ...older]));
    expect(select([])).toEqual({ historyMessages: [], commentMessages: [], latestReviewInput: undefined });
  });

  it("does not reparse old tool patches when a new tool result extends history", () => {
    const parse = vi.spyOn(turnDiff, "summarizeTurnDiff");
    const past = Array.from({ length: 60 }, (_, index) => [user(`cached-${index}`), assistant(`cached-${index}`, `cached-turn-${index}`,
      [{ path: `cached-${index}.ts`, value: `value-${index}` }])]).flat();
    const before = buildReviewHistory(past);
    expect(parse).toHaveBeenCalledTimes(60);
    parse.mockClear();
    const after = buildReviewHistory([...past, user("new"), assistant("new", "new-turn", [{ path: "new.ts", value: "new" }])]);
    expect(parse).toHaveBeenCalledOnce();
    expect(after.slice(1)).toEqual(before);
  });
});

describe("review history turn ownership", () => {
  it("retains deferred historical file summaries without inventing revisions", () => {
    const answer = assistant("answer", "turn", []);
    answer.turnDiff = { threadId: "conv", turnId: "turn", messageId: "answer", revision: 4, updatedAt: 1,
      source: "workspace_snapshot", deferred: true, diff: null, truncated: true,
      files: [{ path: "src/a.ts", additions: 1, deletions: 1 }, { path: "src/b.ts", additions: 2, deletions: 0 }] };
    const [history] = buildReviewHistory([user("1"), answer], undefined, "conv");
    expect(history).toMatchObject({ source: "workspace_snapshot", truncated: true,
      deferredDiff: { conversationId: "conv", messageId: "answer", turnId: "turn", revision: 4 },
      files: [{ path: "src/a.ts", revisions: [] }, { path: "src/b.ts", revisions: [] }] });
  });
  it("restores shell and child changes for every persisted parent turn without tool receipts", () => {
    const first = assistant("a1", "t1", []);
    first.turnDiff = { threadId: "conv", turnId: "t1", messageId: "a1", updatedAt: 1,
      diff: patch("parser.py", "parser") + "\n" + patch("tests.py", "tests") + "\n" + patch("cli.py", "cli") };
    const second = assistant("a2", "t2", []);
    second.turnDiff = { threadId: "conv", turnId: "t2", messageId: "a2", updatedAt: 2, diff: patch("cli.py", "updated") };
    const history = buildReviewHistory([user("1"), first, user("2"), second], undefined, "conv");
    expect(history.map((turn) => turn.files.map((file) => file.path))).toEqual([["cli.py"], ["parser.py", "tests.py", "cli.py"]]);
    expect(history[1].files.every((file) => file.revisions.length === 1 && file.revisions[0].name === "本轮修改")).toBe(true);
  });
  it("shows only the third turn's A edits as the last turn and counts the file once", () => {
    const history = buildReviewHistory([
      user("1"), assistant("a1", "t1", [{ path: "A.ts", value: "first" }]),
      user("2"), assistant("a2", "t2", [{ path: "B.ts", value: "second" }]),
      user("3"), assistant("a3", "t3", [{ path: "A.ts", value: "third" }, { path: "A.ts", value: "fourth" }]),
    ]);
    expect(history.map((turn) => turn.id)).toEqual(["t3", "t2", "t1"]);
    expect(history[0].files.map((file) => file.path)).toEqual(["A.ts"]);
    expect(history[0].files[0].revisions.map((edit) => edit.diff)).toEqual([patch("A.ts", "third"), patch("A.ts", "fourth")]);
  });

  it("uses the tool's owner rather than replaying an old edit in the enclosing new turn", () => {
    const history = buildReviewHistory([
      user("1"), assistant("a1", "t1", [{ path: "A.ts", value: "first", turnId: "t1" }]),
      user("2"), assistant("a2", "t2", [{ path: "A.ts", value: "old replay", turnId: "t1" }]),
    ]);
    expect(history[0]).toMatchObject({ id: "t2", files: [] });
    expect(history[1].files[0].revisions).toHaveLength(2);
  });

  it("groups legacy assistant fragments beneath their user request without inventing file links", () => {
    const unknown = assistant("unknown", undefined, []);
    unknown.blocks = [{ type: "tool_call", record: { id: "unknown-tool", name: "apply_patch", args: { patch: "@@ -4 +4 @@\n-old\n+new" }, status: "success", startedAt: 1 } }];
    const history = buildReviewHistory([user("1"), assistant("a", undefined, [{ path: "A.ts", value: "a" }]), assistant("b", undefined, [{ path: "B.ts", value: "b" }]), unknown]);
    expect(history).toHaveLength(1);
    expect(history[0].files.map((file) => file.path)).toEqual(["A.ts", "B.ts", ""]);
  });

  it("uses the runtime aggregate for its exact turn and retains other turn records", () => {
    const history = buildReviewHistory([
      user("1"), assistant("a1", "t1", [{ path: "A.ts", value: "first" }]),
      user("2"), assistant("a2", "t2", [{ path: "B.ts", value: "intermediate" }]),
    ], { threadId: "conv", turnId: "t2", updatedAt: 0, diff: patch("B.ts", "final") });
    expect(history[0].files[0].revisions).toEqual([{ id: "turn-t2:B.ts", name: "本轮修改", diff: patch("B.ts", "final") }]);
    expect(history[1].files[0].revisions[0].diff).toBe(patch("A.ts", "first"));
  });
});
