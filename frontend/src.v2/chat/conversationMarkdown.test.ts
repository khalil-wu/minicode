// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from "vitest";
import { loadConversationMarkdown, renderConversationMarkdown, type ConversationMarkdownOwner } from "./conversationMarkdown";
import type { ChatMessage } from "../stores/types";

const mocks = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock("../protocol/api", async load => ({ ...await load<typeof import("../protocol/api")>(), apiBase: () => "http://127.0.0.1:8000", fetchWithTimeout: mocks.fetch }));
const message = (id: string, role: "user" | "assistant", content: string): ChatMessage => ({ id, role, content, timestamp: 1, artifacts: [] });
const owner = (): ConversationMarkdownOwner => ({ id: "owner-A", title: "A # title", workspaceRoot: "C:/owner-A", sessionId: "session-A",
  messages: [message("new-user", "user", "New question\n\nwith **Markdown**"), message("new-answer", "assistant", "New answer with `code`.")],
  historyPage: { beforeMessageId: "new-user", hasMore: true, loading: false },
});
beforeEach(() => mocks.fetch.mockReset());
const response = (payload: unknown) => ({ ok: true, json: async () => payload });

it("copies source title/quoted user/plain final Markdown and reads all older pages without changing its captured reader", async () => {
  const original = owner();
  const messages = original.messages;
  mocks.fetch.mockResolvedValueOnce(response({ transcript: [message("old-user", "user", "Old question"), message("old-answer", "assistant", "Old answer")],
    transcript_page: { before_message_id: "old-user", has_more: true } }));
  mocks.fetch.mockResolvedValueOnce(response({ transcript: [message("first-user", "user", "First question"), message("first-answer", "assistant", "First answer")],
    transcript_page: { before_message_id: "first-user", has_more: false } }));
  const text = await loadConversationMarkdown(original);
  expect(text).toBe("# A \\# title\n\n> First question\n\nFirst answer\n\n> Old question\n\nOld answer\n\n> New question\n>\n> with **Markdown**\n\nNew answer with `code`.\n");
  expect(original.messages).toBe(messages);
  expect(original.messages).toHaveLength(2);
  expect(new URL(String(mocks.fetch.mock.calls[0][0])).searchParams.get("session_id")).toBe("session-A");
  expect(new URL(String(mocks.fetch.mock.calls[1][0])).searchParams.get("before_message_id")).toBe("old-user");
});

it("reads earlier tool pages into the original message before copying real command errors", async () => {
  const original = owner(); original.historyPage = undefined;
  original.messages[1] = { ...original.messages[1], toolPage: { before: 20, remaining: 1, total: 2, revision: "rev-A" }, blocks: [
    { type: "text", content: "New answer with `code`.", textRole: "final_answer", transcriptIndex: 21 },
  ] };
  mocks.fetch.mockResolvedValueOnce(response({ blocks: [{ type: "tool_call", transcriptIndex: 0, record: {
    id: "old-command", name: "exec_command", args: { cmd: "npm test" }, status: "failed", exitCode: 2, errorInfo: { user_message: "Missing test dependency" }, stdoutPreview: "real command output",
  } }], tool_page: { before: 0, remaining: 0, total: 2, revision: "rev-A" } }));
  const text = await loadConversationMarkdown(original);
  expect(text).toContain("npm test"); expect(text).toContain("Missing test dependency");
  expect(text).toContain("<details><summary>"); expect(text).toContain("New answer with `code`.");
  expect(original.messages[1].toolPage?.remaining).toBe(1);
  expect(new URL(String(mocks.fetch.mock.calls[0][0])).searchParams.get("revision")).toBe("rev-A");
});

it("does not copy a misleading partial conversation when reading its history fails", async () => {
  mocks.fetch.mockResolvedValue({ ok: false, status: 409, statusText: "Conflict", text: async () => "Original history changed" });
  await expect(loadConversationMarkdown(owner())).rejects.toThrow("Original history changed");
});

it("does not manufacture user messages or role headings for an empty chat", async () => {
  const original = owner(); original.messages = []; original.historyPage = undefined;
  expect(await loadConversationMarkdown(original)).toBeNull();
  expect(renderConversationMarkdown("Title", [message("question", "user", "Question"), message("answer", "assistant", "Answer")], "C:/A")).not.toContain("## Assistant");
});

it("copies each real context reference by its kind without pretending plugins or browser notes have file text", () => {
  const user = { ...message("user", "user", "Use this context"), contextRefs: [
    { kind: "file" as const, name: "app.ts", path: "src/app.ts", text: "const source = true;" },
    { kind: "skill" as const, name: "analysis", path: "C:/skills/analysis/SKILL.md", description: "Analyze the supplied record" },
    { kind: "plugin" as const, name: "Documents", path: "C:/plugins/documents", configName: "documents-local", description: "Document plugin" },
    { kind: "browser_annotation" as const, name: "Annotated page", path: "https://example.com/docs", url: "https://example.com/docs", selector: "#summary", note: "Review this section" },
  ] };
  const text = renderConversationMarkdown("Context", [user], "C:/A");
  expect(text).toContain("file: src/app.ts"); expect(text).toContain("const source = true;");
  expect(text).toContain("skill: analysis (C:/skills/analysis/SKILL.md)");
  expect(text).toContain("plugin: Documents (documents-local)"); expect(text).toContain("Document plugin");
  expect(text).toContain("browser_annotation: https://example.com/docs #summary"); expect(text).toContain("Review this section");
  expect(text).not.toContain("undefined");
});

it.each(["running", "completed", "interrupted"] as const)("retains the actual %s compaction record using source Context compaction/Source/Status Markdown", status => {
  const notice: ChatMessage = { ...message("compact", "system", ""), blocks: [{
    type: "process", id: "compact-item", itemKind: "context_compaction", content: "", status, source: "manual", timestamp: 1,
  }] };
  const text = renderConversationMarkdown("Compaction", [notice], "C:/A");
  expect(text).toContain("Context compaction"); expect(text).toContain("Source: manual"); expect(text).toContain(`Status: ${status}`);
});
