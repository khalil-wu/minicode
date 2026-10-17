/* @vitest-environment jsdom */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it } from "vitest";
import { clearGitReviewSelection, useGitReviewSelection } from "./git-review-selection";

beforeEach(() => {
  localStorage.removeItem("last-explicit-review-diff-filter");
  for (const id of ["review-owner-a", "review-owner-b"]) clearGitReviewSelection(id);
});
afterEach(cleanup);

it("keeps real branch and commit selection with its original thread, while source alone supplies the next thread's preference", () => {
  const hook = renderHook(({ owner, cwd }) => useGitReviewSelection(owner, cwd),
    { initialProps: { owner: "review-owner-a", cwd: "C:/workspace-a" } });
  expect(hook.result.current[0]).toEqual({ source: "branch" });
  act(() => hook.result.current[1]({ source: "uncommitted" }, true));
  act(() => hook.result.current[1]({ source: "commit", baseBranch: "origin/release", commitSha: "actual-sha" }, true));
  hook.rerender({ owner: "review-owner-b", cwd: "C:/workspace-b" });
  expect(hook.result.current[0]).toEqual({ source: "uncommitted" });
  act(() => hook.result.current[1]({ source: "unstaged" }, true));
  hook.rerender({ owner: "review-owner-a", cwd: "C:/workspace-a" });
  expect(hook.result.current[0]).toEqual({ source: "commit", baseBranch: "origin/release", commitSha: "actual-sha" });
});

it("clears the original thread's ref choices synchronously when its repository changes, and never restores those old refs", () => {
  const observed: unknown[] = [];
  const hook = renderHook(({ cwd }) => { const value = useGitReviewSelection("review-owner-a", cwd); observed.push({ cwd, ...value[0] }); return value; },
    { initialProps: { cwd: "C:/workspace-a" } });
  act(() => hook.result.current[1]({ source: "branch", baseBranch: "real-base", commitSha: "old-commit" }));
  hook.rerender({ cwd: "C:/workspace-b" });
  expect(observed.filter((entry: any) => entry.cwd === "C:/workspace-b")).toEqual(expect.arrayContaining([{ cwd: "C:/workspace-b", source: "branch" }]));
  expect(observed.filter((entry: any) => entry.cwd === "C:/workspace-b").every((entry: any) => !entry.baseBranch && !entry.commitSha)).toBe(true);
  hook.rerender({ cwd: "C:/workspace-a" });
  expect(hook.result.current[0]).toEqual({ source: "branch" });
});

it("retires a deleted conversation and uses the current global source when it is visited again", () => {
  const hook = renderHook(() => useGitReviewSelection("review-owner-a", "C:/workspace-a"));
  act(() => hook.result.current[1]({ source: "branch", baseBranch: "old-base" }));
  localStorage.setItem("last-explicit-review-diff-filter", '"last-turn"');
  act(() => clearGitReviewSelection("review-owner-a"));
  expect(hook.result.current[0]).toEqual({ source: "last-turn" });
});
