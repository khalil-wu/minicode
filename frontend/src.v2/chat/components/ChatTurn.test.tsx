/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
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
import type { ChatTurnState, HistoryCellState } from "../cells/cellTypes";
import { ChatTurn, HistoryCellRenderer } from "./ChatTurn";

afterEach(() => cleanup());

describe("HistoryCellRenderer", () => {
  it.each<HistoryCellState>([
    {
      kind: "thinking",
      id: "thinking-1",
      content: "Checking the request",
      source: "provider",
      isStreaming: true,
      createdAt: 1,
    },
    {
      kind: "activity",
      id: "activity-1",
      activityKind: "workspaceSearch",
      title: "Searched workspace",
      status: "done",
      collapsed: false,
      startedAt: 1,
    },
    {
      kind: "exec",
      id: "exec-1",
      command: "npm test",
      status: "success",
      stdoutPreview: ["12 passed"],
      stderrPreview: [],
      collapsed: false,
      createdAt: 1,
    },
    {
      kind: "diff",
      id: "diff-1",
      status: "updated",
      files: [{ path: "src/app.ts", additions: 2, deletions: 1 }],
      summary: { added: 2, deleted: 1, modifiedFiles: 1 },
      collapsed: false,
      createdAt: 1,
    },
  ])("renders the explicit $kind cell", (cell) => {
    const { container } = render(<HistoryCellRenderer cell={cell} />);
    expect(container.firstChild).not.toBeNull();
  });

  it("keeps a parent-owned child transcript view-only across ordinary cells", () => {
    const { rerender } = render(<HistoryCellRenderer
      isTranscriptMode
      cell={{
        kind: "user_message",
        id: "child-user",
        content: "Inspect the implementation",
        attachments: [],
        createdAt: 1,
      }}
    />);
    expect(screen.queryByRole("button", { name: "复制消息" })).toBeNull();
    expect(screen.queryByRole("button", { name: "撤回到输入框" })).toBeNull();

    rerender(<HistoryCellRenderer
      isTranscriptMode
      cell={{
        kind: "assistant_markdown",
        id: "child-answer",
        messageId: "child-answer",
        markdownSource: "Review complete",
        phase: "final",
        copyable: true,
        createdAt: 2,
      }}
    />);
    expect(screen.queryByRole("button", { name: "复制回复" })).toBeNull();
    expect(screen.queryByRole("button", { name: "引用回复" })).toBeNull();
    expect(screen.queryByRole("button", { name: "重新生成" })).toBeNull();

    rerender(<HistoryCellRenderer
      isTranscriptMode
      onStopExecution={vi.fn()}
      cell={{
        kind: "exec",
        id: "child-exec",
        command: "npm test",
        status: "running",
        stdoutPreview: [],
        stderrPreview: [],
        collapsed: false,
        createdAt: 3,
      }}
    />);
    expect(screen.queryByRole("button", { name: "停止命令" })).toBeNull();

    rerender(<HistoryCellRenderer
      isTranscriptMode
      cell={{
        kind: "diff",
        id: "child-diff",
        status: "updated",
        files: [{ path: "src/app.ts", additions: 1, deletions: 0 }],
        summary: { added: 1, deleted: 0, modifiedFiles: 1 },
        collapsed: false,
        createdAt: 4,
      }}
    />);
    expect(screen.queryByRole("button", { name: "撤销" })).toBeNull();
  });
});

