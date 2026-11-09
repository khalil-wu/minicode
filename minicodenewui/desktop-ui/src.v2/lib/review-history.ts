import type { ChatMessage, TurnDiffState } from "../stores/types";
import { getToolCallsFromMessage } from "./content-blocks";
import { summarizeTurnDiff } from "./turn-diff";
import { extractFilePathFromDiff, parseUnifiedDiffLines } from "./unified-diff";
import type { DeferredTurnDiffOwner } from "../chat/loadMessageTurnDiff";
import type { ToolCallRecord } from "./tool-call-reducer";

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

interface ReviewTranscript {
  historyMessages: ChatMessage[];
  commentMessages: ChatMessage[];
  latestReviewInput?: ChatMessage;
}

const historyToolIncluded = (tool: ToolCallRecord) => !tool.temporaryRemoved
  && tool.status !== "pending" && tool.status !== "running"
  && (tool.status === "success" || Boolean(tool.diff?.patch || tool.diff?.files?.some(file => file.patch)));

function sameHistoryMessage(previous: ChatMessage | undefined, message: ChatMessage): boolean {
  if (previous === message) return true;
  if (!previous || previous.id !== message.id || previous.role !== message.role) return false;
  if (message.role === "user") return previous.content === message.content;
  if (previous.turnId !== message.turnId || previous.turnDiff !== message.turnDiff) return false;
  const before = getToolCallsFromMessage(previous).filter(historyToolIncluded);
  const after = getToolCallsFromMessage(message).filter(historyToolIncluded);
  return before.length === after.length && before.every((tool, index) => {
    const next = after[index];
    return tool === next || tool.id === next.id && tool.name === next.name && tool.turnId === next.turnId
      && tool.diff === next.diff && tool.args.diff === next.args.diff && tool.args.patch === next.args.patch
      && tool.args.path === next.args.path && tool.args.file_path === next.args.file_path;
  });
}

/** Subscribe to review facts without replaying all historical patches for every text/stdout delta. */
export function createReviewTranscriptSelector(): (messages: ChatMessage[]) => ReviewTranscript {
  let previousMessages: ChatMessage[] | undefined;
  let selected: ReviewTranscript = { historyMessages: [], commentMessages: [] };
  return messages => {
    if (messages === previousMessages) return selected;
    let historyMessages = selected.historyMessages;
    let commentMessages = selected.commentMessages;
    let historyCount = 0;
    let commentCount = 0;
    let latestReviewInput: ChatMessage | undefined;
    for (const message of messages) {
      if (message.role === "user" || message.role === "assistant") {
        const previous = selected.historyMessages[historyCount];
        const next = sameHistoryMessage(previous, message) ? previous! : message;
        if (historyMessages === selected.historyMessages && next !== previous) historyMessages = historyMessages.slice(0, historyCount);
        if (historyMessages !== selected.historyMessages) historyMessages.push(next);
        historyCount += 1;
      }
      if (message.role === "assistant" && !message.isStreaming) {
        const previous = selected.commentMessages[commentCount];
        const next = previous?.id === message.id && previous.content === message.content ? previous : message;
        if (commentMessages === selected.commentMessages && next !== previous) commentMessages = commentMessages.slice(0, commentCount);
        if (commentMessages !== selected.commentMessages) commentMessages.push(next);
        commentCount += 1;
      }
      if (message.role === "user" && message.codeReview) latestReviewInput = message;
    }
    if (historyMessages.length !== historyCount) historyMessages = historyMessages.slice(0, historyCount);
    if (commentMessages.length !== commentCount) commentMessages = commentMessages.slice(0, commentCount);
    if (latestReviewInput?.id === selected.latestReviewInput?.id && latestReviewInput?.codeReview === selected.latestReviewInput?.codeReview) {
      latestReviewInput = selected.latestReviewInput;
    }
    if (historyMessages !== selected.historyMessages || commentMessages !== selected.commentMessages || latestReviewInput !== selected.latestReviewInput) {
      selected = { historyMessages, commentMessages, latestReviewInput };
    }
    previousMessages = messages;
    return selected;
  };
}

const toolPatches = new WeakMap<ToolCallRecord, { path: string; patch?: string }[]>();

function reviewToolPatches(tool: ToolCallRecord): { path: string; patch?: string }[] {
  const cached = toolPatches.get(tool);
  if (cached) return cached;
  const candidate = tool.diff?.patch ?? tool.args.diff ?? tool.args.patch;
  const diff = typeof candidate === "string" ? candidate : "";
  const summary = summarizeTurnDiff({ threadId: "", turnId: "", diff, updatedAt: 0 });
  const argumentPath = tool.args.path ?? tool.args.file_path;
  const filePatches = tool.diff?.files?.filter(file => file.patch).map(file => ({ path: file.path, patch: file.patch! }));
  const patches = summary?.files ?? (filePatches?.length ? filePatches : diff.includes("\n") ? [{
    path: extractFilePathFromDiff(parseUnifiedDiffLines(diff)) || (typeof argumentPath === "string" ? argumentPath : ""),
    patch: diff,
  }] : []);
  toolPatches.set(tool, patches);
  return patches;
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
      if (!historyToolIncluded(tool)) continue;
      const turn = ensureTurn(tool.turnId || messageTurnId, userLabel);
      for (const patch of reviewToolPatches(tool)) {
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
