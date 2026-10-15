/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ChatTurn } from "./ChatTurn";
import { projectMessagesToTurns } from "../chatSurfaceState";
import { hydrateMessages } from "../transcriptHydration";
import { useAppStore } from "../../stores";
import { releaseConversationReadingState } from "../transcriptReadingState";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: (query: string) => ({ matches: query.includes("prefers-reduced-motion"), addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true) }));
vi.mock("../../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

const owner = "reference-process";
beforeEach(() => {
  releaseConversationReadingState(owner);
  useAppStore.setState({ conversationId: owner, subagents: [], viewMode: "normal", draft: "保留的主聊天草稿" });
});
afterEach(cleanup);

it("projects the reference read/run group, parent message and subsequent thinking as three ordered boundaries", () => {
  const messages = hydrateMessages([{ id: "child-work", role: "assistant", content: "", timestamp: 1000, is_streaming: true, blocks: [
    { type: "tool_call", record: { id: "read", name: "read_file", args: { path: "AGENTS.md" }, status: "success", result_kind: "read",
      activity_kind: "fileRead", started_at: 1000, finished_at: 1500, output_preview: "Actual repository instructions" } },
    { type: "tool_call", record: { id: "run", name: "run_command", args: { command: "rg -n process frontend" }, status: "success", result_kind: "command",
      activity_kind: "commandExecution", started_at: 1500, finished_at: 3000, duration_ms: 1500, stdout_preview: "Actual matched source path" } },
    { type: "tool_call", record: { id: "parent-message", name: "send_message", args: { recipient: "parent", message: "Private dispatch details" }, status: "success", started_at: 3000, finished_at: 3100 } },
    { type: "process", id: "new-thinking", item_kind: "process_text", content: "正在思考", source: "model_preamble", status: "completed", visibility: "timeline", timestamp: 3200 },
  ] }]);
  const { container } = render(<ChatTurn turn={projectMessagesToTurns(messages, true)[0]} isTranscriptMode conversationId={owner} />);
  const summary = screen.getByRole("button", { name: "已读取文件运行了命令" });
  const parentMessage = screen.getByText("已向父代理发送消息");
  const thinking = screen.getByText("正在思考");
  expect(summary.querySelector(".lucide-book-open-text")).toBeTruthy();
  expect(parentMessage.closest(".agent-loop-timeline-group-work")).toBeNull();
  expect(parentMessage.parentElement?.querySelector(".lucide-message-circle")).toBeTruthy();
  expect(container.querySelectorAll(".agent-loop-timeline-group-work")).toHaveLength(1);
  expect(summary.compareDocumentPosition(parentMessage) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(parentMessage.compareDocumentPosition(thinking) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(screen.queryByText("Private dispatch details")).toBeNull();
  fireEvent.click(summary);
  expect(screen.getByText("AGENTS.md")).toBeTruthy();
  expect(screen.getByText("rg -n process frontend")).toBeTruthy();
  expect(screen.getByText("已在1s内运行")).toBeTruthy();
  expect(screen.queryByText("Actual matched source path")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /展开命令详情/ }));
  expect(screen.getByText("Actual matched source path")).toBeTruthy();
  expect(screen.getByText("命令已在1s内运行完成")).toBeTruthy();
  expect(screen.getByText("$ rg -n process frontend")).toBeTruthy();
  fireEvent.click(summary);
  expect(screen.queryByText("AGENTS.md")).toBeNull();
  expect(screen.getByText("已向父代理发送消息")).toBeTruthy();
  expect(screen.getByText("正在思考")).toBeTruthy();
  expect(useAppStore.getState().draft).toBe("保留的主聊天草稿");
});
