// Scripted agent turns for the demo backend. Each scenario drives the same
// event stream a real MiniCode turn produces: run lifecycle, reasoning,
// commentary, tool calls (with streamed command output and approvals), plan
// updates and a streamed final answer.

import type { ApprovalDecision } from "./socket";
import { DEMO_WORKSPACE, state, type Json } from "./state";

export interface TurnContext {
  conversationId: string;
  assistantMessageId: string;
  runId: string;
  content: string;
  origin: Json;
  owner: Json;
  cancelled: boolean;
  finalText: string;
  title: string;
  usage: Json;
  blocks: Json[];
  toolCalls: Json[];
  emit: (payload: Json) => void;
  sleep: (ms: number) => Promise<void>;
  waitForApproval: (requestId: string) => Promise<ApprovalDecision>;
  cancel: () => void;
}

let ids = 0;
const nextId = (prefix: string) => `${prefix}_${Date.now().toString(36)}${(ids++).toString(36)}`;

const chunk = (text: string): string[] => {
  // Split into small, word-ish pieces so streaming reads like a real model.
  const pieces: string[] = [];
  let index = 0;
  while (index < text.length) {
    const size = /[一-鿿]/.test(text[index]) ? 1 + Math.floor(Math.random() * 3) : 2 + Math.floor(Math.random() * 5);
    pieces.push(text.slice(index, index + size));
    index += size;
  }
  return pieces;
};

async function begin(ctx: TurnContext): Promise<void> {
  ctx.emit({ type: "session.state_changed", state: "working", reason: "agent_run_started" });
  ctx.emit({
    type: "agent.run.started", run_id: ctx.runId, parent_run_id: "", role: "main", phase: "plan", status: "running",
    started_at: Date.now(), completed_at: null, kind: "agent", agent_path: ctx.runId, summary: "",
  });
  await ctx.sleep(260);
}

async function finish(ctx: TurnContext): Promise<void> {
  if (ctx.cancelled) return;
  const output = Math.round(ctx.finalText.length / 2.4) + 180;
  ctx.usage = {
    input_tokens: 18_400 + Math.floor(Math.random() * 6_000), output_tokens: output,
    cache_creation_input_tokens: 0, cache_read_input_tokens: 12_288,
    input_includes_cache_read: true, input_includes_cache_write: true,
  };
  ctx.emit({
    type: "agent.run.completed", run_id: ctx.runId, role: "main", phase: "final", status: "completed",
    completed_at: Date.now(), summary: "", terminal_reason: "completed", kind: "agent",
  });
  ctx.emit({ type: "done", status: "completed", usage: ctx.usage, reason: "completed" });
  ctx.emit({ type: "session.state_changed", state: "idle", reason: "completed" });
}

async function think(ctx: TurnContext, text: string): Promise<void> {
  for (const piece of chunk(text)) {
    if (ctx.cancelled) return;
    ctx.emit({
      type: "thinking_delta", content: piece, lifecycle: "delta", source: "provider", visibility: "timeline",
      phase: "model", provider_reasoning_type: "reasoning_content",
    });
    await ctx.sleep(14 + Math.random() * 18);
  }
  await ctx.sleep(180);
}

async function say(ctx: TurnContext, text: string, kind: "commentary" | "final" = "commentary"): Promise<void> {
  if (ctx.cancelled) return;
  const itemId = nextId("item");
  const source = kind === "final" ? "model_final" : "commentary";
  ctx.emit({ type: "item.started", item: { id: itemId, type: "agent_message", text: "", status: "in_progress", source: kind === "final" ? "model_final" : "pending" } });
  let streamed = "";
  for (const piece of chunk(text)) {
    if (ctx.cancelled) return;
    streamed += piece;
    ctx.emit({ type: "agent_message.delta", item_id: itemId, delta: piece });
    await ctx.sleep(kind === "final" ? 12 + Math.random() * 16 : 16 + Math.random() * 18);
  }
  ctx.emit({ type: "item.completed", item: { id: itemId, type: "agent_message", text: streamed, source, status: "completed" } });
  ctx.blocks.push({ type: "text", content: streamed, itemId, source, status: "completed", isStreaming: false });
  if (kind === "final") ctx.finalText = streamed;
  await ctx.sleep(220);
}

