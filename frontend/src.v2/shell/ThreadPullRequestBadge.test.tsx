/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ThreadPullRequestBadge, useThreadPullRequestStatuses, type LinkedPullRequest } from "./ThreadPullRequestBadge";
import type { ConversationMeta } from "../stores/types";

const mocks = vi.hoisted(() => ({ request: vi.fn(), open: vi.fn(), sessionId: "original-session" }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: mocks.sessionId }) }));
vi.mock("../protocol/thread-metadata", () => ({ threadMetadataRequest: mocks.request }));
vi.mock("../desktop/runtime", () => ({ openExternal: mocks.open }));
const pr = (number = 1, state: LinkedPullRequest["state"] = "OPEN", isDraft = false): LinkedPullRequest => ({
  number, title: `Actual PR ${number}`, state, isDraft, url: `https://github.enterprise/owner/repo/pull/${number}`,
  headRefName: "original-branch", statusCheckRollup: [],
});
const owner = (id = "original"): ConversationMeta => ({ id, title: id, updatedAt: "2026-10-21", workspaceRoot: "C:/original",
  gitBranch: "original-branch", resourceReferences: [3, 2, 1].map(number => ({ id: `resource-${number}`, attachment_type: "pull_request",
    identity_key: String(number), payload: { url: pr(number).url, root: "C:/original" }, created_at: number })) });
beforeEach(() => { vi.clearAllMocks(); mocks.sessionId = "original-session"; });
afterEach(() => cleanup());

it("shares a visible owner's query across duplicate rows and chooses a real open PR over a newer closed or draft PR", async () => {
  mocks.request.mockImplementation(async (path: string) => ({ pr: path.includes("resource-3") ? pr(3, "CLOSED") : path.includes("resource-2") ? pr(2, "OPEN", true) : pr(1) }));
  const original = owner();
  const hook = renderHook(() => useThreadPullRequestStatuses([original, original], true));
  await waitFor(() => expect(hook.result.current.original?.pr?.number).toBe(1));
  expect(mocks.request).toHaveBeenCalledTimes(3);
  for (const [path, options, sessionId] of mocks.request.mock.calls) {
    expect(path).toContain("/original/resources/resource-");
    expect(options.signal).toBeInstanceOf(AbortSignal);
    expect(sessionId).toBe("original-session");
  }
});

it("withdraws an old owner's late response after the visible conversation and session change", async () => {
  let complete!: (value: unknown) => void;
  mocks.request.mockImplementation((path: string) => path.includes("/original/")
    ? new Promise(resolve => { complete = resolve; }) : Promise.resolve({ pr: pr(9) }));
  const first = { ...owner(), resourceReferences: owner().resourceReferences!.slice(0, 1) };
  const second = { ...owner("second"), resourceReferences: owner().resourceReferences!.slice(0, 1) };
  const hook = renderHook(({ current }) => useThreadPullRequestStatuses([current], true), { initialProps: { current: first } });
  await waitFor(() => expect(mocks.request).toHaveBeenCalledOnce());
  const signal = mocks.request.mock.calls[0][1].signal;
  mocks.sessionId = "second-session";
  hook.rerender({ current: second });
  await waitFor(() => expect(hook.result.current.second?.pr?.number).toBe(9));
  await act(async () => complete({ pr: pr(3) }));
  expect(signal.aborted).toBe(true);
  expect(hook.result.current.original).toBeUndefined();
  expect(mocks.request.mock.calls[1][2]).toBe("second-session");
});

it("retains actual lookup failure without inventing a successful PR badge", async () => {
  mocks.request.mockRejectedValue(new Error("Original host permission denied"));
  const hook = renderHook(() => useThreadPullRequestStatuses([owner()], true));
  await waitFor(() => expect(hook.result.current.original?.error).toBe("Original host permission denied"));
  expect(hook.result.current.original.pr).toBeNull();
});

it.each([
  [pr(1), "开放的 PR"], [pr(2, "OPEN", true), "草稿 PR"], [pr(3, "MERGED"), "已合并 PR"], [pr(4, "CLOSED"), "已关闭 PR"],
] as const)("renders the upstream status and opens its original URL: %s", (status, label) => {
  render(<ThreadPullRequestBadge pr={status} />);
  fireEvent.click(screen.getByRole("button", { name: `${label}: ${status.title}` }));
  expect(mocks.open).toHaveBeenCalledExactlyOnceWith(status.url);
});
