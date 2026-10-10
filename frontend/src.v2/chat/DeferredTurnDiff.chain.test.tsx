// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage, TurnDiffState } from "../stores/types";
import type { DiffCellState } from "./cells/cellTypes";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), toast: vi.fn() }));
vi.mock("../protocol/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../protocol/api")>(),
  apiBase: () => "http://127.0.0.1:8000", authHeaders: () => new Headers({ "X-MiniCode-Token": "test-token" }),
  fetchWithTimeout: mocks.fetch,
}));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "socket-owner" }) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));

import { useAppStore } from "../stores";
import { DiffCell } from "./cells/DiffCell";
import { hydrateMessages } from "./transcriptHydration";
import { applyAuthoritativeTurnDiff } from "../lib/turn-diff";
import { buildReviewHistory } from "../lib/review-history";

const patch = (path: string) => `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n@@ -1 +1 @@\n-old\n+new\n`;
const fullPatch = patch("src/a.ts") + patch("src/b.ts");
const deferredState = (extra: Partial<TurnDiffState> = {}): TurnDiffState => ({
  threadId: "A", turnId: "turn-a", messageId: "answer-a", revision: 3, updatedAt: 1,
  diff: null, deferred: true, files: [
    { path: "src/a.ts", additions: 1, deletions: 1 },
    { path: "src/b.ts", additions: 1, deletions: 1 },
  ], ...extra,
});
const card = (state: TurnDiffState): DiffCellState => ({
  kind: "diff", id: "turn-diff-turn-a", status: "updated", collapsed: false, createdAt: 1,
  deferredDiff: { conversationId: "A", turnId: "turn-a", messageId: "answer-a", revision: state.revision },
  source: state.source, truncated: state.truncated, files: state.files!, summary: { added: 2, deleted: 2, modifiedFiles: 2 },
});
const message = (state: TurnDiffState): ChatMessage => ({
  id: "answer-a", role: "assistant", turnId: "turn-a", content: "Done", timestamp: 1, artifacts: [], turnDiff: state,
});
const response = (extra: Record<string, unknown> = {}) => ({ ok: true, json: async () => ({
  conversation_id: "A", thread_id: "A", message_id: "answer-a", turn_id: "turn-a", revision: 3, diff: fullPatch, ...extra,
}) });
const seed = (state = deferredState()) => {
  useAppStore.setState({ conversationId: "A", workingDirectory: "C:/A", messages: [message(state)],
    conversationMessages: {}, conversationStreaming: {}, conversationHistoryPages: {},
    conversations: [{ id: "A", title: "任务 A", updatedAt: "2026-10-10", workspaceRoot: "C:/A" }],
    turnDiffs: { A: state }, diffReview: null, isStreaming: false });
  return state;
};

beforeEach(() => { vi.clearAllMocks(); mocks.fetch.mockResolvedValue(response()); seed(); });
afterEach(cleanup);

