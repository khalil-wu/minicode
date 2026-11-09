// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { applyThreadReadState, automaticReadHolds, setThreadReadState } from "./threadReadState";

const { send } = vi.hoisted(() => ({ send: vi.fn() }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommandAwaitResult: send }));
const unread = { firstUnread: { type: "turn" as const, turnId: "first-turn" }, revision: "unread-1" };
beforeEach(() => {
  send.mockReset(); automaticReadHolds.clear();
  useAppStore.setState({ conversations: [{ id: "owner", title: "Owner", updatedAt: "original", revision: 4,
    hasUnreadTurn: true, readState: unread }], draft: "KEEP_UNREAD_DRAFT", conversationId: "other" });
});

it("rejects older receipt events after a newer directory or peer result", () => {
  const latest = { firstUnread: { type: "threadStart" as const }, revision: "new-mark" };
  applyThreadReadState("owner", latest, undefined, 8);
  applyThreadReadState("owner", { firstUnread: null, revision: "old-ack" }, undefined, 6);
  expect(useAppStore.getState().conversations[0]).toMatchObject({ readState: latest, hasUnreadTurn: true, revision: 8, updatedAt: "original" });
  expect(automaticReadHolds.has("owner")).toBe(true);
});

it("returns a conflict and current state without retrying or replacing a newer peer projection", async () => {
  let resolve!: (value: unknown) => void;
  send.mockReturnValue(new Promise(done => { resolve = done; }));
  const request = setThreadReadState("owner", false, unread.revision);
  const peer = { firstUnread: { type: "turn" as const, turnId: "first-turn" }, revision: "peer-result" };
  applyThreadReadState("owner", peer, undefined, 9);
  resolve({ type: "command.result", level: "error", data: { reason: "readStateConflict", readState: {
    firstUnread: null, revision: "conflict-snapshot" }, revision: 8 } });
  await request;
  expect(send).toHaveBeenCalledOnce();
  expect(send).toHaveBeenCalledWith({ type: "conversation.read_state.set", conversation_id: "owner",
    has_unread_turn: false, expected_revision: "unread-1" }, "conversation.read_state.set");
  expect(useAppStore.getState().conversations[0].readState).toEqual(peer);
  expect(automaticReadHolds.has("owner")).toBe(true);
  expect(useAppStore.getState()).toMatchObject({ draft: "KEEP_UNREAD_DRAFT", conversationId: "other" });
});

it("applies the current acknowledgement once and keeps explicit marks held for a new read action", async () => {
  const marked = { firstUnread: { type: "threadStart" as const }, revision: "mark-1" };
  send.mockResolvedValue({ level: "success", data: { readState: marked, revision: 5 } });
  await setThreadReadState("owner", true, unread.revision);
  expect(useAppStore.getState().conversations[0].readState).toEqual(marked);
  expect(automaticReadHolds.has("owner")).toBe(true);
  const read = { firstUnread: null, revision: "read-1" };
  send.mockResolvedValue({ level: "success", data: { readState: read, revision: 6 } });
  await setThreadReadState("owner", false, marked.revision);
  expect(useAppStore.getState().conversations[0]).toMatchObject({ hasUnreadTurn: false, readState: read, revision: 6 });
  expect(automaticReadHolds.has("owner")).toBe(false);
});
