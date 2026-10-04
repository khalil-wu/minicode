/* @vitest-environment jsdom */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
vi.hoisted(() => {
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
});
import { useAppStore } from "../stores";
import { useTurnChanges } from "./useTurnChanges";
afterEach(cleanup);

it("explicitly reopens the same turn after Git review without leaving a competing Git request", () => {
  useAppStore.setState({ conversationId: "A", workingDirectory: "C:/A", gitReviewRequest: null,
    messages: [{ id: "assistant-A", role: "assistant", content: "", artifacts: [], timestamp: 1, turnId: "turn-A" }],
    turnDiffs: { A: { threadId: "A", turnId: "turn-A", updatedAt: 1,
      diff: "diff --git a/main.ts b/main.ts\n--- a/main.ts\n+++ b/main.ts\n@@ -1 +1 @@\n-before\n+after\n" } } });
  const hook = renderHook(useTurnChanges);
  act(() => hook.result.current.openReview());
  const requestId = useAppStore.getState().diffReview!.requestId;
  act(() => useAppStore.getState().openGitReview({ path: "other.ts", section: "working", workspaceRoot: "C:/A", conversationId: "A" }));
  expect(useAppStore.getState().gitReviewRequest?.path).toBe("other.ts");
  act(() => hook.result.current.openReview());
  expect(useAppStore.getState().gitReviewRequest).toBeNull();
  expect(useAppStore.getState().diffReview?.requestId).toBe(requestId);
  expect(useAppStore.getState().diffReview?.selectedPath).toBe("main.ts");
});
