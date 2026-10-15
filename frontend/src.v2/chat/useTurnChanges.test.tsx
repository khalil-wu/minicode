/* @vitest-environment jsdom */
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
vi.hoisted(() => {
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
});
import { useAppStore } from "../stores";
import { useTurnChanges } from "./useTurnChanges";
import { pushToast } from "../overlays/ToastContainer";
import type { TurnDiffState } from "../stores/types";
vi.mock("../protocol/api", async load => ({ ...await load<typeof import("../protocol/api")>(), apiBase: () => "http://diff.test" }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "diff-owner" }) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.clearAllMocks(); });

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

const historicalPatch = "diff --git a/main.ts b/main.ts\n--- a/main.ts\n+++ b/main.ts\n@@ -1 +1 @@\n-before\n+after\n";
const seedDeferredReview = () => {
  const diff: TurnDiffState = { threadId: "A", turnId: "turn-A", messageId: "assistant-A", revision: 3,
    updatedAt: 1, deferred: true, diff: null, files: [{ path: "main.ts", additions: 1, deletions: 1 }] };
  useAppStore.setState({ conversationId: "A", workingDirectory: "C:/A", messages: [{ id: "assistant-A",
    role: "assistant", content: "Done", artifacts: [], timestamp: 1, turnId: "turn-A", turnDiff: diff }],
    conversations: [{ id: "A", title: "A", updatedAt: "", workspaceRoot: "C:/A" }],
    conversationMessages: {}, conversationStreaming: {}, conversationHistoryPages: {}, isStreaming: false,
    turnDiffs: { A: diff }, diffReview: null, gitReviewRequest: null });
};
const loadedResponse = () => new Response(JSON.stringify({ conversation_id: "A", thread_id: "A",
  message_id: "assistant-A", turn_id: "turn-A", revision: 3, diff: historicalPatch }), { status: 200 });

it("publishes complete historical files and the browser return target together after the real loader finishes", async () => {
  seedDeferredReview();
  let resolve!: (response: Response) => void;
  const fetch = vi.fn((_url: RequestInfo | URL, _init?: RequestInit) => new Promise<Response>(done => { resolve = done; }));
  vi.stubGlobal("fetch", fetch);
  const target = { conversationId: "A", tab: "browser" as const, url: "https://example.com", targetId: "page-A" };
  const hook = renderHook(() => useTurnChanges(target));
  const publications: Array<NonNullable<ReturnType<typeof useAppStore.getState>["diffReview"]>> = [];
  const unsubscribe = useAppStore.subscribe(state => { if (state.diffReview) publications.push(state.diffReview); });
  act(() => hook.result.current.openReview());
  expect(String(fetch.mock.calls[0][0])).toContain("/api/conversations/A/messages/assistant-A/turn-diff?");
  expect(useAppStore.getState().diffReview).toBeNull();
  await act(async () => resolve(loadedResponse()));
  await waitFor(() => expect(useAppStore.getState().diffReview?.files[0].patch).toContain("+after"));
  expect(publications.length).toBeGreaterThan(0);
  expect(publications.every(review => review.files.length === 1 && review.previewReturnTarget === target)).toBe(true);
  expect(useAppStore.getState().messages[0].turnDiff?.deferred).toBe(false);
  unsubscribe();
});

it("does not open an old browser review after the reader switches conversation while HTTP is pending", async () => {
  seedDeferredReview();
  let resolve!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(done => { resolve = done; })));
  const hook = renderHook(() => useTurnChanges({ conversationId: "A", tab: "browser" }));
  act(() => hook.result.current.openReview());
  act(() => useAppStore.setState({ conversationId: "B", messages: [], turnDiffs: {} }));
  await act(async () => resolve(loadedResponse()));
  expect(useAppStore.getState().diffReview).toBeNull();
  expect(useAppStore.getState().conversationId).toBe("B");
  expect(pushToast).not.toHaveBeenCalled();
});

it("reports historical loading failures without publishing an incomplete review", async () => {
  seedDeferredReview();
  vi.stubGlobal("fetch", vi.fn(async () => new Response("retained patch unavailable", { status: 500 })));
  const hook = renderHook(() => useTurnChanges({ conversationId: "A", tab: "browser" }));
  act(() => hook.result.current.openReview());
  await waitFor(() => expect(pushToast).toHaveBeenCalledWith("retained patch unavailable", "error"));
  expect(useAppStore.getState().diffReview).toBeNull();
  expect(useAppStore.getState().messages[0].turnDiff?.deferred).toBe(true);
});
