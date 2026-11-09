import type { Chat, Effort, FileChange, PermissionMode, Project } from "./types";

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
const now = Date.now();

export const projects: Project[] = [
  { id: "p-minicode", name: "MiniCode", path: "C:\\Desktop\\MiniCode", branch: "desktop-updates", expanded: true },
  { id: "p-research", name: "冠心病血瘀证研究", path: "D:\\research\\xueyu", branch: "main", expanded: false },
];

export const diffComposer = `@@ -12,9 +12,13 @@ export function Composer({ onSend }: Props) {
   const [value, setValue] = useState("");
   const ref = useRef<HTMLTextAreaElement>(null);

-  const send = () => onSend(value);
+  const send = () => {
+    const text = value.trim();
+    if (!text) return;
+    onSend(text);
+    setValue("");
+  };

   return (
     <div className="composer">
-      <textarea ref={ref} value={value} onChange={(e) => setValue(e.target.value)} />
+      <textarea ref={ref} value={value} rows={1} onChange={(e) => setValue(e.target.value)} />`;

export const diffComposerCss = `@@ -1,6 +1,9 @@
 .composer {
-  border: 1px solid #ddd;
-  border-radius: 6px;
+  border: 1px solid var(--line);
+  border-radius: 20px;
+  box-shadow: var(--shadow-sm);
+  transition: box-shadow 0.2s ease;
   padding: 12px 14px;
 }
+.composer:focus-within { box-shadow: var(--shadow-md); }`;

export const diffStream = `@@ -88,11 +88,14 @@ class AnthropicStream:
     async def on_event(self, event: dict) -> None:
         kind = event.get("type")
         if kind == "content_block_delta":
             delta = event["delta"]
-            if delta.get("type") == "text_delta" and self._phase == "final":
-                self._emit(delta["text"])
+            if delta.get("type") == "text_delta":
+                # 未分相的文本也要立即投影，否则整轮没有打字机效果
+                self._emit(delta["text"], phase=self._phase or "pending")
+            elif delta.get("type") == "input_json_delta":
+                self._tool_args[event["index"]] += delta["partial_json"]
         elif kind == "message_stop":
             self._flush()`;

export const diffTest = `@@ -0,0 +1,14 @@
+import pytest
+
+from backend.llm.anthropic_protocol import AnthropicStream
+
+
+@pytest.mark.asyncio
+async def test_unphased_text_streams_immediately():
+    seen: list[str] = []
+    stream = AnthropicStream(on_text=seen.append)
+    await stream.on_event({"type": "content_block_delta", "index": 0,
+                           "delta": {"type": "text_delta", "text": "你好"}})
+    assert seen == ["你好"]
+    await stream.on_event({"type": "message_stop"})
+    assert stream.closed`;

export const diffApp = `@@ -4,10 +4,14 @@ import { Sidebar } from "./shell/Sidebar";
 import { ChatView } from "./chat/ChatView";

 export function App() {
-  const [chat, setChat] = useState(null);
+  const [chat, setChat] = useState<Chat | null>(null);
+  const [status, setStatus] = useState<"idle" | "loading" | "error">("idle");

   useEffect(() => {
-    load().then(setChat);
+    setStatus("loading");
+    load()
+      .then((c) => { setChat(c); setStatus("idle"); })
+      .catch(() => setStatus("error"));
   }, []);

   return <Layout sidebar={<Sidebar />} main={<ChatView chat={chat} />} />;`;

export const firstChatFiles: FileChange[] = [
  { path: "frontend/src/composer/Composer.tsx", add: 6, del: 2, diff: diffComposer },
  { path: "frontend/src/composer/composer.css", add: 5, del: 2, diff: diffComposerCss },
];