describe("ChatTurn live answer", () => {
  it("renders the first streamed answer text immediately without waiting for completion", () => {
    const turn: ChatTurnState = {
      id: "turn-live-answer",
      userCell: null,
      committedCells: [],
      activeCell: {
        kind: "streaming_assistant_tail",
        id: "answer-live",
        partialMarkdown: "正在直接流式输出",
        updatedAt: 2,
      },
      finalAnswerCell: null,
      status: "streaming",
      startedAt: 1,
    };

    const { container } = render(<ChatTurn turn={turn} />);

    expect(screen.getByText("正在直接流式输出")).toBeTruthy();
    expect(container.querySelector('[data-zone="reply"]')).toBeTruthy();
    expect(container.querySelector('[data-zone="work"]')).toBeNull();
  });

  it("does not append processing status below prior work while the answer is streaming", () => {
    const turn: ChatTurnState = {
      id: "turn-live-answer-after-tool",
      userCell: null,
      committedCells: [{
        kind: "activity",
        id: "search-complete",
        activityKind: "webSearch",
        title: "搜索网页",
        status: "done",
        collapsed: true,
        startedAt: 1,
        completedAt: 2,
      }],
      activeCell: {
        kind: "streaming_assistant_tail",
        id: "answer-live",
        partialMarkdown: "这是正在流式输出的答案。",
        updatedAt: 3,
      },
      finalAnswerCell: null,
      status: "streaming",
      startedAt: 1,
    };

    render(<ChatTurn turn={turn} />);

    expect(screen.getByText("这是正在流式输出的答案。")).toBeTruthy();
    expect(screen.getByText("搜索网页", { selector: ".activity-cell-name" })).toBeTruthy();
    expect(screen.queryByRole("status", { name: "正在处理" })).toBeNull();
  });

  it("shows provisional narration in the work zone while the turn is still running", () => {
    // Unphased providers (chat-completions, Anthropic Messages) cannot label
    // text as commentary or answer while it streams. That provisional text has
    // to be on screen during the turn instead of appearing all at once when the
    // provider finally settles the item.
    const turn: ChatTurnState = {
      id: "turn-live-narration",
      userCell: null,
      committedCells: [{
        kind: "thinking",
        id: "pending-message",
        content: "我先检查相关文件。",
        source: "model_preamble",
        isStreaming: true,
        createdAt: 1,
      }],
      activeCell: null,
      finalAnswerCell: null,
      status: "streaming",
      startedAt: 1,
    };

    const { container } = render(<ChatTurn turn={turn} />);

    expect(screen.getByText("我先检查相关文件。")).toBeTruthy();
    expect(container.querySelector('[data-zone="work"]')).toBeTruthy();
    expect(container.querySelector('[data-zone="reply"]')).toBeNull();
    expect(screen.queryByRole("status", { name: "正在处理" })).toBeNull();
  });
});

describe("ChatTurn reasoning summary", () => {
  it("renders a completed provider summary in the expanded work trace", () => {
    const turn: ChatTurnState = {
      id: "turn-summary",
      userCell: null,
      committedCells: [{
        kind: "thinking",
        id: "reasoning-summary",
        content: "已核对提交链路并定位重复请求。",
        source: "provider",
        providerReasoningType: "reasoning_summary_text",
        isStreaming: false,
        createdAt: 1,
      }],
      activeCell: null,
      finalAnswerCell: {
        kind: "assistant_markdown",
        id: "answer-summary",
        messageId: "answer-summary",
        markdownSource: "修复完成。",
        phase: "final",
        copyable: true,
        createdAt: 2,
      },
      status: "completed",
      startedAt: 1,
      completedAt: 2,
    };

    render(<ChatTurn turn={turn} defaultProcessExpanded />);

    expect(screen.getByText("已核对提交链路并定位重复请求。")).toBeTruthy();
    expect(screen.getByText("修复完成。")).toBeTruthy();
    expect(document.querySelector(".thinking-cell-summary")).toBeTruthy();
  });
});

describe("ChatTurn message interactions", () => {
  it("does not open a duplicate message context menu on right click", () => {
    const turn: ChatTurnState = {
      id: "turn-no-context-menu",
      userCell: {
        kind: "user_message",
        id: "user-no-context-menu",
        content: "请生成一张图片",
        createdAt: 1,
      },
      committedCells: [],
      activeCell: null,
      finalAnswerCell: null,
      status: "completed",
      startedAt: 1,
      completedAt: 2,
    };

    render(<ChatTurn turn={turn} />);
    fireEvent.contextMenu(screen.getByText("请生成一张图片"));

    expect(screen.queryByText("复制消息")).toBeNull();
    expect(screen.queryByText("复制为 Markdown")).toBeNull();
    expect(screen.queryByText("在对话中搜索")).toBeNull();
  });
});

