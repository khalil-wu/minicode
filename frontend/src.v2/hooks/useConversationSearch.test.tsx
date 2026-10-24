/* @vitest-environment jsdom */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { useConversationSearch } from "./useConversationSearch";

const { search, connection } = vi.hoisted(() => ({ search: vi.fn(), connection: { sessionId: "owner-A" } }));
vi.mock("./useWebSocket", () => ({ getWebSocket: () => connection }));
vi.mock("../protocol/conversation-search", () => ({ searchConversations: search }));

const hit = (id: string) => ({ conversation: { id, title: id, archived: false }, message_id: `message-${id}`, snippet: "body needle" });
const tick = async () => { await act(async () => { await vi.advanceTimersByTimeAsync(200); }); };

beforeEach(() => {
  vi.useFakeTimers(); vi.clearAllMocks(); connection.sessionId = "owner-A";
  useAppStore.setState({ isConnected: true });
  search.mockResolvedValue({ data: [], next_cursor: null });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

it("debounces typing and ignores a late response after query replacement and close", async () => {
  let finishOld!: (value: unknown) => void;
  search.mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve; }));
  const view = renderHook(({ query, enabled }) => useConversationSearch({ query, enabled }), { initialProps: { query: "old", enabled: true } });
  await tick();
  const oldSignal = search.mock.calls[0][0].signal as AbortSignal;
  view.rerender({ query: "n", enabled: true });
  view.rerender({ query: "new", enabled: true });
  expect(oldSignal.aborted).toBe(true);
  expect(view.result.current.results).toEqual([]);
  search.mockResolvedValueOnce({ data: [hit("new")], next_cursor: null });
  await tick();
  await act(async () => { finishOld({ data: [hit("old")], next_cursor: null }); });
  expect(search).toHaveBeenCalledTimes(2);
  expect(search.mock.calls[1][0].query).toBe("new");
  expect(view.result.current.results.map(result => result.conversation.id)).toEqual(["new"]);
  view.rerender({ query: "new", enabled: false });
  expect((search.mock.calls[1][0].signal as AbortSignal).aborted).toBe(true);
  expect(view.result.current.results).toEqual([]);
  await tick();
  expect(search).toHaveBeenCalledTimes(2);
});

it("follows real cursors across empty scan pages and fills only the remaining result quota", async () => {
  search.mockResolvedValueOnce({ data: [], next_cursor: "scan-next" })
    .mockResolvedValueOnce({ data: [hit("first"), hit("second")], next_cursor: "matches-next" })
    .mockResolvedValueOnce({ data: Array.from({ length: 48 }, (_, i) => hit(`page-${i}`)), next_cursor: "more" })
    .mockResolvedValueOnce({ data: [hit("last")], next_cursor: null });
  const view = renderHook(() => useConversationSearch({ query: "needle", enabled: true, archived: true, project: "C:/alpha", sort: "created" }));
  await tick();
  expect(search.mock.calls.map(call => [call[0].cursor, call[0].limit])).toEqual([["", 50], ["scan-next", 50], ["matches-next", 48]]);
  expect(search.mock.calls[2][0]).toMatchObject({ sessionId: "owner-A", archived: true, project: "C:/alpha", sort: "created" });
  expect(view.result.current.results).toHaveLength(50);
  await act(async () => { view.result.current.loadMore(); });
  expect(search.mock.calls[3][0].cursor).toBe("more");
  expect(view.result.current.results).toHaveLength(51);
  expect(view.result.current.hasMore).toBe(false);
});

it("aborts a previous session and keeps its late response out of the new owner", async () => {
  let finishOld!: (value: unknown) => void;
  search.mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve; }));
  const view = renderHook(() => useConversationSearch({ query: "needle", enabled: true }));
  await tick();
  connection.sessionId = "owner-B";
  view.rerender();
  search.mockResolvedValueOnce({ data: [hit("B")], next_cursor: null });
  await tick();
  await act(async () => { finishOld({ data: [hit("A")], next_cursor: null }); });
  expect((search.mock.calls[0][0].signal as AbortSignal).aborted).toBe(true);
  expect(search.mock.calls[1][0].sessionId).toBe("owner-B");
  expect(view.result.current.results.map(result => result.conversation.id)).toEqual(["B"]);
});

it("shows actual request errors and does not turn failure into an empty successful page", async () => {
  search.mockRejectedValueOnce(new Error("stored transcript is unreadable"));
  const view = renderHook(() => useConversationSearch({ query: "needle", enabled: true }));
  await tick();
  expect(view.result.current.error).toBe("stored transcript is unreadable");
  expect(view.result.current.loading).toBe(false);
  expect(view.result.current.results).toEqual([]);
});

it("does not request bodies when the feature or surface is disabled", async () => {
  const view = renderHook(() => useConversationSearch({ query: "needle", enabled: false }));
  await tick();
  expect(search).not.toHaveBeenCalled();
  expect(view.result.current.loading).toBe(false);
});
