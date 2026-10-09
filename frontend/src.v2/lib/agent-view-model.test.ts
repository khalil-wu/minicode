import { describe, expect, it } from "vitest";
import {
  projectAgentViews,
  sanitizeAgentResultContent,
  visibleAgentChips,
  agentStatusSummary,
  agentsForReferences,
} from "./agent-view-model";

describe("agent view model", () => {
  it("resolves large agent trees with one relationship scan and preserves exact identity over ambiguous names", () => {
    let relationshipReads = 0;
    const agents = Array.from({ length: 1000 }, (_, index) => ({ id: `agent-${index}`, taskId: `task-${index}`,
      agentPath: `/root/${index}`, role: "explore", status: "running" as const, teammateName: index < 2 ? "same" : `Name ${index}`,
      get parentRunId() { relationshipReads++; return index ? "agent-0" : undefined; },
    }));
    const views = projectAgentViews(agents);
    expect(views[0].children).toHaveLength(999);
    expect(views[1].parent?.id).toBe("agent-0");
    expect(relationshipReads).toBeLessThanOrEqual(4000);
    expect(agentsForReferences(agents, ["same", "agent-1", "/root/2", "Name 3"]).map((agent) => agent.id))
      .toEqual(["agent-1", "agent-2", "agent-3"]);
  });
  it("projects only real delegated work and sorts actionable states first", () => {
    const views = projectAgentViews([
      { id: "subagent-done", role: "reviewer", status: "done", summary: "Review complete" },
      { id: "subagent-running", role: "explore", status: "running", objective: "Audit UI", currentActivity: "Checking spacing" },
      { id: "subagent-error", role: "verification", status: "error", summary: "Tests failed", resultError: "Verification failed" },
      { id: "workflow-audit", role: "workflow", status: "running", workflowName: "Audit" },
      { id: "message-audit", role: "message", status: "running", summary: "internal notification" },
    ]);

    expect(views.map((view) => view.id)).toEqual([
      "subagent-error",
      "subagent-running",
      "subagent-done",
    ]);
    expect(views[1]).toMatchObject({
      title: "Audit UI",
      summary: "Checking spacing",
      statusLabel: "运行中",
      canStop: true,
    });
  });

  it("keeps agents in creation order while progress timestamps change", () => {
    const source = [
      { id: "subagent-beijing", role: "explore", status: "running", objective: "北京明天天气", lastProgressAt: 100 },
      { id: "subagent-shanghai", role: "explore", status: "running", objective: "上海明天天气", lastProgressAt: 300 },
      { id: "subagent-guangzhou", role: "explore", status: "running", objective: "广州明天天气", lastProgressAt: 200 },
    ] as const;
    const views = projectAgentViews([...source]);

    expect(views.map((view) => view.id)).toEqual([
      "subagent-beijing",
      "subagent-shanghai",
      "subagent-guangzhou",
    ]);
    expect(source.map((agent) => agent.id)).toEqual([
      "subagent-beijing",
      "subagent-shanghai",
      "subagent-guangzhou",
    ]);
  });

  it("does not reorder the store-owned source array while ranking views", () => {
    const source = [
      { id: "done", role: "reviewer", status: "done" as const },
      { id: "running", role: "explore", status: "running" as const },
      { id: "error", role: "verification", status: "error" as const },
    ];

    expect(projectAgentViews(source).map((view) => view.id)).toEqual([
      "error",
      "running",
      "done",
    ]);
    expect(source.map((agent) => agent.id)).toEqual(["done", "running", "error"]);
  });

  it("projects stable identity and relative completion time", () => {
    const now = Date.parse("2026-07-19T12:00:00Z");
    const [view] = projectAgentViews([{
      id: "subagent-review",
      role: "reviewer",
      status: "done",
      objective: "检查实现",
      lastEventAt: now - 2 * 60 * 60 * 1000,
    }], now);

    expect(view).toMatchObject({
      identityKey: "subagent-review",
      relativeTimeLabel: "2 小时",
      status: "completed",
    });
  });

  it("keeps identities stable across status changes and only resolves known relationships", () => {
    const agents = [
      { id: "parent", agentPath: "/root/review", role: "reviewer", status: "running" as const, objective: "审阅界面" },
      { id: "worker", agentPath: "/root/review/layout", role: "explore", status: "blocked" as const, parentRunId: "parent", blockedBy: ["schema-task", "unknown"], objective: "优化布局" },
      { id: "schema", taskId: "schema-task", role: "explore", status: "running" as const, objective: "检查结构" },
    ];
    const view = projectAgentViews(agents).find((agent) => agent.id === "worker")!;
    expect(view.identityKey).toBe("/root/review/layout");
    expect(view.parent).toMatchObject({ id: "parent", title: "审阅界面" });
    expect(view.blockedDependencies).toEqual([{ id: "schema", title: "检查结构", identityKey: "schema" }]);
    expect(projectAgentViews([{ ...agents[1], status: "done" }])[0].identityKey).toBe(view.identityKey);
  });

  it("counts exact states without reporting stopped or failed work as running", () => {
    const views = projectAgentViews([
      { id: "a", role: "explore", status: "running" },
      { id: "b", role: "explore", status: "error" },
      { id: "c", role: "explore", status: "cancelled", terminationInitiator: "user" },
      { id: "d", role: "explore", status: "blocked", awaitingPlanApproval: true },
      { id: "e", role: "explore", status: "running", isIdle: true },
      { id: "f", role: "explore", status: "done", awaitingPlanApproval: true, needsInput: true },
    ]);
    expect(agentStatusSummary(views)).toBe("1 失败 · 1 已停止 · 1 等待批准计划 · 1 运行中 · 1 待命 · 1 已完成");
    expect(views.find((view) => view.id === "f")!.awaitingPlanApproval).toBe(false);
  });

  it("limits compact conversation previews and reports the remainder", () => {
    const result = visibleAgentChips(Array.from({ length: 5 }, (_, index) => ({
      id: `subagent-${index}`,
      role: "explore",
      status: "running" as const,
      objective: `调研目标 ${index + 1}`,
    })));

    expect(result.agents).toHaveLength(3);
    expect(result.hiddenCount).toBe(2);
  });

  it("keeps runtime diagnostics out of the ordinary UI contract", () => {
    const [view] = projectAgentViews([{
      id: "subagent-weather",
      role: "explore",
      status: "done",
      objective: "查询石家庄天气",
      summary: "石家庄天气调研完成",
      currentActivity: "Running call_0ee211964b704028a47cfbfe",
      currentTool: "call_0ee211964b704028a47cfbfe",
      currentToolCallId: "call_0ee211964b704028a47cfbfe",
      detail: "46.1s elapsed",
      iteration: 4,
      maxIterations: 8,
      toolCallCount: 12,
      nodeId: "weather-node",
    }]);

    expect(view.summary).toBe("石家庄天气调研完成");
    expect(JSON.stringify(view)).not.toMatch(/call_0ee|46\.1s|iteration|maxIterations|toolCallCount|source/);
  });

  it("does not rewrite agent titles or summaries based on their wording", () => {
    const [view] = projectAgentViews([{
      id: "subagent-a7bb47d3",
      role: "explore",
      status: "running",
      objective: "Agent 1",
      summary: "subagent-a7bb47d3",
    }]);

    expect(view.title).toBe("Agent 1");
    expect(view.summary).toBe("subagent-a7bb47d3");
  });

  it("preserves delegated result content without parsing its prose", () => {
    const source = [
      "### 天气结论",
      "",
      "Running call_a7bb47d3d431442bb8ade914",
      "46.1s elapsed",
      "Tools used (4 total):",
      "- read_file(file_path=C:\\weather.json)",
      "<task-notification>",
      "<task-id>subagent-weather</task-id>",
      "</task-notification>",
      "Ready / launched",
      "Waiting on dependencies",
      "Workflow mode: pipeline",
      "task output: [completed] weather research",
      "- 石家庄今天晴，最高 31°C",
    ].join("\n");
    expect(sanitizeAgentResultContent(source)).toBe(source);
  });

  it("preserves system reminder text in delegated results and errors", () => {
    const [view] = projectAgentViews([{
      id: "subagent-loop",
      role: "explore",
      status: "error",
      objective: "检查模块",
      summary: "执行失败\n<system-reminder>工具循环警告 repeated_exact_failure</system-reminder>",
      resultContent: "已完成部分检查。\n<system-reminder>内部恢复指令</system-reminder>",
      resultError: "检索参数重复。\n<system-reminder>工具循环警告 repeated_exact_failure</system-reminder>",
    }]);

    expect(view.summary).toBe("检索参数重复。\n<system-reminder>工具循环警告 repeated_exact_failure</system-reminder>");
    expect(view.resultContent).toContain("<system-reminder>内部恢复指令</system-reminder>");
    expect(view.resultError).toContain("<system-reminder>工具循环警告 repeated_exact_failure</system-reminder>");
  });

  it("uses the typed summary verbatim", () => {
    const [view] = projectAgentViews([{
      id: "subagent-weather",
      role: "explore",
      status: "done",
      objective: "查询北京天气",
      summary: "## 结论\n\n**北京今天晴，最高 31°C。**",
      resultContent: "## 结论\n\n**北京今天晴，最高 31°C。**\n\n## 来源\n- 官方预报",
    }]);

    expect(view.summary).toBe("## 结论\n\n**北京今天晴，最高 31°C。**");
    expect(view.title).toBe("查询北京天气");
  });

  it("preserves delegated-report envelope headings in visible results", () => {
    const content = sanitizeAgentResultContent([
      "## Result",
      "- 成都今天多云，最高 31°C。",
      "## Evidence",
      "- 已核对官方天气来源。",
      "## Changes",
      "- None.",
      "## Verification",
      "- 来源可访问。",
      "## Risks or blockers",
      "- None.",
    ].join("\n"));

    expect(content).toContain("成都今天多云，最高 31°C");
    expect(content).toContain("已核对官方天气来源");
    expect(content).toMatch(/^##\s+(?:Result|Evidence|Changes|Verification|Risks or blockers)$/m);
  });

  it("does not expose dependency ids when delegated work is waiting", () => {
    const [view] = projectAgentViews([{
      id: "subagent-review",
      role: "reviewer",
      status: "blocked",
      objective: "检查持久化方案",
      blockedBy: ["task-schema-4f9d"],
    }]);

    expect(view).toMatchObject({
      status: "waiting",
      statusLabel: "等待中",
      summary: "等待前置任务完成",
      canStop: true,
    });
    expect(JSON.stringify(view)).not.toContain("task-schema-4f9d");
  });

  it.each(["pending", "running", "blocked"] as const)("makes a %s agent's request for user input explicit and actionable", (status) => {
    const [view] = projectAgentViews([{
      id: "subagent-input",
      role: "reviewer",
      status,
      objective: "确认发布范围",
      needsInput: true,
      waitingOn: "请选择是否包含迁移脚本",
    }]);

    expect(view).toMatchObject({
      status: "attention",
      statusLabel: "等待你回复",
      summary: "请选择是否包含迁移脚本",
      canStop: true,
    });
  });

  it("offers result recovery when a terminal worker only stored whitespace", () => {
    const [view] = projectAgentViews([{
      id: "subagent-empty-result",
      role: "verification",
      status: "done",
      resultContent: "   ",
      resultError: "\n",
    }]);

    expect(view.resultContent).toBeUndefined();
    expect(view.resultError).toBeUndefined();
    expect(view.needsResult).toBe(true);
  });

  it("presents deadline, partial and cancelled states in user language", () => {
    const views = projectAgentViews([
      {
        id: "subagent-deadline",
        role: "explore",
        status: "partial",
        terminationReason: "deadline_exceeded",
      },
      {
        id: "subagent-partial",
        role: "reviewer",
        status: "partial",
      },
      {
        id: "subagent-cancelled",
        role: "verification",
        status: "cancelled",
        terminationInitiator: "user",
      },
    ]);

    expect(views.map((view) => [view.statusLabel, view.summary])).toEqual([
      ["已保留结果", "已保留可用结果"],
      ["部分完成", "已完成部分工作"],
      ["已停止", "已由你停止"],
    ]);
  });

  it("keeps a worker failure visible after the parent turn ends", () => {
    const [view] = projectAgentViews([{
      id: "subagent-weather",
      role: "explore",
      status: "error",
      objective: "查询北京天气",
      resultError: "deadline exceeded",
      activityLog: ["搜索：查询北京天气", "执行未完成"],
    }]);

    expect(view).toMatchObject({
      status: "attention",
      statusLabel: "失败",
      summary: "deadline exceeded",
      resultContent: undefined,
      resultError: "deadline exceeded",
    });
    expect(view.activityLog).toEqual(["搜索：查询北京天气", "执行未完成"]);
  });

  it("does not project the same terminal error twice", () => {
    const [view] = projectAgentViews([{
      id: "subagent-weather",
      role: "explore",
      status: "error",
      objective: "查询成都天气",
      resultError: "RuntimeError: 已达到最大迭代次数限制（12次）。",
      resultContent: "RuntimeError: 已达到最大迭代次数限制（12次）。",
    }]);

    expect(view.resultError).toBe("RuntimeError: 已达到最大迭代次数限制（12次）。");
    expect(view.resultContent).toBeUndefined();
  });

  it("keeps actionable error text that mentions a subagent id", () => {
    const [view] = projectAgentViews([{
      id: "subagent-weather",
      role: "explore",
      status: "error",
      objective: "查询成都天气",
      resultError: "Subagent subagent-weather failed: connection refused",
    }]);

    expect(view.resultError).toBe("Subagent subagent-weather failed: connection refused");
  });

  it("distinguishes blocking work from background work", () => {
    const views = projectAgentViews([
      { id: "subagent-blocking", role: "explore", status: "running", background: false },
      { id: "subagent-background", role: "explore", status: "running", background: true },
    ]);

    expect(views.map((view) => view.executionMode)).toEqual(["blocking", "background"]);
  });
});