interface ToolSpec {
  name: string;
  args: Json;
  hint: string;
  resultKind: string;
  activityKind: string;
  summary: string;
  displaySummary?: string;
  output?: string[];
  durationMs?: number;
  failed?: boolean;
  diff?: Json;
  approval?: { reason?: string; diff?: string };
}

const toolRecord = (id: string, spec: ToolSpec, status: string, startedAt: number): Json => ({
  id, name: spec.name, status, summary: spec.summary, displaySummary: spec.displaySummary ?? "",
  resultKind: spec.resultKind, activityKind: spec.activityKind, visibility: "timeline", displayHint: spec.hint,
  args: spec.args, durationMs: spec.durationMs ?? 400, startedAt, finishedAt: startedAt + (spec.durationMs ?? 400),
  ...(spec.diff ? { diff: spec.diff } : {}),
});

/** Run one tool call; resolves false when the user rejected it. */
async function tool(ctx: TurnContext, spec: ToolSpec): Promise<boolean> {
  if (ctx.cancelled) return false;
  const id = nextId("call");
  const startedAt = Date.now();
  const base = {
    id, name: spec.name, display_hint: spec.hint, result_kind: spec.resultKind, activity_kind: spec.activityKind,
    visibility: "timeline", step_id: id, phase: "tool", started_at: startedAt,
  };
  ctx.emit({ type: "tool_call", ...base, args: {}, status: "pending" });
  await ctx.sleep(120);
  if (spec.approval && state.permissionMode !== "bypass" && state.permissionMode !== "auto") {
    ctx.emit({ type: "tool_call", ...base, args: spec.args, status: "pending" });
    ctx.emit({
      type: "permission.decision", tool_call_id: id, tool_name: spec.name, decision: "ask", source: "policy",
      permission_level: "confirm", approval_policy: "confirm", risk: spec.name === "run_command" ? "high" : "medium",
    });
    ctx.emit({
      type: "control_request", request_id: id, workspace_root: DEMO_WORKSPACE, permission_mode: state.permissionMode,
      workspace_scope: "project",
      request: {
        subtype: "can_use_tool", tool_name: spec.name, input: spec.args, tool_use_id: id,
        source_agent: ctx.runId, source_thread: ctx.conversationId, source_tool: spec.name,
        ...(spec.approval.reason ? { reason: spec.approval.reason } : {}),
        ...(spec.approval.diff ? { diff: spec.approval.diff } : {}),
      },
    });
    const decision = await ctx.waitForApproval(id);
    if (ctx.cancelled) return false;
    if (decision.action === "reject") {
      ctx.emit({ type: "tool_call", ...base, args: spec.args, status: "running" });
      const summary = `Operation rejected${decision.feedback ? `: ${decision.feedback}` : ""}`;
      ctx.emit({
        type: "tool_result", ...base, summary, is_error: true, status: "failed", duration_ms: 0,
        display_summary: "已拒绝", recoverable: true,
      });
      ctx.toolCalls.push(toolRecord(id, { ...spec, summary }, "failed", startedAt));
      ctx.blocks.push({ type: "tool_call", record: toolRecord(id, { ...spec, summary }, "failed", startedAt) });
      return false;
    }
  }
  ctx.emit({ type: "tool_call", ...base, args: spec.args, status: "running" });
  for (const line of spec.output ?? []) {
    await ctx.sleep(90 + Math.random() * 140);
    ctx.emit({ type: "tool_output_delta", id, output: line, stream: "stdout", step_id: id });
  }
  await ctx.sleep(spec.output ? 160 : Math.min(spec.durationMs ?? 400, 700));
  if (ctx.cancelled) return false;
  ctx.emit({
    type: "tool_result", ...base, summary: spec.summary, is_error: Boolean(spec.failed),
    status: spec.failed ? "failed" : "success", duration_ms: spec.durationMs ?? 400,
    display_summary: spec.displaySummary ?? "", recoverable: true, ...(spec.diff ? { diff: spec.diff } : {}),
  });
  const record = toolRecord(id, spec, spec.failed ? "failed" : "success", startedAt);
  ctx.toolCalls.push(record);
  ctx.blocks.push({ type: "tool_call", record });
  await ctx.sleep(200);
  return true;
}