describe("ChatTurn", () => {
  it("keeps one aggregate edit card after the final reply", () => {
    const turn: ChatTurnState = {
      id: "assistant-diff",
      turnId: "turn-diff",
      userCell: null,
      committedCells: [
        {
          kind: "exec",
          id: "test-command",
          command: "npm test",
          status: "success",
          stdoutPreview: ["passed"],
          stderrPreview: [],
          collapsed: false,
          createdAt: 2,
        },
        {
          kind: "diff",
          id: "edit-all",
          status: "updated",
          files: [
            { path: "src/app.ts", additions: 2, deletions: 1, patch: "@@ -1 +1 @@\n-old\n+new" },
            { path: "src/app.test.ts", additions: 3, deletions: 0, patch: "@@ -1,0 +2,3 @@\n+test" },
          ],
          summary: { added: 5, deleted: 1, modifiedFiles: 2 },
          collapsed: false,
          createdAt: 3,
        },
      ],
      activeCell: null,
      finalAnswerCell: {
        kind: "assistant_markdown",
        id: "answer-diff",
        messageId: "assistant-diff",
        markdownSource: "修改完成。",
        phase: "final",
        copyable: true,
        createdAt: 3,
      },
      status: "completed",
      startedAt: 1,
      completedAt: 3,
    };

    render(<ChatTurn turn={turn} defaultProcessExpanded />);

    expect(screen.getAllByText("已编辑 2 个文件")).toHaveLength(1);
    expect(screen.getByTitle("npm test")).toBeTruthy();
    expect(screen.getByText("修改完成。")).toBeTruthy();
    const workArea = screen.getByLabelText("Agent 处理进度");
    const replyArea = screen.getByLabelText("Agent 回复");
    const diffArea = screen.getByLabelText("文件修改");
    expect(workArea.querySelectorAll(".diff-cell")).toHaveLength(0);
    expect(diffArea.querySelectorAll(".diff-cell")).toHaveLength(1);
    expect(replyArea.querySelector(".diff-cell")).toBeNull();
    expect(replyArea.compareDocumentPosition(diffArea) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const processText = workArea.textContent ?? "";
    expect(processText).toContain("npm test");
    expect(processText).not.toContain("src/app.ts");
    expect(diffArea.textContent?.indexOf("src/app.ts")).toBeLessThan(diffArea.textContent?.indexOf("src/app.test.ts"));
  });

  it("renders committed file evidence without a second live-diff dispatcher", () => {
    const turn: ChatTurnState = {
      id: "assistant-diff-fallback",
      turnId: "turn-diff-fallback",
      userCell: null,
      committedCells: [{
        kind: "diff",
        id: "fallback-diff",
        status: "updated",
        files: [{ path: "src/app.ts", additions: 2, deletions: 1 }],
        summary: { added: 2, deleted: 1, modifiedFiles: 1 },
        collapsed: false,
        createdAt: 1,
      }],
      activeCell: null,
      finalAnswerCell: null,
      status: "completed",
      startedAt: 1,
    };

    render(<ChatTurn turn={turn} defaultProcessExpanded />);

    expect(screen.getByText("已编辑 1 个文件")).toBeTruthy();
  });

  it("projects parent messages and closed agents as MiniCode-style collapsible transcript rows", () => {
    const turn: ChatTurnState = {
      id: "assistant-collaboration",
      userCell: null,
      committedCells: [
        {
          kind: "collaboration",
          id: "collaboration-start-kepler",
          action: "sent_message",
          status: "success",
          entries: [{ agentId: "Kepler", agentLabel: "Kepler", content: "请完整审计渲染链路并返回证据。" }],
          collapsed: false,
          createdAt: 2,
        },
        {
          kind: "collaboration",
          id: "collaboration-message-kepler",
          action: "sent_message",
          status: "success",
          entries: [{ agentId: "Kepler", agentLabel: "Kepler", content: "优先核对真实生产问题。" }],
          collapsed: false,
          createdAt: 3,
        },
        {
          kind: "collaboration",
          id: "collaboration-start-kant",
          action: "sent_message",
          status: "success",
          entries: [{ agentId: "Kant", agentLabel: "Kant", content: "复核测试覆盖。" }],
          collapsed: false,
          createdAt: 4,
        },
        {
          kind: "collaboration",
          id: "collaboration-close-kant",
          action: "closed",
          status: "success",
          entries: [{ agentId: "Kant", agentLabel: "Kant" }],
          collapsed: false,
          createdAt: 5,
        },
      ],
      activeCell: null,
      finalAnswerCell: null,
      status: "streaming",
      startedAt: 1,
    };

    render(<ChatTurn turn={turn} />);

    expect(screen.getAllByText("已发送消息 1 个智能体")).toHaveLength(3);
    expect(screen.getByText("请完整审计渲染链路并返回证据。")).toBeTruthy();
    expect(screen.getByText("优先核对真实生产问题。")).toBeTruthy();
    expect(screen.getByText("已关闭 1 个智能体")).toBeTruthy();
    expect(screen.getAllByText("Kant", { selector: "strong" })).toHaveLength(2);
  });

  it("keeps the completed tool transcript visible and renders the final answer separately", () => {
    const turn: ChatTurnState = {
      id: "turn-1",
      userCell: null,
      committedCells: [{
        kind: "activity",
        id: "activity-1",
        activityKind: "genericTool",
        title: "Ran explicit tool",
        status: "done",
        collapsed: false,
        startedAt: 1,
      }],
      activeCell: null,
      finalAnswerCell: {
        kind: "assistant_markdown",
        id: "answer-1",
        messageId: "assistant-1",
        markdownSource: "Implementation complete.",
        phase: "final",
        copyable: true,
        createdAt: 2,
      },
      status: "completed",
      startedAt: 1,
      completedAt: 2_001,
    };

    render(<ChatTurn turn={turn} />);

    expect(screen.getByText("Implementation complete.")).toBeTruthy();
    expect(screen.queryByText("Ran explicit tool")).toBeNull();
    expect(screen.getByText("已处理 2 秒")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "展开处理步骤" }));
    expect(screen.getByText("Ran explicit tool", { selector: ".activity-cell-name" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "收起处理步骤" }));
    expect(screen.queryByText("Ran explicit tool", { selector: ".activity-cell-name" })).toBeNull();
    expect(screen.queryByText("Ran explicit tool")).toBeNull();
  });

  it("keeps raw URLs visible when a settled turn has no complete final answer", () => {
    const command = "https://wttr.in/Beijing?format=%l:+%c+%t+(%f)+wind:%w";
    const turn: ChatTurnState = {
      id: "turn-url",
      userCell: null,
      committedCells: [{
        kind: "exec",
        id: "exec-url",
        command,
        status: "success",
        stdoutPreview: ["Beijing: sunny"],
        stderrPreview: [],
        collapsed: false,
        createdAt: 1,
      }],
      activeCell: null,
      finalAnswerCell: null,
      status: "completed",
      startedAt: 1,
      completedAt: 26_001,
      durationMs: 26_000,
    };

    render(<ChatTurn turn={turn} />);

    expect(screen.getByTitle(command)).toBeTruthy();
    expect(screen.getByText("已处理 26 秒")).toBeTruthy();
    expect(screen.queryByText("1 个工具")).toBeNull();
    expect(screen.queryByText("1 条命令")).toBeNull();
    expect(screen.queryByRole("button", { name: "展开处理步骤" })).toBeNull();
  });

  it("renders a failed turn error as process output without fabricating an answer", () => {
    const turn: ChatTurnState = {
      id: "turn-failed",
      userCell: null,
      committedCells: [{
        kind: "error",
        id: "error-1",
        title: "Tool failed",
        message: "Process exited with code 1",
        recoverable: false,
        createdAt: 1,
      }],
      activeCell: null,
      finalAnswerCell: null,
      status: "failed",
      startedAt: 1,
      completedAt: 1_001,
    };

    render(<ChatTurn turn={turn} />);
    expect(screen.queryByLabelText("Agent reply")).toBeNull();
    expect(screen.getByText("Process exited with code 1")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "展开处理步骤" })).toBeNull();
  });

  it("keeps streaming commentary before the running tool without creating a reply area", () => {
    const turn: ChatTurnState = {
      id: "turn-commentary",
      userCell: null,
      committedCells: [
        {
          kind: "thinking",
          id: "commentary-1",
          content: "重新发起，将这些调研任务标记为只读以并行执行。",
          source: "commentary",
          isStreaming: true,
          createdAt: 1,
        },
        {
          kind: "activity",
          id: "tool-1",
          activityKind: "genericTool",
          title: "Start subagent",
          status: "running",
          collapsed: false,
          startedAt: 2,
        },
      ],
      activeCell: null,
      finalAnswerCell: null,
      status: "streaming",
      startedAt: 1,
    };

    render(<ChatTurn turn={turn} />);

    const workArea = screen.getByLabelText("Agent 处理进度");
    const workText = workArea.textContent || "";
    expect(workText.indexOf("重新发起，将这些调研任务标记为只读以并行执行。"))
      .toBeLessThan(workText.indexOf("正在执行工具"));
    expect(screen.queryByLabelText("Agent reply")).toBeNull();
    expect(screen.queryByLabelText("Assistant response")).toBeNull();
  });

  it("uses the running tool row instead of a duplicate processing status", () => {
    const runningTurn: ChatTurnState = {
      id: "turn-lifecycle",
      userCell: null,
      committedCells: [{
        kind: "activity",
        id: "tool-lifecycle",
        activityKind: "genericTool",
        title: "Inspecting projection",
        status: "running",
        collapsed: false,
        startedAt: 1,
      }],
      activeCell: null,
      finalAnswerCell: null,
      status: "streaming",
      startedAt: 1,
    };
    const { rerender } = render(<ChatTurn turn={runningTurn} />);

    expect(screen.queryByRole("button", { name: "收起处理步骤" })).toBeNull();
    expect(screen.queryByRole("status", { name: "正在处理" })).toBeNull();
    expect(screen.getByText("正在执行工具", { selector: ".activity-cell-name" })).toBeTruthy();
    const workArea = screen.getByLabelText("Agent 处理进度");
    const summary = workArea.querySelector('[data-position="bottom"]');
    const timeline = workArea.querySelector(".agent-loop-timeline");
    expect(summary).toBeNull();
    expect(timeline).toBeTruthy();

    rerender(<ChatTurn turn={{
      ...runningTurn,
      committedCells: [{
        ...runningTurn.committedCells[0],
        kind: "activity",
        status: "done",
        completedAt: 2_001,
      }],
      status: "completed",
      completedAt: 2_001,
      durationMs: 2_000,
      finalAnswerCell: {
        kind: "assistant_markdown",
        id: "answer-lifecycle",
        messageId: "answer-lifecycle",
        markdownSource: "Projection complete.",
        phase: "final",
        copyable: true,
        createdAt: 2_001,
      },
    }} />);

    expect(screen.getByRole("button", { name: "展开处理步骤" }).getAttribute("aria-expanded")).toBe("false");
    expect(screen.getByText("已处理 2 秒")).toBeTruthy();
    expect(screen.queryByText("Inspecting projection", { selector: ".activity-cell-name" })).toBeNull();
  });

  it("shows processing only after the tool completes and before the next output", () => {
    const turn: ChatTurnState = {
      id: "turn-between-tool-and-output",
      userCell: null,
      committedCells: [{
        kind: "activity",
        id: "search-complete",
        activityKind: "webSearch",
        title: "搜索网页",
        status: "done",
        collapsed: true,
        startedAt: 1,
        completedAt: 2,
      }],
      activeCell: null,
      finalAnswerCell: null,
      status: "streaming",
      startedAt: 1,
    };

    render(<ChatTurn turn={turn} />);

    const workArea = screen.getByLabelText("Agent 处理进度");
    const timeline = workArea.querySelector(".agent-loop-timeline");
    const summary = workArea.querySelector('[data-position="bottom"]');
    expect(screen.getByText("搜索网页", { selector: ".activity-cell-name" })).toBeTruthy();
    expect(screen.getByRole("status", { name: "正在处理" })).toBeTruthy();
    expect(timeline).toBeTruthy();
    expect(summary).toBeTruthy();
    expect(Array.from(workArea.children).indexOf(timeline as Element))
      .toBeLessThan(Array.from(workArea.children).indexOf(summary as Element));
  });

  it("applies transcript defaults until the user chooses a disclosure state", () => {
    const turn: ChatTurnState = {
      id: "turn-transcript-default",
      userCell: null,
      committedCells: [{
        kind: "activity",
        id: "tool-transcript-default",
        activityKind: "genericTool",
        title: "Inspecting delegated work",
        status: "done",
        collapsed: false,
        startedAt: 1,
        completedAt: 2,
      }],
      activeCell: null,
      finalAnswerCell: {
        kind: "assistant_markdown",
        id: "answer-transcript-default",
        messageId: "answer-transcript-default",
        markdownSource: "Delegated work complete.",
        phase: "final",
        copyable: true,
        createdAt: 2,
      },
      status: "completed",
      startedAt: 1,
      completedAt: 2,
    };
    const { rerender } = render(<ChatTurn turn={turn} defaultProcessExpanded={false} />);

    expect(screen.queryByText("Inspecting delegated work", { selector: ".activity-cell-name" })).toBeNull();
    rerender(<ChatTurn turn={turn} defaultProcessExpanded />);
    expect(screen.getByText("Inspecting delegated work", { selector: ".activity-cell-name" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "收起处理步骤" }));
    rerender(<ChatTurn turn={turn} defaultProcessExpanded={false} />);
    rerender(<ChatTurn turn={turn} defaultProcessExpanded />);
    expect(screen.queryByText("Inspecting delegated work", { selector: ".activity-cell-name" })).toBeNull();
  });
});
