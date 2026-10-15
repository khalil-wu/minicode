import { expect, it } from "vitest";
import { summarizeTurnDiff } from "./turn-diff";
import type { TurnDiffState } from "../stores/types";

it("reuses one diff summary across unrelated text deltas and recomputes a new diff revision", () => {
  const diff = "diff --git a/main.ts b/main.ts\n--- a/main.ts\n+++ b/main.ts\n@@ -1 +1 @@\n-before\n+after\n";
  const state: TurnDiffState = { threadId: "thread", turnId: "turn", updatedAt: 1, diff };
  const first = summarizeTurnDiff(state);
  for (let index = 0; index < 1000; index++) expect(summarizeTurnDiff(state)).toBe(first);
  expect(first).toMatchObject({ additions: 1, deletions: 1 });
  expect(summarizeTurnDiff({ ...state, updatedAt: 2, diff: diff + "+another\n" })).not.toBe(first);
});