async function plan(ctx: TurnContext, steps: Array<[string, "pending" | "in_progress" | "completed"]>, explanation: string | null = null): Promise<void> {
  const planItems = steps.map(([step, status]) => ({ step, status }));
  await tool(ctx, {
    name: "update_plan", args: { plan: planItems, ...(explanation ? { explanation } : {}) }, hint: "Update plan",
    resultKind: "plan", activityKind: "status", summary: "Plan updated", displaySummary: "Completed: Update plan", durationMs: 120,
  });
  ctx.emit({ type: "turn.plan.updated", thread_id: ctx.conversationId, explanation, plan: planItems });
}

const numbered = (source: string): string =>
  `${source.split("\n").map((line, index) => `${String(index + 1).padStart(6)}→${line}`).join("\n")}\n\n[content_hash: demo${source.length}]`;

const read = (path: string, source: string): ToolSpec => ({
  name: "read_file", args: { file_path: path }, hint: "Read", resultKind: "file", activityKind: "fileRead",
  summary: numbered(source), displaySummary: "Completed: Read", durationMs: 140,
});

const command = (cmd: string, output: string[], exitCode = 0): ToolSpec => ({
  name: "run_command", args: { command: cmd, cwd: DEMO_WORKSPACE }, hint: "Run", resultKind: "command",
  activityKind: "commandExecution", output: output.map((line) => `${line}\r\n`),
  summary: `Exit code: ${exitCode}\ncommand: ${cmd}\ncwd: ${DEMO_WORKSPACE}\nexit_code: ${exitCode}\n\n${output.join("\n")}`,
  displaySummary: `Exit code: ${exitCode}`, durationMs: 1_400 + Math.floor(Math.random() * 900), failed: exitCode !== 0,
});

const structuredDiff = (path: string, patch: string, additions: number, deletions: number): Json => ({
  format: "structured",
  stats: { files_count: 1, additions, deletions },
  files: [{ path: `${DEMO_WORKSPACE}/${path}`, status: "modified", additions, deletions, patch }],
});

// ── Scenario: fix a failing test ────────────────────────────────────────

const COUNTER_BEFORE = `export interface CounterOptions {
  min?: number;
  max?: number;
}

export function nextCount(count: number, step: number, _options: CounterOptions = {}): number {
  return count + step;
}`;

const COUNTER_PATCH = `--- a/src/lib/counter.ts
+++ b/src/lib/counter.ts
@@ -3,6 +3,8 @@ export interface CounterOptions {
   max?: number;
 }

-export function nextCount(count: number, step: number, _options: CounterOptions = {}): number {
-  return count + step;
+export function nextCount(count: number, step: number, options: CounterOptions = {}): number {
+  const { min = 0, max = Number.POSITIVE_INFINITY } = options;
+  const next = count + step;
+  return Math.min(max, Math.max(min, next));
 }
`;

const COUNTER_TEST = `import { describe, expect, it } from "vitest";
import { nextCount } from "./counter";

describe("nextCount", () => {
  it("adds the step", () => expect(nextCount(1, 2)).toBe(3));
  it("never goes below min", () => expect(nextCount(0, -1)).toBe(0));
  it("stops at max", () => expect(nextCount(9, 5, { max: 10 })).toBe(10));
});`;

