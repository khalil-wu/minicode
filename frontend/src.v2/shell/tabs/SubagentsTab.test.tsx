/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../../stores";
import type { SubagentState } from "../../stores/types";
import { SubagentsTab } from "./SubagentsTab";

const { sendClientCommandMock, sendClientCommandAwaitResultMock } = vi.hoisted(() => ({
  sendClientCommandMock: vi.fn(() => true),
  sendClientCommandAwaitResultMock: vi.fn(async (command: unknown) => {
    sendClientCommandMock(command);
    return { type: "command.result", command: (command as { type?: string }).type || "", level: "success", message: "", data: {} };
  }),
}));

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
});

vi.mock("../../protocol/ws-outbox", () => ({
  sendClientCommand: sendClientCommandMock,
  sendClientCommandAwaitResult: sendClientCommandAwaitResultMock,
  commandResultSucceeded: (result: { level?: string }) => result.level !== "error",
}));

describe("SubagentsTab", () => {
  it("renders a rotating mark for running subagents", () => {
    useAppStore.setState({
      focusedSubagentId: null,
      subagents: [{
        id: "subagent-running-stable",
        role: "explore",
        status: "running",
        objective: "查询北京当前天气",
      }],
    });

    const { container } = render(<SubagentsTab />);

    expect(screen.getByText("运行中")).toBeTruthy();
    expect(container.querySelector('.subagents-glyph[data-status="running"]')).toBeTruthy();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    sendClientCommandMock.mockReturnValue(true);
    sendClientCommandAwaitResultMock.mockImplementation(async (command: unknown) => {
      sendClientCommandMock(command);
      return { type: "command.result", command: (command as { type?: string }).type || "", level: "success", message: "", data: {} };
    });
    useAppStore.setState({
      focusedSubagentId: null,
      conversationId: "conversation-1",
      workingDirectory: "C:\\workspace",
      subagents: [
        {
          id: "subagent-1",
          role: "verification",
          status: "running",
          objective: "验证测试结果",
          currentActivity: "正在检查测试结果",
        },
        {
          id: "subagent-2",
          role: "explore",
          status: "done",
          objective: "定位界面问题",
          summary: "已找到主要问题",
          resultContent: "## 结论\n\n侧栏应只展示任务、状态和结果。",
        },
      ],
    });
  });

  afterEach(() => {
    cleanup();
  });

  it("shows a calm empty state when there is no delegated work", () => {
    useAppStore.setState({ subagents: [] });

    render(<SubagentsTab />);

    expect(screen.getByText("暂无子智能体")).toBeTruthy();
    expect(screen.getByText("MiniCode 拆分任务后，委派工作会显示在这里。")).toBeTruthy();
  });

  it("groups concise task rows by user-facing state", () => {
    render(<SubagentsTab />);

    expect(screen.getByRole("region", { name: "进行中，1 项" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "已完成，1 项" })).toBeTruthy();
    expect(screen.getByText("验证测试结果")).toBeTruthy();
    expect(screen.getByText("正在检查测试结果")).toBeTruthy();
    expect(screen.getByText("定位界面问题")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "结论" })).toBeNull();
  });

  it("does not synthesize a chat answer from the runtime result envelope", async () => {
    render(<SubagentsTab />);

    expect(screen.queryByText("侧栏应只展示任务、状态和结果。")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体任务：定位界面问题" }));

    expect(screen.getByRole("region", { name: "子智能体任务详情：定位界面问题" })).toBeTruthy();
    await waitFor(() => expect(sendClientCommandAwaitResultMock).toHaveBeenCalled());
    expect(screen.queryByRole("heading", { name: "结论" })).toBeNull();
    expect(screen.queryByText("侧栏应只展示任务、状态和结果。")).toBeNull();
    expect(screen.getByText("这个子智能体没有可回放的工作记录。")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "返回子智能体列表" }));
    expect(screen.getByRole("region", { name: "进行中，1 项" })).toBeTruthy();
  });

  it("does not repeat the short summary as a substitute transcript", async () => {
    render(<SubagentsTab />);
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体任务：定位界面问题" }));

    await waitFor(() => expect(sendClientCommandAwaitResultMock).toHaveBeenCalled());
    expect(screen.queryByRole("heading", { name: "任务摘要" })).toBeNull();
    expect(screen.queryByText("已找到主要问题")).toBeNull();
  });

  it("keeps stop and result retrieval as detail-only actions", async () => {
    render(<SubagentsTab />);

    expect(screen.queryByText("停止任务")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体任务：验证测试结果" }));
    fireEvent.click(screen.getByRole("button", { name: "停止子智能体" }));

    expect(sendClientCommandMock).toHaveBeenCalledWith({
      type: "subagent.cancel",
      subagent_id: "subagent-1",
      conversation_id: "conversation-1",
      workspace_root: "C:\\workspace",
    });

    useAppStore.setState({
      focusedSubagentId: null,
      subagents: [{
        id: "subagent-result",
        role: "reviewer",
        status: "done",
        objective: "检查实现质量",
        resultAvailable: true,
      }],
    });

    cleanup();
    render(<SubagentsTab />);
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体任务：检查实现质量" }));

    expect(sendClientCommandMock).toHaveBeenCalledWith({
      type: "subagent.status",
      subagent_id: "subagent-result",
      include_result: true,
      conversation_id: "conversation-1",
      workspace_root: "C:\\workspace",
    });
    await waitFor(() => expect(screen.getByText("获取结果")).toBeTruthy());
    fireEvent.click(screen.getByText("获取结果"));
    expect(sendClientCommandMock).toHaveBeenCalledWith({
      type: "subagent.status",
      subagent_id: "subagent-result",
      include_result: true,
      conversation_id: "conversation-1",
      workspace_root: "C:\\workspace",
    });
  });

  it("keeps runtime counters and result envelopes out of the transcript", async () => {
    useAppStore.setState({
      subagents: [{
        id: "subagent-noisy",
        role: "explore",
        status: "done",
        objective: "调研石家庄天气",
        summary: "天气调研完成",
        currentActivity: "Running call_a7bb47d3d431442bb8ade914",
        detail: "46.1s elapsed",
        iteration: 8,
        maxIterations: 8,
        toolCallCount: 34,
        resultContent: [
          "### 天气结论",
          "- 石家庄今天晴，最高 31°C",
        ].join("\n"),
      }],
    });

    const { container } = render(<SubagentsTab />);
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体任务：调研石家庄天气" }));

    await waitFor(() => expect(sendClientCommandAwaitResultMock).toHaveBeenCalled());
    expect(screen.queryByText("石家庄今天晴，最高 31°C")).toBeNull();
    expect(container.textContent).not.toMatch(/call_a7bb|46\.1s|iteration|tool call/i);
  });

  it("keeps a running child transcript read-only", async () => {
    useAppStore.setState({
      conversationId: "conv-steer",
      focusedSubagentId: null,
      subagents: [{
        id: "subagent-steer",
        role: "reviewer",
        status: "running",
        objective: "检查实现",
      }],
    });
    render(<SubagentsTab />);
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体任务：检查实现" }));
    await waitFor(() => expect(sendClientCommandMock).toHaveBeenCalledWith(expect.objectContaining({
      type: "subagent.transcript",
      subagent_id: "subagent-steer",
    })));
    expect(screen.queryByLabelText("给这个子智能体发送消息")).toBeNull();
    expect(screen.queryByRole("button", { name: "发送给子智能体" })).toBeNull();
    expect(sendClientCommandMock).not.toHaveBeenCalledWith(expect.objectContaining({ type: "send_message" }));
  });

  it("keeps a completed child transcript read-only", async () => {
    useAppStore.setState({
      conversationId: "conv-resume",
      focusedSubagentId: null,
      subagents: [{
        id: "subagent-completed",
        role: "reviewer",
        status: "done",
        objective: "检查实现",
      }],
    });
    render(<SubagentsTab />);
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体任务：检查实现" }));
    await waitFor(() => expect(sendClientCommandMock).toHaveBeenCalledWith(expect.objectContaining({
      type: "subagent.transcript",
      subagent_id: "subagent-completed",
    })));
    expect(screen.queryByLabelText("给这个子智能体发送消息")).toBeNull();
    expect(screen.queryByText("继续给这个子智能体补充指令")).toBeNull();
    expect(sendClientCommandMock).not.toHaveBeenCalledWith(expect.objectContaining({ type: "send_message" }));
  });

  it("shows a terminal transcript gap as an explicit error", async () => {
    sendClientCommandAwaitResultMock.mockImplementation(async (command: unknown) => {
      sendClientCommandMock(command);
      return {
        type: "command.result",
        command: "subagent.transcript",
        level: "warning",
        message: "Terminal subagent subagent-gap has no durable transcript.",
        data: { seq: 0, messages: [], error_kind: "subagent_transcript_missing" },
      };
    });
    useAppStore.setState({
      conversationId: "conv-gap",
      focusedSubagentId: "subagent-gap",
      subagents: [{
        id: "subagent-gap",
        role: "reviewer",
        status: "done",
        objective: "检查持久化缺口",
        resultContent: "This must not become a fake final answer.",
      }],
    });

    render(<SubagentsTab />);

    expect(await screen.findByText("Terminal subagent subagent-gap has no durable transcript.")).toBeTruthy();
    expect(screen.queryByText("This must not become a fake final answer.")).toBeNull();
    expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
  });

  it("renders the child through the ordinary ChatTurn transcript without a details gate", async () => {
    sendClientCommandAwaitResultMock.mockImplementation(async (command: unknown) => {
      sendClientCommandMock(command);
      return {
        type: "command.result",
        command: "subagent.transcript",
        level: "success",
        message: "",
        data: { messages: [
          { id: "child-user", role: "user", content: "检查实现", timestamp: 1 },
          {
            id: "child-turn",
            role: "assistant",
            content: "## 结论\n实现已核对",
            timestamp: 2,
            completed_at: 4,
            duration_ms: 3000,
            terminal_status: "completed",
            blocks: [
              { type: "process", id: "child-process", item_kind: "process_text", content: "正在读取关键文件", source: "model_preamble", status: "completed", visibility: "timeline", timestamp: 2 },
              { type: "text", item_id: "child-answer", content: "## 结论\n实现已核对", source: "model_final", status: "completed" },
            ],
          },
        ] },
      };
    });
    useAppStore.setState({
      conversationId: "conv-transcript",
      focusedSubagentId: null,
      subagents: [{
        id: "subagent-transcript",
        role: "reviewer",
        status: "running",
        objective: "检查实现",
      }],
    });
    const { container } = render(<SubagentsTab />);
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体任务：检查实现" }));

    await waitFor(() => expect(screen.getByRole("heading", { name: "结论" })).toBeTruthy());
    expect(screen.queryByText("正在读取关键文件")).toBeNull();
    expect(screen.getByRole("button", { name: "展开处理步骤" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "结论" })).toBeTruthy();
    expect(screen.getByText("实现已核对")).toBeTruthy();
    expect(container.querySelector("details")).toBeNull();
    expect(container.querySelector("textarea")).toBeNull();
  });

  it("projects child tools and diffs with the same ordinary ChatTurn cells", async () => {
    sendClientCommandAwaitResultMock.mockImplementation(async (command: unknown) => {
      sendClientCommandMock(command);
      return {
        type: "command.result",
        command: "subagent.transcript",
        level: "success",
        message: "",
        data: { messages: [
          { id: "child-user-tools", role: "user", content: "检查并修复实现", timestamp: 1 },
          {
            id: "child-turn-tools",
            role: "assistant",
            content: "修复完成",
            timestamp: 2,
            completed_at: 8,
            duration_ms: 6000,
            terminal_status: "completed",
            is_streaming: false,
            blocks: [
              {
                type: "tool_call",
                record: {
                  id: "read-1",
                  name: "read_file",
                  args: { path: "src/app.ts" },
                  status: "success",
                  result_kind: "file",
                  activity_kind: "fileRead",
                  input_summary: "src/app.ts",
                  output_preview: "export const app = true;",
                  started_at: 2,
                  finished_at: 3,
                },
              },
              {
                type: "tool_call",
                record: {
                  id: "search-1",
                  name: "search_files",
                  args: { query: "app", path: "src" },
                  status: "success",
                  result_kind: "search",
                  activity_kind: "workspaceSearch",
                  input_summary: "app in src",
                  output_preview: "src/app.ts:1",
                  started_at: 3,
                  finished_at: 4,
                },
              },
              {
                type: "tool_call",
                record: {
                  id: "command-1",
                  name: "run_command",
                  args: { command: "npm test" },
                  status: "success",
                  result_kind: "command",
                  activity_kind: "commandExecution",
                  stdout_preview: "1 passed",
                  duration_ms: 1000,
                  started_at: 4,
                  finished_at: 5,
                },
              },
              {
                type: "tool_call",
                record: {
                  id: "edit-1",
                  name: "edit_file",
                  args: { path: "src/app.ts" },
                  status: "success",
                  result_kind: "edit",
                  activity_kind: "fileChange",
                  input_summary: "src/app.ts",
                  started_at: 5,
                  finished_at: 6,
                  diff: {
                    plus: 1,
                    minus: 1,
                    files: [{
                      path: "src/app.ts",
                      plus: 1,
                      minus: 1,
                      patch: "@@ -1 +1 @@\n-export const app = false;\n+export const app = true;",
                    }],
                  },
                },
              },
              { type: "text", item_id: "child-final-tools", content: "修复完成", source: "model_final", status: "completed" },
            ],
          },
        ] },
      };
    });
    useAppStore.setState({
      conversationId: "conv-tool-transcript",
      focusedSubagentId: null,
      subagents: [{
        id: "subagent-tool-transcript",
        role: "build",
        status: "done",
        objective: "检查并修复实现",
      }],
    });

    const { container } = render(<SubagentsTab />);
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体任务：检查并修复实现" }));

    expect(await screen.findByText("已处理 6 秒")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "展开处理步骤" }));
    fireEvent.click(screen.getByRole("button", { name: "读取了文件并搜索了内容并运行了命令并编辑了文件" }));

    const readCell = screen.getAllByText("Read", { selector: ".activity-cell-name" })[0]?.closest(".activity-cell");
    const searchCell = screen.getAllByText("Search", { selector: ".activity-cell-name" })[0]?.closest(".activity-cell");
    expect(readCell).toBeTruthy();
    expect(searchCell).toBeTruthy();
    fireEvent.click(within(readCell as HTMLElement).getByRole("button", { name: "展开活动详情" }));
    fireEvent.click(within(searchCell as HTMLElement).getByRole("button", { name: "展开活动详情" }));
    expect(within(readCell as HTMLElement).getAllByText("src/app.ts").length).toBeGreaterThan(0);
    expect(within(searchCell as HTMLElement).getByText("src/app.ts:1")).toBeTruthy();

    expect(screen.getByText("已运行命令")).toBeTruthy();
    expect(screen.getByText("npm test")).toBeTruthy();
    expect(screen.getByText("已编辑", { exact: true })).toBeTruthy();
    expect(screen.getByText("修复完成")).toBeTruthy();
    expect(screen.getByText("已编辑 1 个文件")).toBeTruthy();

    const workArea = screen.getByLabelText("Agent 处理进度");
    const replyArea = screen.getByLabelText("Agent 回复");
    const diffArea = screen.getByLabelText("文件修改");
    expect(workArea.compareDocumentPosition(replyArea) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(replyArea.compareDocumentPosition(diffArea) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(container.querySelectorAll(".chat-turn")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "撤销" })).toBeNull();
    expect(screen.queryByText("审核")).toBeNull();
  });

  it("loads the durable transcript once and does not poll on ordinary progress", async () => {
    useAppStore.setState({
      conversationId: "conv-push",
      focusedSubagentId: "subagent-push",
      subagents: [{
        id: "subagent-push",
        role: "reviewer",
        status: "running",
        objective: "检查推送",
      }],
    });
    render(<SubagentsTab />);

    await waitFor(() => expect(sendClientCommandAwaitResultMock).toHaveBeenCalledTimes(1));
    for (let index = 0; index < 5; index += 1) {
      act(() => {
        useAppStore.setState({
          subagents: [{
            id: "subagent-push",
            role: "reviewer",
            status: "running",
            objective: "检查推送",
            currentActivity: `进度 ${index + 1}`,
          }],
        });
      });
    }

    await waitFor(() => expect(sendClientCommandAwaitResultMock).toHaveBeenCalledTimes(1));
  });

  it("lets a pushed snapshot invalidate an older durable transcript response", async () => {
    let resolveRequest: ((value: {
      type: string;
      command: string;
      level: string;
      message: string;
      data: { seq: number; messages: Array<Record<string, unknown>> };
    }) => void) | undefined;
    sendClientCommandAwaitResultMock.mockImplementation(async (command: unknown) => {
      sendClientCommandMock(command);
      return await new Promise((resolve) => {
        resolveRequest = resolve;
      });
    });
    useAppStore.setState({
      conversationId: "conv-race",
      focusedSubagentId: "subagent-race",
      subagents: [{
        id: "subagent-race",
        role: "reviewer",
        status: "running",
        objective: "检查竞态",
      }],
    });
    render(<SubagentsTab />);

    await waitFor(() => expect(sendClientCommandAwaitResultMock).toHaveBeenCalledTimes(1));
    act(() => {
      useAppStore.setState({
        subagents: [{
          id: "subagent-race",
          role: "reviewer",
          status: "running",
          objective: "检查竞态",
          transcriptSeq: 2,
          transcriptMessages: [{
            id: "pushed-final",
            role: "assistant",
            content: "来自实时推送的新记录",
            timestamp: 2,
          }],
        }],
      });
    });
    expect(await screen.findByText("来自实时推送的新记录")).toBeTruthy();

    await act(async () => {
      resolveRequest?.({
        type: "command.result",
        command: "subagent.transcript",
        level: "success",
        message: "",
        data: {
          seq: 1,
          messages: [{
            id: "stale-final",
            role: "assistant",
            content: "过期的请求结果",
            timestamp: 1,
          }],
        },
      });
      await Promise.resolve();
    });

    expect(screen.getByText("来自实时推送的新记录")).toBeTruthy();
    expect(screen.queryByText("过期的请求结果")).toBeNull();
    expect(sendClientCommandAwaitResultMock).toHaveBeenCalledTimes(1);
  });

  it("renders an already-pushed transcript while durable hydration is still pending", async () => {
    sendClientCommandAwaitResultMock.mockImplementation(async (command: unknown) => {
      sendClientCommandMock(command);
      return await new Promise(() => undefined);
    });
    useAppStore.setState({
      conversationId: "conv-preloaded-push",
      focusedSubagentId: "subagent-preloaded-push",
      subagents: [{
        id: "subagent-preloaded-push",
        role: "reviewer",
        status: "running",
        objective: "检查预载推送",
        transcriptSeq: 2,
        transcriptMessages: [{
          id: "preloaded-process",
          role: "assistant",
          content: "",
          timestamp: 2,
          isStreaming: true,
          blocks: [{
            type: "process",
            id: "preloaded-process",
            itemKind: "process_text",
            content: "实时工作记录已经到达",
            source: "model_preamble",
            status: "completed",
            visibility: "timeline",
            timestamp: 2,
          }, {
            type: "text",
            itemId: "preloaded-answer",
            content: "子智能体正文正在实时返回",
            source: "model_final",
            status: "in_progress",
            isStreaming: true,
          }],
        }],
      }],
    });

    render(<SubagentsTab />);

    expect(screen.getByText("实时工作记录已经到达")).toBeTruthy();
    expect(await screen.findByText("子智能体正文正在实时返回")).toBeTruthy();
    expect(screen.getByLabelText("Agent 回复")).toBeTruthy();
    expect(screen.queryByText("正在载入工作详情…")).toBeNull();
    await waitFor(() => expect(sendClientCommandAwaitResultMock).toHaveBeenCalledTimes(1));
  });

  it("hides workflow containers and internal node metadata while preserving worker tasks", () => {
    useAppStore.setState({
      subagents: [
        {
          id: "workflow-weather",
          role: "workflow",
          status: "running",
          workflowId: "workflow-weather",
          workflowName: "天气调研",
          workflowMode: "parallel",
          summary: "internal workflow container",
        },
        {
          id: "subagent-shijiazhuang",
          role: "explorer",
          status: "running",
          workflowId: "workflow-weather",
          nodeId: "node-weather-1",
          taskId: "task-weather-1",
          objective: "查询石家庄天气",
        },
        {
          id: "subagent-taiyuan",
          role: "explorer",
          status: "done",
          workflowId: "workflow-weather",
          nodeId: "node-weather-2",
          taskId: "task-weather-2",
          objective: "查询太原天气",
        },
      ],
    });

    const { container } = render(<SubagentsTab />);

    expect(screen.getByText("查询石家庄天气")).toBeTruthy();
    expect(screen.getByText("查询太原天气")).toBeTruthy();
    expect(container.textContent).not.toMatch(/internal workflow container|workflow-weather|node-weather|task-weather|并行|节点/);
  });

  it("presents partial, deadline and cancelled outcomes without protocol language", () => {
    useAppStore.setState({
      subagents: [
        {
          id: "subagent-deadline",
          role: "explore",
          status: "partial",
          objective: "调研可用资料",
          terminationReason: "deadline_exceeded",
        },
        {
          id: "subagent-cancelled",
          role: "reviewer",
          status: "cancelled",
          objective: "检查修改",
          terminationInitiator: "user",
        },
      ],
    });

    render(<SubagentsTab />);

    expect(screen.getByRole("region", { name: "需要处理，2 项" })).toBeTruthy();
    expect(screen.getByText("已保留结果")).toBeTruthy();
    expect(screen.getByText("已保留可用结果")).toBeTruthy();
    expect(screen.getByText("已停止")).toBeTruthy();
    expect(screen.getByText("已由你停止")).toBeTruthy();
    expect(screen.queryByText(/deadline|cancelled|partial/i)).toBeNull();
  });

  it("keeps a long completed history compact", () => {
    const completed: SubagentState[] = Array.from({ length: 10 }, (_, index) => ({
      id: `subagent-completed-${index}`,
      role: "reviewer",
      status: "done",
      objective: `已完成任务 ${index + 1}`,
    }));
    useAppStore.setState({ subagents: completed });

    render(<SubagentsTab />);

    expect(screen.queryByText("已完成任务 10")).toBeNull();
    fireEvent.click(screen.getByText("再显示 4 项"));
    expect(screen.getByText("已完成任务 10")).toBeTruthy();
  });

  it("keeps internal message rows out of the ordinary panel", () => {
    useAppStore.setState({
      subagents: [{
        id: "message-1",
        role: "message",
        status: "running",
        summary: "Message from run-1: checking progress",
      }],
    });

    render(<SubagentsTab />);

    expect(screen.queryByText(/Message from run-1/)).toBeNull();
    expect(screen.getByText("暂无子智能体")).toBeTruthy();
  });

});
