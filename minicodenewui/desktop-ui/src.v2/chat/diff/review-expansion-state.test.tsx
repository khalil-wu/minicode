/* @vitest-environment jsdom */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it } from "vitest";
import { clearReviewExpansion, useReviewExpansion } from "./review-expansion-state";
beforeEach(() => { clearReviewExpansion("expansion-a"); clearReviewExpansion("expansion-b"); });
afterEach(cleanup);

it("starts expanded, retains actual per-file overrides in its original route, and never stores them globally", () => {
  const stored = { ...localStorage };
  const hook = renderHook(({ owner }) => useReviewExpansion(owner), { initialProps: { owner: "expansion-a" } });
  expect(hook.result.current.state.expanded).toBe(true);
  act(() => hook.result.current.setFileOpen("same.ts", false));
  act(() => hook.result.current.setActivePath("same.ts"));
  hook.rerender({ owner: "expansion-b" });
  expect(hook.result.current.state.overrides.size).toBe(0);
  hook.rerender({ owner: "expansion-a" });
  expect(hook.result.current.state.overrides.get("same.ts")).toBe(false);
  expect(hook.result.current.state.activePath).toBe("same.ts");
  expect({ ...localStorage }).toEqual(stored);
});

it("clears per-file overrides on the real expand-all action, and focus opens only the requested file", () => {
  const hook = renderHook(() => useReviewExpansion("expansion-a"));
  act(() => hook.result.current.setFileOpen("a.ts", false));
  act(() => hook.result.current.setExpanded(false));
  expect(hook.result.current.state).toMatchObject({ expanded: false, overrides: new Map() });
  act(() => hook.result.current.focusFile("b.ts"));
  expect(hook.result.current.state).toEqual({ expanded: false, overrides: new Map([["b.ts", true]]), activePath: "b.ts" });
  act(() => hook.result.current.setExpanded(true));
  expect(hook.result.current.state).toEqual({ expanded: true, overrides: new Map(), activePath: "b.ts" });
});