export const initialChats: Chat[] = [
  {
    id: "c-composer",
    title: "优化输入框发送逻辑",
    projectId: "p-minicode",
    updatedAt: now - 2 * MIN,
    pinned: true,
    queue: [],
    messages: [
      {
        id: "u1",
        role: "user",
        at: now - 4 * MIN,
        text: "输入框按回车会把空消息也发出去，帮我修一下，顺便让样式更柔和一点。",
      },
      {
        id: "a1",
        role: "assistant",
        status: "done",
        phase: "idle",
        startedAt: now - 4 * MIN,
        seconds: 48,
        model: "Claude Opus 5",
        steps: [
          { id: "s1", kind: "think", target: "6 秒", status: "done" },
          { id: "s2", kind: "search", target: "onSend", detail: "3 处结果 · Composer.tsx, ChatPane.tsx, store.ts", status: "done" },
          { id: "s3", kind: "read", target: "Composer.tsx", detail: "frontend/src/composer/Composer.tsx · 1–120 行", status: "done" },
          { id: "s4", kind: "read", target: "composer.css", detail: "frontend/src/composer/composer.css · 1–40 行", status: "done" },
          { id: "s5", kind: "edit", target: "Composer.tsx", add: 6, del: 2, path: "frontend/src/composer/Composer.tsx", status: "done" },
          { id: "s6", kind: "edit", target: "composer.css", add: 5, del: 2, path: "frontend/src/composer/composer.css", status: "done" },
          { id: "s7", kind: "run", target: "npm test -- composer", output: "✓ composer/send.test.tsx (12)\n\nTest Files  1 passed (1)\n     Tests  12 passed (12)\n  Duration  1.82s", status: "done" },
        ],
        text:
          "已修好。现在**空白消息不会再被发送**，发送成功后输入框会自动清空。\n\n改动要点：\n\n1. `send()` 先 `trim()`，为空直接返回；\n2. 发送后重置草稿；\n3. 输入框换成 `20px` 圆角和轻阴影，聚焦时阴影加深，边框改用主题变量。\n\n```tsx\nconst send = () => {\n  const text = value.trim();\n  if (!text) return;\n  onSend(text);\n  setValue(\"\");\n};\n```\n\n相关测试 12 项全部通过。",
        files: firstChatFiles,
      },
    ],
  },
  {
    id: "c-stream",
    title: "审计后端流式输出链路",
    projectId: "p-minicode",
    updatedAt: now - 1 * HOUR,
    queue: [],
    messages: [
      { id: "u1", role: "user", at: now - HOUR, text: "从 provider 到前端逐层核对流式输出，有没有吞掉增量的地方？" },
      {
        id: "a1",
        role: "assistant",
        status: "done",
        phase: "idle",
        startedAt: now - HOUR,
        seconds: 214,
        model: "Claude Opus 5",
        steps: [
          { id: "s1", kind: "think", target: "14 秒", status: "done" },
          { id: "s2", kind: "read", target: "anthropic_protocol.py", status: "done" },
          { id: "s3", kind: "search", target: "text_delta", detail: "9 处结果", status: "done" },
          { id: "s4", kind: "read", target: "provider_text_projection.py", status: "done" },
          { id: "s5", kind: "edit", target: "anthropic_protocol.py", add: 5, del: 2, path: "backend/llm/anthropic_protocol.py", status: "done" },
          { id: "s6", kind: "edit", target: "test_stream_unphased.py", add: 14, del: 0, path: "backend/tests/test_stream_unphased.py", status: "done" },
          { id: "s7", kind: "run", target: "pytest backend/tests -k stream -q", output: "........                                                     [100%]\n8 passed in 0.94s", status: "done" },
        ],
        text:
          "找到了一处：`AnthropicStream.on_event` 只在 `phase == \"final\"` 时转发 `text_delta`，**未分相的文本会被攒到结束才一次性出现**，所以 Anthropic 与 Chat Completions 整轮没有打字机效果。\n\n| 层 | 结论 |\n| --- | --- |\n| provider 协议层 | 已修复，未分相文本按 `pending` 立即投影 |\n| 事件投影 | 正常 |\n| WebSocket 转发 | 正常 |\n| 前端渲染 | 正常 |\n\n已补一条回归测试，8 项流式测试全部通过。",
        files: [
          { path: "backend/llm/anthropic_protocol.py", add: 5, del: 2, diff: diffStream },
          { path: "backend/tests/test_stream_unphased.py", add: 14, del: 0, diff: diffTest },
        ],
      },
    ],
  },
  {
    id: "c-markdown",
    title: "修复 Markdown 图标渲染",
    projectId: "p-minicode",
    updatedAt: now - 20 * HOUR,
    unread: true,
    queue: [],
    messages: [
      { id: "u1", role: "user", at: now - 20 * HOUR, text: "列表里的代码块会被反复高亮，滚动时很卡。" },
      {
        id: "a1",
        role: "assistant",
        status: "done",
        phase: "idle",
        startedAt: now - 20 * HOUR,
        seconds: 95,
        model: "Claude Sonnet 5",
        steps: [
          { id: "s1", kind: "read", target: "AssistantMarkdownCell.tsx", status: "done" },
          { id: "s2", kind: "search", target: "useHighlighted", detail: "4 处结果", status: "done" },
        ],
        text: "原因是列表项每次渲染都会生成新的 `key`，导致高亮组件被卸载重建。改成以代码内容哈希作 key 后，同一代码块只高亮一次。",
      },
    ],
  },
  {
    id: "c-data",
    title: "整理数据集预处理脚本",
    projectId: "p-research",
    updatedAt: now - 2 * DAY,
    queue: [],
    messages: [
      { id: "u1", role: "user", at: now - 2 * DAY, text: "把三个中心的数据合并，缺失值用中位数填补。" },
      {
        id: "a1",
        role: "assistant",
        status: "done",
        phase: "idle",
        startedAt: now - 2 * DAY,
        seconds: 63,
        model: "Claude Sonnet 5",
        steps: [{ id: "s1", kind: "read", target: "preprocess.py", status: "done" }],
        text: "已合并 3 个中心共 1,284 例样本，数值列缺失值按中心分组后用中位数填补，分类列保留为 `未知`。",
      },
    ],
  },
  {
    id: "c-rag",
    title: "评估知识图谱与 RAG",
    projectId: null,
    updatedAt: now - 3 * DAY,
    queue: [],
    messages: [
      { id: "u1", role: "user", at: now - 3 * DAY, text: "知识图谱和 RAG 各自适合什么场景？" },
      {
        id: "a1",
        role: "assistant",
        status: "done",
        phase: "idle",
        startedAt: now - 3 * DAY,
        seconds: 12,
        model: "Claude Opus 5",
        steps: [],
        text: "简单说：**知识图谱**擅长结构稳定、需要多跳推理和可解释路径的问题；**RAG** 擅长非结构化文档、更新频繁、问题开放的场景。两者可以组合——用图谱做实体链接和约束，用 RAG 补充上下文。",
      },
    ],
  },
  {
    id: "c-mail",
    title: "写一封英文邮件",
    projectId: null,
    updatedAt: now - 8 * DAY,
    queue: [],
    messages: [
      { id: "u1", role: "user", at: now - 8 * DAY, text: "帮我写一封英文邮件，向导师请假三天。" },
      {
        id: "a1",
        role: "assistant",
        status: "done",
        phase: "idle",
        startedAt: now - 8 * DAY,
        seconds: 6,
        model: "Claude Haiku 4.5",
        steps: [],
        text: "**Subject:** Leave request (Nov 12–14)\n\nDear Professor Li,\n\nI would like to request leave from November 12 to 14 for a family matter. I will keep up with my reading and send the weekly update as usual.\n\nBest regards,\nJiawei",
      },
    ],
  },
  {
    id: "c-old",
    title: "旧版设置页迁移方案",
    projectId: "p-minicode",
    updatedAt: now - 30 * DAY,
    archived: true,
    queue: [],
    messages: [{ id: "u1", role: "user", at: now - 30 * DAY, text: "旧版设置页怎么迁到新架构？" }],
  },
];