describe("deferred historical turn diff", () => {
  it("hydrates and projects file summaries without requesting their patch", () => {
    const [answer] = hydrateMessages([{ id: "answer-a", role: "assistant", turn_id: "turn-a", metadata: { turn_diff: {
      thread_id: "A", conversation_id: "A", message_id: "answer-a", turn_id: "turn-a", revision: 3,
      source: "workspace_snapshot", diff: null, deferred: true, files: [{ path: "src/a.ts", additions: 1, deletions: 1 }],
    } } }]);
    expect(answer.turnDiff).toMatchObject({ deferred: true, source: "workspace_snapshot", files: [{ path: "src/a.ts" }] });
    const projected = applyAuthoritativeTurnDiff([{ id: answer.id, turnId: answer.turnId!, startedAt: 1,
      committedCells: [] } as unknown as Parameters<typeof applyAuthoritativeTurnDiff>[0][number]], answer.turnDiff);
    expect(projected[0].committedCells[0]).toMatchObject({ source: "workspace_snapshot", files: [{ path: "src/a.ts" }],
      deferredDiff: { conversationId: "A", messageId: "answer-a", turnId: "turn-a", revision: 3 } });
    render(<DiffCell cell={card(seed(answer.turnDiff!))} conversationId="A" workspaceRoot="C:/A" />);
    expect(screen.getByText("工作区比较 1 个文件")).toBeTruthy();
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it.each(["查看变更", "src/b.ts"])("loads only on %s and retains all files in review and history", async (action) => {
    render(<DiffCell cell={card(deferredState())} conversationId="A" workspaceRoot="C:/A" />);
    expect(mocks.fetch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: action }));
    await waitFor(() => expect(useAppStore.getState().diffReview?.files).toHaveLength(2));
    const [url, init] = mocks.fetch.mock.calls[0];
    expect(String(url)).toContain("/api/conversations/A/messages/answer-a/turn-diff?");
    expect(url.searchParams.get("session_id")).toBe("socket-owner");
    expect(url.searchParams.get("turn_id")).toBe("turn-a");
    expect(url.searchParams.get("revision")).toBe("3");
    expect(init.headers.get("X-MiniCode-Token")).toBe("test-token");
    expect(useAppStore.getState().diffReview?.selectedPath).toBe(action === "src/b.ts" ? "src/b.ts" : "src/a.ts");
    expect(useAppStore.getState().messages[0].turnDiff).toMatchObject({ deferred: false, diff: fullPatch });
    expect(buildReviewHistory(useAppStore.getState().messages, undefined, "A")[0].files.map((file) => file.path)).toEqual(["src/a.ts", "src/b.ts"]);
    expect(mocks.fetch).toHaveBeenCalledTimes(1);
  });

  it.each(["switch", "delete", "revision", "live revision"])("does not install or open a late response after %s", async (change) => {
    let finish!: (value: ReturnType<typeof response>) => void;
    mocks.fetch.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    render(<DiffCell cell={card(deferredState())} conversationId="A" workspaceRoot="C:/A" />);
    fireEvent.click(screen.getByRole("button", { name: "查看变更" }));
    act(() => {
      if (change === "switch") useAppStore.setState({ conversationId: "B", workingDirectory: "C:/B", messages: [] });
      else if (change === "delete") useAppStore.setState({ messages: [], conversationMessages: {}, conversations: [] });
      else if (change === "live revision") useAppStore.getState().setTurnDiff("A", { ...deferredState({ revision: 4 }), diff: patch("src/new.ts"), deferred: false });
      else useAppStore.setState({ messages: [message(deferredState({ revision: 4 }))] });
    });
    expect(mocks.fetch.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => finish(response()));
    expect(useAppStore.getState().diffReview).toBeNull();
    expect(useAppStore.getState().messages.every((entry) => entry.turnDiff?.diff !== fullPatch)).toBe(true);
    expect(mocks.toast).not.toHaveBeenCalled();
  });

  it("cancels the request when the owning cell is unmounted", async () => {
    let finish!: (value: ReturnType<typeof response>) => void;
    mocks.fetch.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const view = render(<DiffCell cell={card(deferredState())} conversationId="A" workspaceRoot="C:/A" />);
    fireEvent.click(screen.getByRole("button", { name: "查看变更" }));
    view.unmount();
    expect(mocks.fetch.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => finish(response()));
    expect(useAppStore.getState().messages[0].turnDiff?.deferred).toBe(true);
    expect(useAppStore.getState().diffReview).toBeNull();
  });

  it.each(["thread_id", "message_id", "turn_id", "revision"])("reports a mismatching %s response without installing it", async (field) => {
    mocks.fetch.mockResolvedValue(response({ [field]: field === "revision" ? 4 : "foreign" }));
    render(<DiffCell cell={card(deferredState())} conversationId="A" workspaceRoot="C:/A" />);
    fireEvent.click(screen.getByRole("button", { name: "查看变更" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("不匹配"));
    expect(useAppStore.getState().messages[0].turnDiff?.deferred).toBe(true);
    expect(useAppStore.getState().diffReview).toBeNull();
  });

  it("shows the failed request and allows an explicit retry", async () => {
    mocks.fetch.mockResolvedValueOnce({ ok: false, status: 503, statusText: "Unavailable", text: async () => '{"detail":"历史差异读取失败"}' });
    render(<DiffCell cell={card(deferredState())} conversationId="A" workspaceRoot="C:/A" />);
    fireEvent.click(screen.getByRole("button", { name: "查看变更" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("历史差异读取失败"));
    expect(useAppStore.getState().messages[0].turnDiff?.deferred).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "查看变更" }));
    await waitFor(() => expect(useAppStore.getState().diffReview?.files).toHaveLength(2));
    expect(mocks.fetch).toHaveBeenCalledTimes(2);
  });

  it("keeps the historical workspace attribution and truncation warning in review", async () => {
    const state = seed(deferredState({ source: "workspace_snapshot", truncated: true }));
    mocks.fetch.mockResolvedValue(response({ source: "workspace_snapshot", truncated: true }));
    render(<DiffCell cell={card(state)} conversationId="A" workspaceRoot="C:/A" />);
    expect(screen.getByText("工作区比较 2 个文件")).toBeTruthy();
    expect(screen.getByText(/不完整历史Diff/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "撤销" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "查看变更" }));
    await waitFor(() => expect(useAppStore.getState().diffReview).toMatchObject({ toolName: "工作区比较", truncated: true, mode: "view" }));
    const history = buildReviewHistory(useAppStore.getState().messages, undefined, "A");
    expect(history[0]).toMatchObject({ source: "workspace_snapshot", truncated: true });
    expect(history[0].files[0].revisions[0].name).toBe("工作区比较");
  });
});
