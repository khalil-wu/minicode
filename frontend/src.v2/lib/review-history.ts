import type { ChatMessage, TurnDiffState } from "../stores/types";
import { getToolCallsFromMessage } from "./content-blocks";
import { summarizeTurnDiff } from "./turn-diff";
import { extractFilePathFromDiff, parseUnifiedDiffLines } from "./unified-diff";
import type { DeferredTurnDiffOwner } from "../chat/loadMessageTurnDiff";

export interface HistoryDiffFile {
  path: string;
  additions?: number;
  deletions?: number;
  revisions: { id: string; name: string; diff: string }[];
}

export interface HistoryDiffTurn {
  id: string;
  label: string;
  files: HistoryDiffFile[];
  source?: string;
  truncated?: boolean;
  deferredDiff?: DeferredTurnDiffOwner;
}

/** The tool's turn identity takes precedence over its enclosing transcript message. */
export function buildReviewHistory(messages: ChatMessage[], turnDiff?: TurnDiffState, conversationId?: string): HistoryDiffTurn[] {
  const turns = new Map<string, HistoryDiffTurn>();
  let userId = "";
  let userLabel = "";
  const ensureTurn = (id: string, label: string) => {
    let turn = turns.get(id);
    if (!turn) {
      turn = { id, label, files: [] };
      turns.set(id, turn);
    }
    return turn;
  };
  for (const message of messages) {
    if (message.role === "user") {
      userId = message.id;
      userLabel = message.content.replace(/\s+/g, " ").trim().slice(0, 48);
      continue;
    }
    if (message.role !== "assistant") continue;
    const messageTurnId = message.turnId || userId || message.id;
    ensureTurn(messageTurnId, userLabel);
    for (const tool of getToolCallsFromMessage(message)) {
      if (tool.temporaryRemoved) continue;
      if (tool.status === "pending" || tool.status === "running") continue;
      if (tool.status !== "success" && !tool.diff?.patch && !tool.diff?.files?.some((file) => file.patch)) continue;
      const turn = ensureTurn(tool.turnId || messageTurnId, userLabel);
      const candidate = tool.diff?.patch ?? tool.args.diff ?? tool.args.patch;
      const diff = typeof candidate === "string" ? candidate : "";
      const summary = summarizeTurnDiff({ threadId: "", turnId: turn.id, diff, updatedAt: 0 });
      const argumentPath = tool.args.path ?? tool.args.file_path;
      const filePatches = tool.diff?.files?.filter((file) => file.patch).map((file) => ({ path: file.path, patch: file.patch! }));
      const patches = summary?.files ?? (filePatches?.length ? filePatches : diff.includes("\n") ? [{
        path: extractFilePathFromDiff(parseUnifiedDiffLines(diff)) || (typeof argumentPath === "string" ? argumentPath : ""),
        patch: diff,
      }] : []);
      for (const patch of patches) {
        let file = turn.files.find((entry) => entry.path === patch.path);
        if (!file) {
          file = { path: patch.path, revisions: [] };
          turn.files.push(file);
        }
        if (!file.revisions.some((revision) => revision.id === tool.id)) {
          file.revisions.push({ id: tool.id, name: tool.name, diff: patch.patch! });
        }
      }
    }
    const persisted = message.turnDiff;
    if (persisted && persisted.turnId === messageTurnId && (!conversationId || persisted.threadId === conversationId)) {
      const turn = ensureTurn(messageTurnId, userLabel);
      turn.source = persisted.source;
      turn.truncated = persisted.truncated;
      turn.deferredDiff = persisted.deferred && persisted.messageId ? {
        conversationId: persisted.threadId, messageId: persisted.messageId,
        turnId: persisted.turnId, revision: persisted.revision,
      } : undefined;
      const summary = summarizeTurnDiff(persisted);
      if (persisted.diff === "") turn.files = [];
      if (summary) turn.files = summary.files.map((file) => ({ path: file.path, additions: file.additions, deletions: file.deletions,
        revisions: file.patch ? [{ id: `turn-${persisted.turnId}:${file.path}`,
          name: persisted.source === "workspace_snapshot" ? "工作区比较" : "本轮修改", diff: file.patch }] : [] }));
    }
  }
  // The runtime's final turn patch is the aggregate result, rather than a list of intermediate edits.
  if (turnDiff && (!conversationId || turnDiff.threadId === conversationId) && turns.has(turnDiff.turnId)
    && (!turnDiff.messageId || messages.some((message) => message.role === "assistant" && message.id === turnDiff.messageId && message.turnId === turnDiff.turnId))) {
    const summary = summarizeTurnDiff(turnDiff);
    const turn = turns.get(turnDiff.turnId)!;
    turn.source = turnDiff.source;
    turn.truncated = turnDiff.truncated;
    turn.deferredDiff = turnDiff.deferred && turnDiff.messageId ? {
      conversationId: turnDiff.threadId, messageId: turnDiff.messageId,
      turnId: turnDiff.turnId, revision: turnDiff.revision,
    } : undefined;
    if (turnDiff.diff === "") turns.get(turnDiff.turnId)!.files = [];
    if (summary) turns.get(turnDiff.turnId)!.files = summary.files.map((file) => ({
      path: file.path,
      additions: file.additions,
      deletions: file.deletions,
      revisions: file.patch ? [{ id: `turn-${turnDiff.turnId}:${file.path}`,
        name: turnDiff.source === "workspace_snapshot" ? "工作区比较" : "本轮修改", diff: file.patch }] : [],
    }));
  }
  return [...turns.values()].reverse();
}
