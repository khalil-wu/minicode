/* @vitest-environment jsdom */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { handleChatStreamEvent } from "./chatStreamEvents";
import { notifyScheduledCompletions } from "./completionNotifications";
import { projectMessagesToTurns } from "./chatSurfaceState";
import type { CompletionNotification } from "../protocol/notification-types";
import type { ServerEvent } from "../protocol/events";
import type { StreamBuffer } from "../lib/stream-buffer";

const native = vi.hoisted(() => ({ notify: vi.fn() }));
vi.mock("../desktop/runtime", async load => ({ ...await load<Record<string, unknown>>(), desktop: () => native }));
vi.mock("../protocol/ws-outbox", async load => ({ ...await load<Record<string, unknown>>(), sendClientCommand: vi.fn(() => true) }));
const notice = (overrides: Partial<CompletionNotification> = {}): CompletionNotification => ({
  thread_source: "user", automation_kind: null, automation_notification_decision: null,
  heartbeat_assistant_message: null, last_agent_message: "Actual final answer", has_pending_continuation: false,
  eligible: true, body: "Actual final answer", should_mark_unread: true, ...overrides,
});
const handlers = { textStreamBuffer: { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() } as StreamBuffer };
const done = (notification: CompletionNotification, extra = {}) => handleChatStreamEvent({
  type: "done", conversation_id: "actual-owner", message_id: "actual-message", turn_id: "actual-turn",
  status: "completed", usage: {}, completion_notification: notification, ...extra,
} as ServerEvent, "actual-owner", handlers);

beforeEach(() => {
  native.notify.mockClear();
  useAppStore.setState({ conversationId: "actual-owner", pendingConversationSwitchId: null,
    conversations: [{ id: "actual-owner", title: "Actual conversation title", updatedAt: "2026-10-14" }],
    messages: [{ id: "actual-message", role: "assistant", content: "Actual final answer", isStreaming: true, timestamp: 1, artifacts: [] }],
    conversationMessages: {}, conversationStreaming: { "actual-owner": true }, isStreaming: true,
    pendingApproval: null, approvalQueue: [], pendingDiffReview: null, diffReviewQueue: [], pendingAskUser: null, askUserQueue: [],
    draft: "Original unsent draft", attachments: [],
  });
});

describe("Source completion facts reach the native consumer", () => {
  it("uses the final answer and conversation title with actual ownership and deduplicates replay", () => {
    done(notice());
    expect(native.notify).toHaveBeenCalledExactlyOnceWith({ kind: "completion", title: "Actual conversation title", body: "Actual final answer",
      target: { kind: "conversation", conversationId: "actual-owner" }, turnId: "actual-turn", messageId: "actual-message" });
    done(notice(), { replayed: true });
    expect(native.notify).toHaveBeenCalledOnce();
    expect(useAppStore.getState().draft).toBe("Original unsent draft");
  });
  it.each([
    { body: null, automation_kind: "heartbeat" as const, automation_notification_decision: "DONT_NOTIFY" as const },
    { eligible: false, thread_source: "chatgpt_hidden" },
    { eligible: false, has_pending_continuation: true },
  ])("retains terminal evidence while Source suppresses notification: %j", filter => {
    done(notice(filter));
    expect(native.notify).not.toHaveBeenCalled();
    expect(useAppStore.getState().messages[0].content).toBe("Actual final answer");
    expect(useAppStore.getState().messages[0].terminalStatus).toBe("completed");
  });
  it("does not suppress an ordinary empty failed turn or failed_runs_only failure", () => {
    done(notice({ last_agent_message: "", body: "MiniCode 已完成一个回合。", automation_notification_decision: "NOTIFY" }), { status: "failed" });
    expect(native.notify).toHaveBeenCalledWith(expect.objectContaining({ title: "Actual conversation title", body: "MiniCode 已完成一个回合。" }));
    expect(useAppStore.getState().messages[0].terminalStatus).toBe("failed");
  });
  it.each(["::SKIP_COMPLETION::", " \n:: SKIP_COMPLETION ::\n ", "<![CDATA[ ::SKIP_COMPLETION:: ]]>"])("typed final projection consumes the exact Source marker and keeps raw evidence: %j", text => {
    handleChatStreamEvent({ type: "item.completed", conversation_id: "actual-owner", message_id: "actual-message",
      item: { id: "source-final-item", type: "agent_message", text, source: "model_final", status: "completed" },
    } as ServerEvent, "actual-owner", handlers);
    done(notice({ body: null, last_agent_message: text }));
    const messages = useAppStore.getState().messages;
    expect(messages[0].blocks?.find(block => block.type === "text")?.content).toBe(text);
    const turn = projectMessagesToTurns(messages, false, "")[0];
    expect(turn.finalAnswerCell).toBeNull();
    expect(turn.activeCell).toBeNull();
    expect(native.notify).not.toHaveBeenCalled();
  });
  it.each(["Use ::SKIP_COMPLETION:: in this command", "```text\n::SKIP_COMPLETION::\n```", "> ::SKIP_COMPLETION::", "<![CDATA[::SKIP_COMPLETION::]]>"])("a literal example or malformed envelope remains a visible actual final: %j", text => {
    handleChatStreamEvent({ type: "item.completed", conversation_id: "actual-owner", message_id: "actual-message",
      item: { id: "source-final-item", type: "agent_message", text, source: "model_final", status: "completed" },
    } as ServerEvent, "actual-owner", handlers);
    done(notice({ body: text, last_agent_message: text }));
    expect(projectMessagesToTurns(useAppStore.getState().messages, false, "")[0].finalAnswerCell?.markdownSource).toBe(text);
  });
  it("Source control projection preserves command output and terminal error evidence", () => {
    const marker = "::SKIP_COMPLETION::";
    const message = useAppStore.getState().messages[0];
    const turn = projectMessagesToTurns([{ ...message, isStreaming: false, terminalStatus: "failed", failureMessage: marker,
      blocks: [
        { type: "tool_call", record: { id: "actual-command", name: "run_command", args: { command: "print literal marker" },
          status: "success", startedAt: 1, stdoutPreview: marker } },
        { type: "text", itemId: "actual-final", source: "model_final", status: "completed", isStreaming: false, content: marker },
      ],
    }], false, "")[0];
    expect(turn.finalAnswerCell).toBeNull();
    expect(turn.committedCells.some(cell => cell.kind === "exec" && cell.stdoutFull?.includes(marker))).toBe(true);
    expect(turn.committedCells.some(cell => cell.kind === "error" && cell.message === marker)).toBe(true);
  });
  it("scheduler initial snapshots stay quiet, then live terminal transitions notify once", () => {
    const run = { id: "real-run", task_id: "actual-task", conversation_id: "actual-owner", message_id: "scheduled-real-message", scheduled_at: "2026-10-14", status: "completed", completion_notification: notice() };
    notifyScheduledCompletions("fixture-real-workspace", [run]);
    expect(native.notify).not.toHaveBeenCalled();
    notifyScheduledCompletions("fixture-real-workspace", [{ ...run, id: "next-real-run", message_id: "next-real-message", status: "running", completion_notification: null }]);
    notifyScheduledCompletions("fixture-real-workspace", [{ ...run, id: "next-real-run", message_id: "next-real-message" }]);
    notifyScheduledCompletions("fixture-real-workspace", [{ ...run, id: "next-real-run", message_id: "next-real-message" }]);
    expect(native.notify).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ messageId: "next-real-message", body: "Actual final answer" }));
  });
});