async function fixScenario(ctx: TurnContext): Promise<void> {
  ctx.title = "修复计数器越界";
  await think(ctx, "用户希望修复计数器的问题。先跑相关测试确认失败的断言，再读实现，最小化修改后重新验证。");
  await say(ctx, "我先跑一下计数器相关的测试，确认具体是哪条断言失败。");
  await plan(ctx, [["复现失败的测试", "in_progress"], ["定位 nextCount 的实现", "pending"], ["修复并补充边界处理", "pending"], ["重新运行测试", "pending"]]);
  await tool(ctx, command("npm test -- counter", [
    "> aurora@0.4.2 test",
    "> vitest run counter",
    "",
    " RUN  v4.1.2 /Users/demo/Projects/aurora",
    "",
    " ❯ src/lib/counter.test.ts (3 tests | 2 failed) 6ms",
    "   ✓ nextCount > adds the step",
    "   × nextCount > never goes below min",
    "     → expected -1 to be 0",
    "   × nextCount > stops at max",
    "     → expected 14 to be 10",
    "",
    " Test Files  1 failed (1)",
    "      Tests  2 failed | 1 passed (3)",
  ], 1));
  await say(ctx, "两条边界用例失败：`nextCount` 直接返回 `count + step`，`min` / `max` 选项根本没有被使用。我读一下实现和测试。");
  await tool(ctx, read("src/lib/counter.ts", COUNTER_BEFORE));
  await tool(ctx, read("src/lib/counter.test.ts", COUNTER_TEST));
  await plan(ctx, [["复现失败的测试", "completed"], ["定位 nextCount 的实现", "completed"], ["修复并补充边界处理", "in_progress"], ["重新运行测试", "pending"]]);
  const approved = await tool(ctx, {
    name: "edit_file",
    args: {
      file_path: "src/lib/counter.ts",
      old_string: "export function nextCount(count: number, step: number, _options: CounterOptions = {}): number {\n  return count + step;",
      new_string: "export function nextCount(count: number, step: number, options: CounterOptions = {}): number {\n  const { min = 0, max = Number.POSITIVE_INFINITY } = options;\n  const next = count + step;\n  return Math.min(max, Math.max(min, next));",
    },
    hint: "Edit", resultKind: "edit", activityKind: "fileChange",
    summary: "Edited src/lib/counter.ts: replaced 1 occurrence(s). Diff stats: +4 -2.",
    displaySummary: "Completed: Edit", durationMs: 210,
    diff: structuredDiff("src/lib/counter.ts", COUNTER_PATCH, 4, 2),
    approval: { diff: COUNTER_PATCH },
  });
  if (!approved) {
    await say(ctx, "好的，我不会修改文件。\n\n问题出在 `src/lib/counter.ts`：`nextCount` 忽略了 `min` / `max`，直接返回 `count + step`。如果之后需要修复，把返回值改成先相加、再用 `Math.min(max, Math.max(min, next))` 夹在范围内即可。", "final");
    return finish(ctx);
  }
  ctx.emit({ type: "file.changed", path: "src/lib/counter.ts", event: "modified", workspace_root: DEMO_WORKSPACE });
  await plan(ctx, [["复现失败的测试", "completed"], ["定位 nextCount 的实现", "completed"], ["修复并补充边界处理", "completed"], ["重新运行测试", "in_progress"]]);
  await tool(ctx, command("npm test -- counter", [
    "> aurora@0.4.2 test",
    "> vitest run counter",
    "",
    " ✓ src/lib/counter.test.ts (3 tests) 4ms",
    "",
    " Test Files  1 passed (1)",
    "      Tests  3 passed (3)",
    "   Duration  412ms",
  ]));
  await plan(ctx, [["复现失败的测试", "completed"], ["定位 nextCount 的实现", "completed"], ["修复并补充边界处理", "completed"], ["重新运行测试", "completed"]]);
  await say(ctx, [
    "已修复，计数器相关的 3 条测试全部通过。",
    "",
    "**原因**　`nextCount` 声明了 `min` / `max` 选项，但实现里直接返回 `count + step`，选项从未生效，所以负步长会得到 `-1`，超出上限也不会截断。",
    "",
    "**改动**　`src/lib/counter.ts`",
    "",
    "```ts",
    "export function nextCount(count: number, step: number, options: CounterOptions = {}): number {",
    "  const { min = 0, max = Number.POSITIVE_INFINITY } = options;",
    "  const next = count + step;",
    "  return Math.min(max, Math.max(min, next));",
    "}",
    "```",
    "",
    "- 未传 `max` 时上限为无穷大，现有调用方行为不变",
    "- `min` 默认为 `0`，与组件里「不允许负数」的约定一致",
    "",
    "**验证**　`npm test -- counter` → 3 passed。",
  ].join("\n"), "final");
  await finish(ctx);
}

