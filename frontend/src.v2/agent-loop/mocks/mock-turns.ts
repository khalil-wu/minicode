import type { AgentTurnState } from "../types";

const now = "2026-06-17T08:00:00.000Z";

export const simpleWeatherSearch: AgentTurnState = {
  turnId: "mock-weather",
  status: "completed",
  userMessage: { id: "user-weather", content: "查一下今天上海天气", createdAt: now },
  timeline: [
    {
      id: "weather-summary",
      type: "process",
      kind: "action_summary",
      source: "runtime",
      seq: 1,
      content: "正在搜索实时信息并核对来源。",
      status: "completed",
    },
    {
      id: "weather-search",
      type: "activity_group",
      activityKind: "web_search",
      seq: 2,
      title: "已搜索实时信息",
      summary: "2 次搜索",
      status: "completed",
      defaultCollapsed: true,
      details: [
        { kind: "source", title: "上海天气", url: "https://weather.example/shanghai" },
      ],
    },
  ],
  finalAnswer: { id: "answer-weather", content: "上海今天多云，建议带伞备用。", status: "completed" },
  summary: { durationMs: 5400, commandCount: 0, searchCount: 2, readCount: 0, editedFileCount: 0, sourceCount: 1, testCount: 0 },
  ui: { processCollapsed: true, expandedItemIds: [] },
};

export const newsResearchTask: AgentTurnState = {
  turnId: "mock-news",
  status: "completed",
  userMessage: { id: "user-news", content: "整理今天 AI 新闻", createdAt: now },
  timeline: [
    { id: "news-plan", type: "process", kind: "process_text", source: "model", seq: 1, content: "我先搜索新闻，再打开主要来源交叉核对。", status: "completed" },
    {
      id: "news-search",
      type: "activity_group",
      activityKind: "web_search",
      seq: 2,
      title: "已搜索实时信息",
      summary: "3 次搜索",
      status: "completed",
      defaultCollapsed: true,
      details: [{ kind: "source", title: "AI news search", url: "https://news.example/ai" }],
    },
    {
      id: "news-read",
      type: "activity_group",
      activityKind: "web_read",
      seq: 3,
      title: "已读取网页资料",
      summary: "6 个来源",
      status: "completed",
      defaultCollapsed: true,
      details: [{ kind: "source", title: "Source summary", excerpt: "Multiple sources were checked." }],
    },
  ],
  finalAnswer: { id: "answer-news", content: "今天 AI 新闻主要集中在模型发布、监管和企业落地。", status: "completed" },
  summary: { durationMs: 102000, commandCount: 0, searchCount: 3, readCount: 6, editedFileCount: 0, sourceCount: 6, testCount: 0 },
  ui: { processCollapsed: true, expandedItemIds: [] },
};

export const codeTaskPassed: AgentTurnState = {
  turnId: "mock-code-pass",
  status: "completed",
  userMessage: { id: "user-code-pass", content: "把聊天流改成 AgentLoop 工作台", createdAt: now },
  timeline: [
    { id: "code-read-note", type: "process", kind: "process_text", source: "model", seq: 1, content: "我先检查当前消息流和事件投影结构。", status: "completed" },
    {
      id: "code-read",
      type: "activity_group",
      activityKind: "file_read",
      seq: 2,
      title: "已读取相关文件",
      summary: "5 个文件",
      status: "completed",
      defaultCollapsed: true,
      details: [{ kind: "text", title: "Files", content: "ChatTurn.tsx, chatSurfaceState.ts, cells.css" }],
    },
    {
      id: "code-diff",
      type: "file_changes",
      seq: 3,
      added: 255,
      removed: 46,
      files: [
        { path: "frontend/src.v2/chat/components/ChatTurn.tsx", added: 70, removed: 18, status: "modified" },
        { path: "frontend/src.v2/agent-loop/components/AgentTurn.tsx", added: 96, removed: 0, status: "created" },
      ],
      actions: { canReview: true, canUndo: true },
    },
    {
      id: "code-test",
      type: "activity_group",
      activityKind: "test",
      seq: 4,
      title: "已运行测试",
      summary: "2 组测试",
      status: "completed",
      defaultCollapsed: true,
      details: [{ kind: "shell", title: "Vitest", command: "npm exec vitest run", exitCode: 0 }],
    },
  ],
  finalAnswer: { id: "answer-code-pass", content: "完成了：过程、工具证据、文件变更和最终回答已经分层。", status: "completed" },
  summary: { durationMs: 1292000, commandCount: 2, searchCount: 0, readCount: 5, editedFileCount: 2, sourceCount: 5, testCount: 2 },
  ui: { processCollapsed: true, expandedItemIds: [] },
};

export const failedThenFixedTask: AgentTurnState = {
  ...codeTaskPassed,
  turnId: "mock-code-fail-fix",
  timeline: [
    ...codeTaskPassed.timeline,
    { id: "failed-note", type: "process", kind: "observation", source: "runtime", seq: 5, content: "测试未通过：ActivityGroup 的 collapsed 默认值和旧用例不一致。", status: "completed" },
    {
      id: "fix-test",
      type: "activity_group",
      activityKind: "test",
      seq: 6,
      title: "已重新运行测试",
      summary: "通过",
      status: "completed",
      defaultCollapsed: true,
      details: [{ kind: "shell", title: "Vitest", command: "npm exec vitest run src.v2/chat/components/ChatTurn.test.tsx", exitCode: 0 }],
    },
  ],
};

export const contextCompactedTask: AgentTurnState = {
  ...simpleWeatherSearch,
  turnId: "mock-compact",
  timeline: [
    ...simpleWeatherSearch.timeline,
    { id: "compact-status", type: "system_status", seq: 3, content: "上下文已自动压缩", tone: "subtle" },
  ],
};

export const stoppedTask: AgentTurnState = {
  turnId: "mock-stopped",
  status: "stopped",
  userMessage: { id: "user-stopped", content: "继续搜索更多资料", createdAt: now },
  timeline: [
    { id: "stop-summary", type: "process", kind: "action_summary", source: "runtime", seq: 1, content: "正在搜索实时信息并核对来源。", status: "completed" },
    { id: "stop-status", type: "system_status", seq: 2, content: "任务已停止", tone: "subtle" },
  ],
  summary: { durationMs: 9000, commandCount: 0, searchCount: 1, readCount: 0, editedFileCount: 0, sourceCount: 0, testCount: 0 },
  ui: { processCollapsed: false, expandedItemIds: [] },
};

export const mockAgentLoopTurns = [
  simpleWeatherSearch,
  newsResearchTask,
  codeTaskPassed,
  failedThenFixedTask,
  contextCompactedTask,
  stoppedTask,
];
