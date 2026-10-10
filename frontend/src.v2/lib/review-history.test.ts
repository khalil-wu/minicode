import { describe, expect, it } from "vitest";
import type { ChatMessage } from "../stores/types";
import { buildReviewHistory } from "./review-history";

const patch = (path: string, value: string) => `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n@@ -100 +100 @@\n-old\n+${value}`;
const user = (id: string): ChatMessage => ({ id: `user-${id}`, role: "user", content: `请求 ${id}`, artifacts: [], timestamp: 1 });
const assistant = (id: string, turnId: string | undefined, edits: { path: string; value: string; turnId?: string }[]): ChatMessage => ({
  id, turnId, role: "assistant", content: "", artifacts: [], timestamp: 2,
  blocks: edits.map((edit, index) => ({ type: "tool_call", record: {
    id: `${id}-${index}`, name: "edit_file", args: { path: edit.path, patch: patch(edit.path, edit.value) }, turnId: edit.turnId, status: "success", startedAt: 1,
  } })),
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