// ── Scenario: explain code ──────────────────────────────────────────────

const RECONNECT_SOURCE = `const BASE_DELAY_MS = 500;
const MAX_DELAY_MS = 15_000;

export function reconnectDelay(attempt: number): number {
  const exponential = Math.min(MAX_DELAY_MS, BASE_DELAY_MS * 2 ** attempt);
  const jitter = exponential * 0.2 * Math.random();
  return Math.round(exponential - jitter);
}

export function shouldReconnect(code: number): boolean {
  // 1000 = normal close, 4001 = session replaced by another window
  return code !== 1000 && code !== 4001;
}`;

async function explainScenario(ctx: TurnContext): Promise<void> {
  ctx.title = "解释重连策略";
  await think(ctx, "需要先找到重连逻辑所在的文件，读完再按调用顺序解释，最后给出可以改进的地方。");
  await say(ctx, "我先在仓库里找一下重连相关的实现。");
  await tool(ctx, {
    name: "grep_files", args: { pattern: "reconnect", path: "src", output_mode: "files_with_matches" }, hint: "Search",
    resultKind: "search", activityKind: "workspaceSearch", summary: "src/net/reconnect.ts\nsrc/net/socket.ts\nsrc/net/socket.test.ts",
    displaySummary: "Completed: Search", durationMs: 90,
  });
  await tool(ctx, read("src/net/reconnect.ts", RECONNECT_SOURCE));
  await tool(ctx, read("src/net/socket.ts", `import { reconnectDelay, shouldReconnect } from "./reconnect";

export function connect(url: string, attempt = 0): WebSocket {
  const socket = new WebSocket(url);
  socket.addEventListener("open", () => { attempt = 0; });
  socket.addEventListener("close", (event) => {
    if (!shouldReconnect(event.code)) return;
    setTimeout(() => connect(url, attempt + 1), reconnectDelay(attempt));
  });
  return socket;
}`));
  await say(ctx, [
    "重连逻辑分成两层：`reconnect.ts` 负责**算等多久、要不要重连**，`socket.ts` 负责**在关闭时调度下一次连接**。",
    "",
    "## 1. 退避时间 `reconnectDelay`",
    "",
    "每次失败等待时间翻倍，从 500ms 起步，封顶 15s，再减去最多 20% 的随机抖动：",
    "",
    "| 第几次重试 | 理论等待 | 实际范围 |",
    "| --- | --- | --- |",
    "| 0 | 500ms | 400 – 500ms |",
    "| 1 | 1s | 0.8 – 1s |",
    "| 3 | 4s | 3.2 – 4s |",
    "| ≥ 5 | 15s（封顶） | 12 – 15s |",
    "",
    "抖动的作用是避免服务重启后所有客户端在同一毫秒涌回来。",
    "",
    "## 2. 是否重连 `shouldReconnect`",
    "",
    "只有两种关闭码不会重连：`1000` 正常关闭，以及 `4001` 会话被另一个窗口接管。其余情况（网络断开、服务重启）都会进入退避重连。",
    "",
    "## 3. 调度 `connect`",
    "",
    "连接成功时把 `attempt` 归零；关闭时如果需要重连，就按当前次数计算延迟后递归调用自己。",
    "",
    "> 有一个小隐患：`open` 回调里的 `attempt = 0` 只修改了当前闭包里的参数，而 `close` 中传的是 `attempt + 1`。成功连上后再断开，下一次仍会从旧的次数继续退避。可以把计数提到模块级状态，或在 `open` 时显式重置后再传入。",
  ].join("\n"), "final");
  await finish(ctx);
}

