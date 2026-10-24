/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import captured from "../../test/fixtures/real-full-task-agent-messages.json";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { hydrateMessages, type BackendTranscriptMessage } from "./transcriptHydration";
import { ChatTurn } from "./components/ChatTurn";
import { CollaborationCell } from "./cells/CollaborationCell";
import type { ChatMessage } from "../stores/types";
import { useAppStore } from "../stores";
import { releaseConversationReadingState } from "./transcriptReadingState";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true,
  value: (query: string) => ({ matches: query.includes("prefers-reduced-motion"), addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

beforeEach(() => {
  releaseConversationReadingState(captured.conversationId);
  useAppStore.setState({ conversationId: captured.conversationId, subagents: [], viewMode: "normal" });
});
afterEach(cleanup);

describe("real Source mailbox destination projection", () => {
  it.each(captured.children)("keeps $name's actual root message visible when its child process is opened", ({ message }) => {
    const messages = hydrateMessages([message as unknown as BackendTranscriptMessage]);
    const turn = projectMessagesToTurns(messages, false)[0];
    const { container } = render(<ChatTurn turn={turn} isTranscriptMode conversationId={captured.conversationId} />);
    expect(container.querySelector('.chat-turn-process')?.getAttribute('data-collapsed')).toBe('true');
    fireEvent.click(screen.getByRole("button", { name: "展开处理步骤" }));
    expect(screen.getByText("已向父代理发送消息")).toBeTruthy();
    expect(container.querySelector('.collaboration-cell[data-action="sent_message"]')?.textContent).toBe("已向父代理发送消息");
    expect(container.querySelector("textarea")).toBeNull();
    expect(container.querySelector(".mc-agent-avatar")).toBeNull();
  });

  it.each([
    ["/root", "parent", "已向父代理发送消息"],
    ["/root/review", "branch-id", "review"],
    ["/root/other_branch", "sibling-id", "other_branch"],
  ])("uses the actual resolved receiver for %s without turning another agent into the root mailbox", (target, recipientId, label) => {
    useAppStore.setState({ subagents: [
      { id: "branch-id", displayName: "review", agentPath: "/root/review", role: "subagent", status: "running" },
      { id: "sibling-id", displayName: "other_branch", agentPath: "/root/other_branch", role: "subagent", status: "running" },
    ] });
    const message: ChatMessage = { id: "receipt", role: "assistant", content: "", artifacts: [], timestamp: 1,
      blocks: [{ type: "tool_call", record: { id: "resolved", name: "send_message", args: { target, message: "Private coordination" },
        recipientId, status: "success", startedAt: 1, finishedAt: 2 } }] };
    const cell = projectMessagesToTurns([message], false)[0].committedCells[0];
    expect(cell).toMatchObject({ kind: "collaboration", entries: [{ agentId: recipientId }] });
    if (cell.kind !== "collaboration") throw new Error("Missing coordination record");
    const { container } = render(<CollaborationCell cell={cell} conversationId={captured.conversationId} isTranscriptMode />);
    expect(container.textContent).toBe(label);
    expect(container.textContent).not.toContain("Private coordination");
    expect(container.querySelectorAll(".mc-agent-avatar")).toHaveLength(recipientId === "parent" ? 0 : 1);
    if (recipientId !== "parent") {
      fireEvent.click(screen.getByRole("button", { name: `打开子智能体：${label}` }));
      expect(useAppStore.getState().focusedSubagentId).toBe(recipientId);
    }
  });
});
