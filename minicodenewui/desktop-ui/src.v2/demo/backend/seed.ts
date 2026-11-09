// Pre-populated threads so the sidebar and history views have content the
// moment the demo opens.

import { DEMO_WORKSPACE, isoAgo, state, type DemoConversation, type Json } from "./state";

const user = (id: string, content: string, minutesAgo: number): Json => ({
  id, role: "user", content, timestamp: isoAgo(minutesAgo), attachments: [],
});

const tool = (id: string, name: string, args: Json, summary: string, extra: Json = {}): Json => ({
  type: "tool_call",
  record: {
    id, name, status: "success", summary, displaySummary: "", visibility: "timeline", args,
    durationMs: 320, startedAt: Date.now() - 3_600_000, finishedAt: Date.now() - 3_599_000,
    resultKind: name === "run_command" ? "command" : name.includes("edit") ? "edit" : name.includes("files") ? "search" : "file",
    activityKind: name === "run_command" ? "commandExecution" : name.includes("edit") ? "fileChange" : name.includes("files") ? "workspaceSearch" : "fileRead",
    ...extra,
  },
});

const text = (content: string, source: "commentary" | "model_final", itemId: string): Json => ({
  type: "text", content, itemId, source, status: "completed", isStreaming: false,
});

const assistant = (id: string, content: string, minutesAgo: number, blocks: Json[]): Json => ({
  id, role: "assistant", content, timestamp: isoAgo(minutesAgo), terminal_status: "completed",
  completed_at: Date.now() - minutesAgo * 60_000,
  usage: { input_tokens: 24_310, output_tokens: 612, cache_creation_input_tokens: 0, cache_read_input_tokens: 16_384, input_includes_cache_read: true },
  blocks,
  tool_calls: blocks.filter((block) => block.type === "tool_call").map((block) => block.record),
});

const conversation = (id: string, title: string, minutesAgo: number, transcript: Json[], pinned = false): DemoConversation => ({
  id, title, created_at: isoAgo(minutesAgo + 30), updated_at: isoAgo(minutesAgo), workspace_root: DEMO_WORKSPACE, transcript, pinned,
});

const RELEASE_ANSWER = `## Aurora 0.4.2

**新功能**
- 设置页新增「外观」分组，支持浅色、深色与跟随系统
- 命令面板支持模糊搜索最近打开的文件

**修复**
- 计数器在负步长时不再出现负数
- 断线重连后不再重复提交未确认的消息

**性能**
- 首屏脚本体积减少 18%，冷启动快约 120ms`;

const API_ANSWER = `超时现在统一由 \`fetchWithTimeout\` 处理，调用方只需要传 \`timeoutMs\`：

\`\`\`ts
const response = await fetchWithTimeout(url, { timeoutMs: 10_000, signal });
\`\`\`

- 调用方传入的 \`signal\` 仍然有效，任意一方先触发都会中止请求
- 超时抛出 \`TimeoutError\`，界面据此显示「请求超时，请重试」
- 所有调用点已迁移，\`api.ts\` 里不再有裸 \`fetch\``;

export function seedDemoConversations(): void {
  const seeds: DemoConversation[] = [
    conversation("conv_demo_release", "撰写 0.4.2 发布说明", 42, [
      user("u_seed_1", "根据最近的提交写一份 0.4.2 的发布说明", 44),
      assistant("a_seed_1", RELEASE_ANSWER, 42, [
        text("我先看一下上个版本以来的提交记录。", "commentary", "seed:1"),
        tool("seed_call_1", "run_command", { command: "git log v0.4.1..HEAD --oneline" },
          "Exit code: 0\n\n9f2c1e1 feat(settings): appearance group\n4b7aa02 fix(counter): clamp negative steps\n1d03be5 perf: split vendor chunk\nc81f9d4 fix(ws): do not resend unacked input"),
        text(RELEASE_ANSWER, "model_final", "seed:2"),
      ]),
    ], true),
    conversation("conv_demo_api", "重构 API 客户端的超时处理", 180, [
      user("u_seed_2", "把 api.ts 里所有请求的超时处理统一一下", 186),
      assistant("a_seed_2", API_ANSWER, 180, [
        text("先找出所有直接调用 fetch 的位置。", "commentary", "seed:3"),
        tool("seed_call_2", "grep_files", { pattern: "fetch\\(", path: "src/lib" }, "src/lib/api.ts\nsrc/lib/upload.ts\nsrc/lib/telemetry.ts"),
        tool("seed_call_3", "read_file", { file_path: "src/lib/api.ts" }, "     1→export async function getJson(url: string) {\n     2→  const response = await fetch(url);\n     3→  return response.json();\n     4→}\n\n[content_hash: seed]"),
        tool("seed_call_4", "edit_file", { file_path: "src/lib/api.ts" }, "Edited src/lib/api.ts. Diff stats: +6 -2.", {
          diff: { format: "structured", stats: { files_count: 1, additions: 6, deletions: 2 }, files: [{
            path: `${DEMO_WORKSPACE}/src/lib/api.ts`, status: "modified", additions: 6, deletions: 2,
            patch: "--- a/src/lib/api.ts\n+++ b/src/lib/api.ts\n@@ -1,4 +1,8 @@\n-export async function getJson(url: string) {\n-  const response = await fetch(url);\n+import { fetchWithTimeout } from \"./timeout\";\n+\n+export async function getJson(url: string, signal?: AbortSignal) {\n+  const response = await fetchWithTimeout(url, { timeoutMs: 10_000, signal });\n+  if (!response.ok) throw new Error(response.statusText);\n   return response.json();\n }\n",
          }] },
        }),
        text(API_ANSWER, "model_final", "seed:4"),
      ]),
    ]),
    conversation("conv_demo_tokens", "梳理设计 token 命名", 60 * 26, [
      user("u_seed_3", "我们的颜色 token 命名有点乱，给个统一的方案", 60 * 26 + 5),
      assistant("a_seed_3", "建议按「用途 → 层级 → 状态」命名：`--surface-base`、`--surface-raised`、`--text-primary`、`--text-secondary`、`--accent`、`--accent-hover`。组件只引用语义 token，不直接写色值。", 60 * 26, [
        text("建议按「用途 → 层级 → 状态」命名：`--surface-base`、`--surface-raised`、`--text-primary`、`--text-secondary`、`--accent`、`--accent-hover`。组件只引用语义 token，不直接写色值。", "model_final", "seed:5"),
      ]),
    ]),
    conversation("conv_demo_onboarding", "首次启动引导的文案", 60 * 24 * 3, [
      user("u_seed_4", "首次启动的欢迎页文案帮我润色一下，语气友好但别太长", 60 * 24 * 3 + 4),
      assistant("a_seed_4", "**欢迎使用 Aurora**\n\n打开一个文件夹即可开始。你的文件始终保存在本地。", 60 * 24 * 3, [
        text("**欢迎使用 Aurora**\n\n打开一个文件夹即可开始。你的文件始终保存在本地。", "model_final", "seed:6"),
      ]),
    ]),
  ];
  for (const seed of seeds) state.conversations.set(seed.id, seed);
}