// ── Scenario: plan a feature ────────────────────────────────────────────

async function planScenario(ctx: TurnContext): Promise<void> {
  ctx.title = "规划深色模式";
  await think(ctx, "这是一个规划类请求。先看现有的主题实现和设置项，再给出分步计划，不直接改代码。");
  await say(ctx, "我先看看现在主题是怎么组织的。");
  await tool(ctx, {
    name: "glob_files", args: { pattern: "src/**/*theme*" }, hint: "Glob", resultKind: "search", activityKind: "workspaceSearch",
    summary: "Found 3 matching files\nsrc/styles/theme.css\nsrc/lib/theme.ts\nsrc/settings/AppearancePanel.tsx", displaySummary: "Completed: Glob", durationMs: 70,
  });
  await tool(ctx, read("src/lib/theme.ts", `export type Theme = "light";

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
}`));
  await plan(ctx, [["梳理现有颜色变量", "completed"], ["引入 light / dark / system 三态", "pending"], ["设置页加入外观切换", "pending"], ["跟随系统并持久化", "pending"]],
    "主题目前只有 light 一种，颜色写死在 theme.css 中。");
  await say(ctx, [
    "目前主题只有 `light` 一种，颜色直接写在 `theme.css` 里。建议分四步做：",
    "",
    "1. **抽出语义变量**　把 `theme.css` 中的十六进制颜色替换为 `--surface`、`--text`、`--accent` 等语义变量，`:root` 下保留浅色取值。",
    "2. **三态主题**　`Theme` 扩展为 `\"light\" | \"dark\" | \"system\"`；`system` 通过 `matchMedia(\"(prefers-color-scheme: dark)\")` 解析成实际主题，并监听变化。",
    "3. **外观设置**　在 `AppearancePanel` 中加入分段控件，切换时写入 `localStorage` 并立即调用 `applyTheme`。",
    "4. **首帧无闪烁**　在 `index.html` 里内联一段极小的脚本，在样式加载前设置 `data-theme`，避免深色用户看到白屏一闪。",
    "",
    "预计改动 4 个文件，不涉及任何组件结构。需要我按这个计划开始实现吗？",
  ].join("\n"), "final");
  await finish(ctx);
}

// ── Scenario: general chat ──────────────────────────────────────────────

async function chatScenario(ctx: TurnContext): Promise<void> {
  ctx.title = ctx.content.trim().slice(0, 18) || "新对话";
  await think(ctx, "这是一个一般性的问题，直接回答，保持简洁。");
  await say(ctx, [
    "这是 **MiniCode 演示模式**：界面是真实的应用，回复由内置脚本模拟，不会连接任何模型或后端。",
    "",
    "可以试试这些说法，看看完整的工作流：",
    "",
    "- **「修复计数器测试」**　运行命令、读取文件、请求批准修改、展示 diff，再重新验证",
    "- **「解释一下重连逻辑」**　搜索代码、阅读文件，给出带表格的讲解",
    "- **「规划深色模式」**　生成分步计划",
    "",
    "也可以在左下角打开设置，或在输入框中键入 `/` 查看命令。",
  ].join("\n"), "final");
  await finish(ctx);
}

export async function runScenario(ctx: TurnContext): Promise<void> {
  await begin(ctx);
  const text = ctx.content.toLowerCase();
  if (/修复|修一下|bug|fix|测试|test|报错|失败/.test(text)) return fixScenario(ctx);
  if (/解释|讲讲|说明|explain|怎么|如何|为什么|逻辑/.test(text)) return explainScenario(ctx);
  if (/计划|规划|设计|plan|方案|深色|dark/.test(text)) return planScenario(ctx);
  return chatScenario(ctx);
}