export interface ModelInfo {
  id: string;
  name: string;
  short: string;
  note: string;
  context: number;
}

export const models: ModelInfo[] = [
  { id: "opus", name: "Claude Opus 5", short: "Opus 5", note: "最强推理，适合复杂工程", context: 1_000_000 },
  { id: "sonnet", name: "Claude Sonnet 5", short: "Sonnet 5", note: "均衡，日常首选", context: 400_000 },
  { id: "haiku", name: "Claude Haiku 4.5", short: "Haiku 4.5", note: "最快，适合小改动", context: 200_000 },
  { id: "deepseek", name: "DeepSeek V4", short: "DeepSeek V4", note: "自定义 OpenAI 兼容接口", context: 128_000 },
];

export const efforts: { id: Effort; label: string; note: string }[] = [
  { id: "low", label: "低", note: "最快，适合简单问题" },
  { id: "medium", label: "中", note: "速度与深度兼顾" },
  { id: "high", label: "高", note: "更仔细地规划与核对" },
  { id: "xhigh", label: "极高", note: "最深入，耗时更长" },
];

export const permissionModes: { id: PermissionMode; label: string; note: string }[] = [
  { id: "readonly", label: "只读", note: "可以读取文件，编辑和运行命令前都会询问" },
  { id: "ask", label: "请求批准", note: "可以编辑工作区文件，运行命令和联网前询问" },
  { id: "full", label: "完全访问", note: "不经询问即可编辑任意文件、运行命令和联网" },
];

export interface TreeNode {
  name: string;
  path: string;
  dir?: boolean;
  children?: TreeNode[];
}

