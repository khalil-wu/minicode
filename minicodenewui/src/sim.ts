/*
 * 本地模拟的 agent：按提示词挑一个剧本，逐步推进思考、工具调用、审批、提问和流式回复。
 * 所有时间都除以 store.speed，冒烟测试可用 ?speed=20 加速。
 */
import { useApp } from "./store";
import { diffApp, diffComposer, diffStream, diffTest } from "./mock";
import type { ApprovalDecision, AssistantTurn, FileChange, Step } from "./types";
import { uid } from "./lib/util";

interface Runtime {
  cancelled: boolean;
  turnId: string;
}

const runtimes = new Map<string, Runtime>();
const pending = new Map<string, (value: string) => void>();
const CANCELLED = Symbol("cancelled");

export function cancelTurn(chatId: string) {
  const rt = runtimes.get(chatId);
  if (!rt) return;
  rt.cancelled = true;
  pending.get(rt.turnId)?.("cancel");
  pending.delete(rt.turnId);
  runtimes.delete(chatId);
}

export function resolvePending(turnId: string, value: ApprovalDecision | string) {
  const fn = pending.get(turnId);
  pending.delete(turnId);
  fn?.(value);
}

type Scenario = "chat" | "plan" | "question" | "failing" | "code";

function pickScenario(prompt: string, hasProject: boolean, planMode: boolean): Scenario {
  if (planMode) return "plan";
  if (/方案|选哪|哪种|选择|which/i.test(prompt)) return "question";
  if (/失败|报错|修复|bug|fix|error/i.test(prompt)) return "failing";
  if (!hasProject || /^(你好|hi|hello)|解释|是什么|为什么|怎么理解|区别/i.test(prompt)) return "chat";
  return "code";
}

export function startTurn(chatId: string, turnId: string, prompt: string) {
  cancelTurn(chatId);
  const rt: Runtime = { cancelled: false, turnId };
  runtimes.set(chatId, rt);
  run(chatId, turnId, prompt, rt).catch((err) => {
    if (err !== CANCELLED) console.error(err);
  });
}

