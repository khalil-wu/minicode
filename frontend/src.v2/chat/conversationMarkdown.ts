import { ApiError, apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import type { ConversationRecordPayload } from "../protocol/conversation-types";
import type { ChatMessage, ContentBlock, ConversationHistoryPage, MessageContextRef } from "../stores/types";
import type { HistoryCellState, UserMessageCellState } from "./cells/cellTypes";
import { hydrateMessages, normalizeContentBlocks } from "./transcriptHydration";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { getRecordOutputText } from "./cells/activityCellHelpers";

export interface ConversationMarkdownOwner {
  id: string;
  title: string;
  workspaceRoot: string;
  messages: ChatMessage[];
  historyPage?: ConversationHistoryPage;
  sessionId: string;
  messageCount?: number;
}

async function readHistory(url: URL) {
  const response = await fetchWithTimeout(url, { headers: authHeaders() });
  if (!response.ok) throw new ApiError(response.status, errorMessageFromResponseText(await response.text(), response.statusText));
  return response.json();
}

/** Read the original owner's complete history without changing the live reader or its scroll position. */
export async function loadConversationMarkdown(owner: ConversationMarkdownOwner) {
  let messages = owner.messages;
  let before = owner.historyPage?.hasMore ? owner.historyPage.beforeMessageId : "";
  const readPage = async (cursor: string) => {
    const url = new URL(`${apiBase()}/api/conversations/${encodeURIComponent(owner.id)}/messages`);
    url.searchParams.set("session_id", owner.sessionId);
    url.searchParams.set("before_message_id", cursor);
    url.searchParams.set("limit", "200");
    return await readHistory(url) as Required<Pick<ConversationRecordPayload, "transcript" | "transcript_page">>;
  };
  if (!owner.historyPage && (owner.messageCount ?? 0) > messages.length) {
    const page = await readPage("");
    messages = hydrateMessages(page.transcript);
    before = page.transcript_page.has_more ? page.transcript_page.before_message_id : "";
  }
  while (before) {
    const page = await readPage(before);
    messages = [...hydrateMessages(page.transcript), ...messages];
    before = page.transcript_page.has_more ? page.transcript_page.before_message_id : "";
  }
  const complete = await Promise.all(messages.map(async message => {
    let page = message.toolPage;
    if (!page?.remaining) return message;
    const blocks = new Map<number, ContentBlock>();
    const unpositioned: ContentBlock[] = [];
    for (const block of message.blocks ?? []) {
      if (block.transcriptIndex === undefined) unpositioned.push(block);
      else blocks.set(block.transcriptIndex, block);
    }
    while (page.remaining) {
      const url = new URL(`${apiBase()}/api/conversations/${encodeURIComponent(owner.id)}/messages/${encodeURIComponent(message.id)}/tools`);
      url.searchParams.set("session_id", owner.sessionId);
      url.searchParams.set("before", String(page.before));
      if (page.revision) url.searchParams.set("revision", page.revision);
      const earlier: { blocks: unknown[]; tool_page: typeof page } = await readHistory(url);
      for (const block of normalizeContentBlocks(earlier.blocks) ?? []) blocks.set(block.transcriptIndex!, block);
      page = earlier.tool_page;
    }
    return { ...message, blocks: [...blocks].sort(([left], [right]) => left - right).map(([, block]) => block).concat(unpositioned), toolPage: page };
  }));
  return complete.length ? renderConversationMarkdown(owner.title, complete, owner.workspaceRoot) : null;
}

const quote = (text: string) => text.replace(/\r\n?/g, "\n").trim().split("\n").map(line => line ? `> ${line}` : ">").join("\n");
const escapeDetails = (text: string) => text.replace(/<\/?details\b[^>]*>/gi, tag => tag.replaceAll("<", "&lt;").replaceAll(">", "&gt;"));
const htmlText = (text: string) => text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
function fence(text: string, language: string) {
  const width = Math.max(3, ...Array.from(text.matchAll(/`+/g), match => match[0].length + 1));
  const delimiter = "`".repeat(width);
  return `${delimiter}${language}\n${text.trimEnd()}\n${delimiter}`;
}

function contextRefMarkdown(ref: MessageContextRef): string {
  switch (ref.kind) {
    case "file":
    case "folder":
    case "url": return `- ${ref.kind}: ${ref.path}${ref.text ? `\n\n${ref.text}` : ""}`;
    case "skill": return `- skill: ${ref.name}${ref.path ? ` (${ref.path})` : ""}${ref.description ? `\n\n${ref.description}` : ""}`;
    case "plugin": return `- plugin: ${ref.name} (${ref.configName})\n  ${ref.path}${ref.description ? `\n\n${ref.description}` : ""}`;
    case "browser_annotation": return `- browser_annotation: ${ref.url}${ref.selector ? ` ${ref.selector}` : ""}\n\n${ref.note}`;
    case "diff_comment": return `- Diff comment: ${ref.path}\n\n${ref.comment.content.map(item => item.text).join("\n")}`;
  }
}

function userMarkdown(cell: UserMessageCellState) {
  const context = [
    ...(cell.attachments ?? []).map(attachment => `- Attachment: ${attachment.name}`),
    ...(cell.contextRefs ?? []).map(contextRefMarkdown),
    ...(cell.quotedMessage ? [`- Quoted message\n\n${cell.quotedMessage.content}`] : []),
  ];
  return quote([cell.content, ...(context.length ? [`User context\n\n${context.join("\n")}`] : [])].join("\n\n"));
}

function cellMarkdown(cell: HistoryCellState): string | null {
  switch (cell.kind) {
    case "thinking": return ["commentary", "model_preamble", "post_tool"].includes(cell.source) ? escapeDetails(cell.content) : null;
    case "assistant_markdown": return escapeDetails(cell.markdownSource);
    case "streaming_assistant_tail": return cell.partialMarkdown;
    case "exec": {
      const output = [cell.stdoutFull ?? cell.stdoutPreview.join("\n"), cell.stderrFull ?? cell.stderrPreview.join("\n")].filter(Boolean).join("\n");
      const status = cell.exitCode == null ? cell.status : cell.exitCode === 0 ? "Success" : `Failed with exit code ${cell.exitCode}`;
      return `<details><summary>Ran <code>${htmlText(cell.command)}</code></summary>\n\n${[
        fence(`$ ${cell.command}`, "bash"), ...(output ? [fence(output, "text")] : []), status,
      ].join("\n\n")}\n\n</details>`;
    }
    case "activity": return cell.toolCallRecords?.length ? cell.toolCallRecords.map(record => [
      record.displaySummary || record.displayHint || record.name, fence(JSON.stringify(record.args, null, 2), "json"), getRecordOutputText(record),
      record.errorInfo?.user_message || record.errorInfo?.user_summary || record.userSummary,
    ].filter(Boolean).join("\n\n")).join("\n\n") : [cell.title, cell.outputText].filter(Boolean).join("\n\n");
    case "diff": return cell.files.map(file => `${file.changeType === "deleted" ? "Deleted" : "Wrote"} \`${file.path}\` (+${file.additions} -${file.deletions})${file.patch ? `\n\n${fence(file.patch, "diff")}` : ""}`).join("\n\n");
    case "error": return [cell.title, cell.message].filter(Boolean).join("\n\n");
    case "status_notice": return [cell.title, cell.message].filter(Boolean).join("\n\n");
    case "guardian_review": return `Auto-review\n\n${[
      `Status: ${cell.review.status === "in_progress" ? "inProgress" : cell.review.status === "timed_out" ? "timedOut" : cell.review.status}`,
      ...(cell.review.risk_level ? [`Risk: ${cell.review.risk_level}`] : []),
      ...(cell.review.rationale ? [`Rationale: ${escapeDetails(cell.review.rationale)}`] : []),
    ].join("\n")}`;
    case "guardian_warning": return null;
    case "context_compaction": return `Context compaction\n\n${[
      ...(cell.source ? [`Source: ${cell.source}`] : []), `Status: ${cell.status}`,
    ].join("\n")}`;
    case "collaboration": return [
      ...cell.entries.map(entry => [entry.agentLabel, entry.content].filter(Boolean).join("\n\n")), cell.error,
    ].filter(Boolean).join("\n\n");
    case "user_message": return userMarkdown(cell);
  }
}

/** Conversation Markdown uses a title, quoted user text, disclosed process, and plain final Markdown. */
export function renderConversationMarkdown(title: string, messages: ChatMessage[], workspaceRoot: string) {
  const parts = [`# ${title.replace(/\s+/g, " ").trim().replaceAll("#", "\\#")}`];
  for (const turn of projectMessagesToTurns(messages, false, workspaceRoot)) {
    if (turn.userCell) parts.push(userMarkdown(turn.userCell));
    const previous = turn.committedCells.map(cellMarkdown).filter((content): content is string => Boolean(content?.trim()));
    if (previous.length) parts.push(`<details><summary>${previous.length} previous ${previous.length === 1 ? "message" : "messages"}</summary>\n\n${quote(previous.join("\n\n"))}\n</details>`);
    const answer = turn.finalAnswerCell?.markdownSource ?? (turn.activeCell?.kind === "streaming_assistant_tail" ? turn.activeCell.partialMarkdown : null);
    if (answer?.trim()) parts.push(answer.trim());
  }
  return `${parts.join("\n\n").trimEnd()}\n`;
}