export const fileTree: TreeNode[] = [
  {
    name: "backend",
    path: "backend",
    dir: true,
    children: [
      { name: "llm", path: "backend/llm", dir: true, children: [{ name: "anthropic_protocol.py", path: "backend/llm/anthropic_protocol.py" }] },
      { name: "tests", path: "backend/tests", dir: true, children: [{ name: "test_stream_unphased.py", path: "backend/tests/test_stream_unphased.py" }] },
      { name: "main.py", path: "backend/main.py" },
    ],
  },
  {
    name: "frontend",
    path: "frontend",
    dir: true,
    children: [
      {
        name: "src",
        path: "frontend/src",
        dir: true,
        children: [
          {
            name: "composer",
            path: "frontend/src/composer",
            dir: true,
            children: [
              { name: "Composer.tsx", path: "frontend/src/composer/Composer.tsx" },
              { name: "composer.css", path: "frontend/src/composer/composer.css" },
            ],
          },
          { name: "App.tsx", path: "frontend/src/App.tsx" },
          { name: "main.tsx", path: "frontend/src/main.tsx" },
        ],
      },
      { name: "package.json", path: "frontend/package.json" },
    ],
  },
  { name: "AGENTS.md", path: "AGENTS.md" },
  { name: "README.md", path: "README.md" },
];

export const fileContents: Record<string, string> = {
  "frontend/src/composer/Composer.tsx": `import { useRef, useState } from "react";

interface Props {
  onSend: (text: string) => void;
}

export function Composer({ onSend }: Props) {
  const [value, setValue] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  const send = () => {
    const text = value.trim();
    if (!text) return;
    onSend(text);
    setValue("");
  };

  return (
    <div className="composer">
      <textarea ref={ref} value={value} rows={1} onChange={(e) => setValue(e.target.value)} />
      <button onClick={send}>发送</button>
    </div>
  );
}
`,
  "README.md": `# MiniCode

本地优先的 AI 编程桌面应用。

- 后端：Python（FastAPI + WebSocket）
- 前端：React + TypeScript + Vite
- 桌面：Electron

## 开发

\`\`\`bash
npm --prefix frontend run dev
python -m backend.main
\`\`\`
`,
  "AGENTS.md": `# MiniCode 项目协作约定

- 先读调用链再改代码。
- 结论必须实测。
`,
  "backend/main.py": `from fastapi import FastAPI

app = FastAPI(title="MiniCode")


@app.get("/api/status")
async def status() -> dict:
    return {"ok": True}
`,
};

export const plugins = [
  { id: "github", name: "GitHub", desc: "读取 issue 与 PR，创建评审评论", icon: "GH", tint: "#24292f", installed: true },
  { id: "figma", name: "Figma", desc: "把设计稿转成组件，核对间距与颜色", icon: "Fi", tint: "#a259ff", installed: false },
  { id: "linear", name: "Linear", desc: "同步任务状态，自动关联提交", icon: "Li", tint: "#5e6ad2", installed: false },
  { id: "sentry", name: "Sentry", desc: "拉取报错堆栈，直接定位到代码行", icon: "Se", tint: "#362d59", installed: true },
  { id: "notion", name: "Notion", desc: "把设计文档与会议纪要作为上下文", icon: "No", tint: "#2f2f2f", installed: false },
  { id: "postgres", name: "PostgreSQL", desc: "只读查询数据库结构与样本数据", icon: "Pg", tint: "#336791", installed: false },
  { id: "playwright", name: "Playwright", desc: "打开网页、截图、跑端到端测试", icon: "Pw", tint: "#2ead33", installed: true },
  { id: "slack", name: "Slack", desc: "完成任务后在频道里同步结果", icon: "Sl", tint: "#4a154b", installed: false },
];

export const automations = [
  { id: "a1", name: "每日依赖安全巡检", schedule: "每天 09:00", project: "MiniCode", enabled: true, last: "今天 09:00 · 无新漏洞" },
  { id: "a2", name: "周报：本周提交摘要", schedule: "每周五 17:30", project: "MiniCode", enabled: true, last: "上周五 · 已生成" },
  { id: "a3", name: "清理过期分支", schedule: "每月 1 日", project: "MiniCode", enabled: false, last: "从未运行" },
];

export const mcpServers = [
  { id: "fs", name: "filesystem", transport: "stdio", tools: 11, status: "connected" as const, enabled: true },
  { id: "browser", name: "playwright", transport: "stdio", tools: 23, status: "connected" as const, enabled: true },
  { id: "docs", name: "context7", transport: "http", tools: 2, status: "error" as const, enabled: true },
  { id: "db", name: "postgres", transport: "stdio", tools: 4, status: "disabled" as const, enabled: false },
];

export const suggestions = [
  { title: "解释这个项目的架构", prompt: "解释这个项目的整体架构，从入口到主要模块。" },
  { title: "找出并修复一个 bug", prompt: "运行测试，找出失败的用例并修复。" },
  { title: "为改动补测试", prompt: "为最近的改动补上单元测试。" },
  { title: "写一份发布说明", prompt: "根据最近的提交写一份中文发布说明。" },
];