async function run(chatId: string, turnId: string, prompt: string, rt: Runtime) {
  const store = useApp.getState;
  const chat = store().chats.find((c) => c.id === chatId);
  const scenario = pickScenario(prompt, !!chat?.projectId, store().planMode);

  const wait = async (ms: number) => {
    await new Promise((r) => setTimeout(r, ms / store().speed));
    if (rt.cancelled) throw CANCELLED;
  };
  const patch = (fn: (t: AssistantTurn) => AssistantTurn) => {
    if (!rt.cancelled) store().patchTurn(chatId, turnId, fn);
  };
  const ask = (): Promise<string> =>
    new Promise((resolve) => {
      pending.set(turnId, resolve);
    }).then((v) => {
      if (rt.cancelled || v === "cancel") throw CANCELLED;
      return v as string;
    });

  const step = async (s: Omit<Step, "id" | "status">, ms: number, result?: Partial<Step>) => {
    const id = uid("s");
    patch((t) => ({ ...t, phase: s.kind === "think" ? "thinking" : "working", steps: [...t.steps, { ...s, id, status: "running" }] }));
    await wait(ms);
    patch((t) => ({ ...t, steps: t.steps.map((x) => (x.id === id ? { ...x, status: "done", ...result } : x)) }));
  };

  /** 运行命令前按权限模式决定是否需要审批 */
  const gate = async (kind: "command" | "write", command: string, reason: string) => {
    const { permission, alwaysAllow } = store();
    const needs = kind === "command" ? permission !== "full" : permission === "readonly";
    if (!needs || (alwaysAllow[chatId] ?? []).includes(command)) return true;
    patch((t) => ({ ...t, phase: "waiting", approval: { id: uid("ap"), kind, command, reason } }));
    const decision = await ask();
    patch((t) => ({ ...t, phase: "working", approval: undefined }));
    return decision !== "deny";
  };

  const stream = async (text: string) => {
    patch((t) => ({ ...t, phase: "writing" }));
    let i = 0;
    while (i < text.length) {
      const n = 2 + Math.floor(Math.random() * 4);
      const chunk = text.slice(i, i + n);
      i += n;
      patch((t) => ({ ...t, text: t.text + chunk }));
      await wait(18);
    }
  };

  const short = prompt.replace(/\s+/g, " ").slice(0, 28);

  await step({ kind: "think", target: "" }, 900, { target: `${2 + Math.floor(Math.random() * 6)} 秒` });

  if (scenario === "chat") {
    await stream(
      `关于「${short}」，简单说三点：\n\n1. **先明确目标**：要解决的到底是速度、正确性还是可维护性；\n2. **找最小改动**：优先复用已有模块，而不是另起炉灶；\n3. **用证据收尾**：每个结论都配一条能复现的命令或测试。\n\n如果你愿意，可以把相关文件拖进来，我直接在项目里动手。`,
    );
    store().finishTurn(chatId, turnId);
    runtimes.delete(chatId);
    return;
  }

  await step({ kind: "search", target: short.slice(0, 14) || "项目文件" }, 700, { detail: "6 处结果 · src/App.tsx, src/store.ts, README.md" });
  await step({ kind: "read", target: "App.tsx", detail: "frontend/src/App.tsx · 1–86 行" }, 600);

  if (scenario === "plan") {
    await step({ kind: "read", target: "store.ts", detail: "frontend/src/store.ts · 1–240 行" }, 600);
    await stream("我看过了相关代码，下面是实施计划，确认后我再动手。");
    patch((t) => ({
      ...t,
      plan: {
        state: "proposed",
        markdown: `### 目标\n${short}\n\n### 步骤\n1. 在 \`store.ts\` 增加 \`status\` 状态，区分加载中与失败\n2. \`App.tsx\` 中 \`load()\` 失败时展示重试入口\n3. 补两条单元测试：加载成功、加载失败\n4. 运行 \`npm test\` 与 \`npm run build\` 验证\n\n### 不会改动\n- 公共接口与路由\n- 样式文件`,
      },
    }));
    store().finishTurn(chatId, turnId);
    runtimes.delete(chatId);
    return;
  }

  let choice = "";
  if (scenario === "question") {
    patch((t) => ({
      ...t,
      phase: "waiting",
      question: {
        id: uid("q"),
        prompt: "有两种实现方式，你更倾向哪一种？",
        options: [
          { id: "a", label: "最小改动", desc: "只改 App.tsx，10 行以内，不动状态结构" },
          { id: "b", label: "抽出状态", desc: "把加载状态挪进 store，后续页面可复用" },
          { id: "c", label: "你来决定", desc: "按项目现有风格选择" },
        ],
      },
    }));
    choice = await ask();
    patch((t) => ({ ...t, phase: "working" }));
  }

  const files: FileChange[] =
    /输入|composer|发送/i.test(prompt)
      ? [{ path: "frontend/src/composer/Composer.tsx", add: 6, del: 2, diff: diffComposer }]
      : /流式|stream/i.test(prompt)
        ? [
            { path: "backend/llm/anthropic_protocol.py", add: 5, del: 2, diff: diffStream },
            { path: "backend/tests/test_stream_unphased.py", add: 14, del: 0, diff: diffTest },
          ]
        : [{ path: "frontend/src/App.tsx", add: 7, del: 2, diff: diffApp }];

  if (scenario === "failing") {
    const ok = await gate("command", "npm test", "先运行测试，确认问题能复现");
    if (ok) {
      await step({ kind: "run", target: "npm test" }, 1100, {
        status: "failed",
        output: "✗ App > shows error state when load fails\n  Expected: \"加载失败\"\n  Received: null\n\nTests  1 failed | 11 passed (12)",
      });
    }
  }

  for (const f of files) {
    const allowed = await gate("write", `编辑 ${f.path}`, "只读模式下修改文件需要你的同意");
    if (!allowed) continue;
    await step({ kind: "edit", target: f.path.split("/").pop()!, path: f.path, add: f.add, del: f.del }, 700);
  }

  const verify = scenario === "failing" ? "npm test" : "npm run build";
  const ran = await gate("command", verify, scenario === "failing" ? "重新运行测试，确认已修复" : "构建一次，确认没有类型错误");
  if (ran) {
    await step({ kind: "run", target: verify }, 1200, {
      output: scenario === "failing" ? "✓ App (12)\n\nTests  12 passed (12)\n  Duration  2.04s" : "vite v8.3.6 building for production...\n✓ 214 modules transformed.\n✓ built in 2.41s",
    });
  }

  patch((t) => ({ ...t, files }));

  const picked =
    choice === "a" ? "按你选的**最小改动**方案，" : choice === "b" ? "按你选的**抽出状态**方案，" : choice ? "我按项目现有风格选了**最小改动**，" : "";
  const verified = ran ? (scenario === "failing" ? "测试 12 项全部通过。" : "构建通过，没有新增警告。") : "你拒绝了验证命令，所以这次**没有运行验证**，建议稍后手动跑一次。";
  await stream(
    `${picked}已处理「${short}」。\n\n改动：\n\n${files.map((f) => `- \`${f.path}\`（+${f.add} −${f.del}）`).join("\n")}\n\n${scenario === "failing" ? "根因是 `load()` 失败时没有设置错误状态，组件拿到的是 `null`。\n\n" : ""}${verified}`,
  );
  store().finishTurn(chatId, turnId);
  runtimes.delete(chatId);
}
