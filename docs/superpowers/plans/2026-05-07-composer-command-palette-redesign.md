# Composer Command Palette Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild MiniCode composer interactions so `@` is only for files/folders/context references, while `/` is a polished command palette for settings, local Skills selection, MCP status, modes, and task actions without dumping prompt templates into the visible input.

**Architecture:** Split composer behavior into three explicit layers: query detection, selectable palette items, and action execution. Slash items become typed actions (`execute`, `open-panel`, `draft-chip`, `submit-command`) instead of text templates; mention items become file/folder-only references. User-visible UI shows concise command rows, chips, and small inline panels, while backend instructions are generated only at send time when needed.

**Tech Stack:** React, TypeScript, Zustand chat store, Vitest source/regression tests, existing WebSocket client messages, existing Skills/MCP/settings views.

---

## File Structure

- Create `frontend/src/lib/composer-action-model.ts`
  - Owns typed slash action definitions, groups, command search text, and action execution metadata.
  - Replaces the current "every slash command must have a template" assumption.

- Modify `frontend/src/lib/composer-commands.ts`
  - Keep query detection helpers.
  - Convert `ComposerSlashCommandOption` to support optional `template` plus required `action`.
  - Remove `replaceSlashCommandQuery` as the default path for action commands; keep a smaller helper only for explicit draft scaffolds.

- Modify `frontend/src/lib/composer-mentions.ts`
  - Make `MentionKind` file/folder-only.
  - Keep token format stable for `@file:` and `@folder:`.
  - Stop allowing `@skill:` and `@mcp:` because `/` owns Skills/MCP now.

- Create `frontend/src/components/ComposerCommandPalette.tsx`
  - Renders Claude Code-style grouped `/` menu.
  - Shows icons, title, concise description, optional current value, and source tags.
  - Handles keyboard selection without changing the input text unless the selected command explicitly opens a draft chip/panel.

- Create `frontend/src/components/ComposerMentionPalette.tsx`
  - Renders `@` menu for workspace files/folders only.
  - Uses compact rows with file/folder icons and path truncation.

- Create `frontend/src/components/ComposerActionPanel.tsx`
  - Renders inline panels above the composer for commands that need options: model, reasoning, permissions, plan, memory, MCP status.
  - Panels use buttons and segmented controls, not raw command syntax.

- Modify `frontend/src/components/ChatInputBar.tsx`
  - Replace inline menu markup/styles with `ComposerCommandPalette`, `ComposerMentionPalette`, and `ComposerActionPanel`.
  - Add props for selected composer action chips and panel callbacks.

- Modify `frontend/src/components/ChatPanel.tsx`
  - Remove skills/mcp from `mentionOptions`.
  - Route slash action selection to settings, inline Skills selector, MCP status panel, permission panel, model page, task manager, or composer action chips.
  - Keep the extensions marketplace as a separate toolbar/sidebar store button; `/skills` must not open the store.
  - Generate hidden instruction text only during send for action chips like review/debug/plan.

- Modify `frontend/styles/components.css`
  - Add palette, row, group label, chip, panel, scrollbar, and focus styles.
  - Remove remaining large inline palette styles from `ChatInputBar.tsx`.

- Modify `frontend/src/index.css`, `frontend/style-v2.css`, `frontend/styles/components.css`, `frontend/styles/layout.css`, and `frontend/styles/effects-and-overrides.css`
  - Align the dark visual system with Claude Code desktop: softer near-black surfaces, restrained violet/indigo accent, low-glare borders, and readable muted text.
  - Remove warm-orange dominance from default UI chrome; keep warm tones only for warning/attention states.
  - Fix assistant output typography so body text is readable and `strong` text is not pure white or overly bold.

- Modify `frontend/src/components/TerminalPanel.tsx`
  - Redesign the terminal as a compact desktop workbench panel with sessions, cwd/branch context, shell controls, and clear running/idle feedback.
  - Replace decorative empty states with functional guidance and immediate actions.

- Modify tests:
  - `frontend/src/lib/composer-commands.test.ts`
  - `frontend/src/lib/runtime-commands.test.ts`
  - `frontend/src/lib/attachment-ui-regression.test.ts`
  - `frontend/src/lib/chat-panel-architecture.test.ts`
  - Add or update terminal/output visual architecture tests.
  - Add `frontend/src/lib/composer-action-model.test.ts`
  - Add `frontend/src/lib/composer-mentions.test.ts`

---

## Design Corrections To Preserve During Implementation

- `/skills` is a per-turn Skill selector near the composer. It must never open the marketplace and must never paste the Skill prompt into the visible input.
- `/mcp` is a compact status/availability panel. Installing or managing external connections belongs to the independent store button.
- `@` is only for files, folders, and user-visible context references. It must not show Skills, MCP servers, providers, or settings pages.
- `/permissions` uses inline buttons with plain labels like `日常`, `先计划`, `每步确认`, `少打断`; it must not expose raw command syntax.
- `/plan`, `/review`, `/debug`, `/test`, and similar workflow actions create chips or small panels. They generate hidden instruction text only when sending.
- `/status` shows a small readable state card with connection, task, and token count. It must not dump JSON, RAG details, backend traces, or other internal noise into chat.
- The store is for discovering/installing/managing extensions only. It should have one obvious toolbar/sidebar entry, not multiple hidden command routes.
- Provider/model selection belongs to the settings/model page or a compact model picker. Do not duplicate a free-text model input and a selectable model list for the same decision.
- Assistant output should look like serious reading material: comfortable line height, controlled measure, muted off-white body text, and restrained heading weight. Pure white bold output is a readability bug.
- Terminal is a real work surface, not a debug afterthought. It needs session tabs, clear command controls, copy/clear actions, cwd/branch context, and idle/running states.

### Task 1: Introduce Typed Slash Action Model

**Files:**
- Create: `frontend/src/lib/composer-action-model.ts`
- Test: `frontend/src/lib/composer-action-model.test.ts`
- Modify: `frontend/src/lib/composer-commands.ts`
- Modify: `frontend/src/lib/composer-commands.test.ts`

- [ ] **Step 1: Write failing tests for action metadata**

Create `frontend/src/lib/composer-action-model.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import {
  COMPOSER_ACTIONS,
  getActionSlashCommand,
  getSlashActionsByGroup,
  isVisiblePromptTemplateAction,
} from "./composer-action-model";

describe("composer action model", () => {
  it("treats slash as an action palette instead of a prompt-template list", () => {
    const skills = getActionSlashCommand("skills");
    const mcp = getActionSlashCommand("mcp");
    const model = getActionSlashCommand("model");
    const review = getActionSlashCommand("review");

    expect(skills).toEqual(expect.objectContaining({
      command: "skills",
      label: "/skills",
      group: "skills",
      action: { kind: "open-inline-panel", panel: "skills" },
    }));
    expect(mcp).toEqual(expect.objectContaining({
      command: "mcp",
      action: { kind: "open-inline-panel", panel: "mcp-status" },
    }));
    expect(model).toEqual(expect.objectContaining({
      command: "model",
      action: { kind: "open-settings-page", page: "model" },
    }));
    expect(review?.action.kind).toBe("draft-chip");
    expect(isVisiblePromptTemplateAction(review!)).toBe(false);
  });

  it("groups commands in a Claude Code-style order", () => {
    const groups = getSlashActionsByGroup(COMPOSER_ACTIONS);

    expect(groups.map((group) => group.id)).toEqual([
      "context",
      "settings",
      "workflows",
      "skills",
      "session",
    ]);
    expect(groups[0].items.map((item) => item.command)).toContain("ide");
    expect(groups[1].items.map((item) => item.command)).toEqual(
      expect.arrayContaining(["model", "reasoning", "permissions", "memory"])
    );
  });

  it("keeps every visible description short and human-readable", () => {
    for (const item of COMPOSER_ACTIONS) {
      expect(item.description.length).toBeLessThanOrEqual(42);
      expect(item.description).not.toMatch(/prompt|template|wire|schema|payload|RAG|MCP server/i);
    }
  });
});
```

Add to `frontend/src/lib/composer-commands.test.ts`:

```ts
import { COMPOSER_ACTIONS } from "./composer-action-model";

it("does not require visible slash actions to carry prompt templates", () => {
  const commands = new Map(COMPOSER_ACTIONS.map((item) => [item.command, item]));

  expect(commands.get("skills")?.template).toBeUndefined();
  expect(commands.get("permissions")?.template).toBeUndefined();
  expect(commands.get("model")?.template).toBeUndefined();
  expect(commands.get("review")?.template).toBeUndefined();
  expect(commands.get("review")?.action.kind).toBe("draft-chip");
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd frontend
npm test -- --run src/lib/composer-action-model.test.ts src/lib/composer-commands.test.ts
```

Expected: FAIL because `composer-action-model.ts` does not exist and `COMPOSER_ACTIONS` is not exported.

- [ ] **Step 3: Implement action model**

Create `frontend/src/lib/composer-action-model.ts`:

```ts
export type ComposerActionGroupId =
  | "context"
  | "settings"
  | "workflows"
  | "skills"
  | "session";

export type ComposerActionKind =
  | { kind: "open-settings-page"; page: "model" | "general" | "permissions" | "mcp" | "skills" }
  | { kind: "open-permission-panel" }
  | { kind: "open-inline-panel"; panel: "reasoning" | "memory" | "plan" | "skills" | "mcp-status" | "ide-context" }
  | { kind: "draft-chip"; chip: "review" | "debug" | "refactor" | "test" | "docs" | "file" | "image" | "commit" }
  | { kind: "send-runtime-command"; command: "status" | "tasks" }
  | { kind: "conversation"; operation: "new" | "clear" | "archive" | "unarchive" };

export interface ComposerActionItem {
  id: string;
  command: string;
  label: string;
  title: string;
  description: string;
  group: ComposerActionGroupId;
  icon: string;
  searchText: string;
  action: ComposerActionKind;
  currentValue?: string;
  template?: never;
}

export interface ComposerActionGroup {
  id: ComposerActionGroupId;
  label: string;
  items: ComposerActionItem[];
}

const GROUP_ORDER: ComposerActionGroupId[] = ["context", "settings", "workflows", "skills", "session"];

export const COMPOSER_ACTIONS: ComposerActionItem[] = [
  {
    id: "action-ide",
    command: "ide",
    label: "/ide",
    title: "IDE 上下文",
    description: "打开或关闭 IDE 上下文",
    group: "context",
    icon: "sparkles",
    searchText: "ide context workspace 当前编辑器 上下文",
    action: { kind: "open-inline-panel", panel: "ide-context" },
  },
  {
    id: "action-mcp",
    command: "mcp",
    label: "/mcp",
    title: "MCP",
    description: "查看外部连接状态",
    group: "context",
    icon: "plug",
    searchText: "mcp 外部连接 工具 状态",
    action: { kind: "open-inline-panel", panel: "mcp-status" },
  },
  {
    id: "action-review",
    command: "review",
    label: "/review",
    title: "代码审查",
    description: "检查风险并给出修改建议",
    group: "workflows",
    icon: "badge-check",
    searchText: "review code audit 代码审查 风险",
    action: { kind: "draft-chip", chip: "review" },
  },
  {
    id: "action-debug",
    command: "debug",
    label: "/debug",
    title: "定位问题",
    description: "复现、定位并修复问题",
    group: "workflows",
    icon: "bug",
    searchText: "debug bug fix reproduce 调试",
    action: { kind: "draft-chip", chip: "debug" },
  },
  {
    id: "action-model",
    command: "model",
    label: "/model",
    title: "模型",
    description: "选择本次使用的模型",
    group: "settings",
    icon: "box",
    searchText: "model provider llm 模型 provider",
    action: { kind: "open-settings-page", page: "model" },
  },
  {
    id: "action-reasoning",
    command: "reasoning",
    label: "/reasoning",
    title: "推理模式",
    description: "调整思考强度",
    group: "settings",
    icon: "brain",
    searchText: "reasoning effort thinking 推理 思考",
    action: { kind: "open-inline-panel", panel: "reasoning" },
  },
  {
    id: "action-permissions",
    command: "permissions",
    label: "/permissions",
    title: "权限",
    description: "选择执行前是否确认",
    group: "settings",
    icon: "shield",
    searchText: "permissions approval confirm bypass 权限 确认",
    action: { kind: "open-permission-panel" },
  },
  {
    id: "action-plan",
    command: "plan",
    label: "/plan",
    title: "计划模式",
    description: "先列方案再动手",
    group: "settings",
    icon: "list-checks",
    searchText: "plan 计划 模式",
    action: { kind: "open-inline-panel", panel: "plan" },
  },
  {
    id: "action-memory",
    command: "memory",
    label: "/memory",
    title: "记忆",
    description: "设置是否记住本轮信息",
    group: "settings",
    icon: "database",
    searchText: "memory 记忆 summary profile",
    action: { kind: "open-inline-panel", panel: "memory" },
  },
  {
    id: "action-skills",
    command: "skills",
    label: "/skills",
    title: "技能",
    description: "选择本轮要用的技能",
    group: "skills",
    icon: "package",
    searchText: "skills ability 能力 技能 本轮使用",
    action: { kind: "open-inline-panel", panel: "skills" },
  },
  {
    id: "action-new",
    command: "new",
    label: "/new",
    title: "新对话",
    description: "开始一轮干净任务",
    group: "session",
    icon: "plus",
    searchText: "new chat conversation 新对话",
    action: { kind: "conversation", operation: "new" },
  },
  {
    id: "action-status",
    command: "status",
    label: "/status",
    title: "状态",
    description: "查看会话和用量",
    group: "session",
    icon: "activity",
    searchText: "status usage token 状态 用量",
    action: { kind: "send-runtime-command", command: "status" },
  },
  {
    id: "action-tasks",
    command: "tasks",
    label: "/tasks",
    title: "任务",
    description: "查看正在执行的任务",
    group: "session",
    icon: "terminal",
    searchText: "tasks running jobs 任务",
    action: { kind: "send-runtime-command", command: "tasks" },
  },
];

export function getActionSlashCommand(command: string): ComposerActionItem | null {
  const normalized = command.trim().replace(/^\/+/, "").toLowerCase();
  return COMPOSER_ACTIONS.find((item) => item.command === normalized) ?? null;
}

export function getSlashActionsByGroup(items: ComposerActionItem[]): ComposerActionGroup[] {
  return GROUP_ORDER.map((id) => ({
    id,
    label: {
      context: "上下文",
      settings: "配置",
      workflows: "工作流",
      skills: "技能",
      session: "会话",
    }[id],
    items: items.filter((item) => item.group === id),
  })).filter((group) => group.items.length > 0);
}

export function isVisiblePromptTemplateAction(item: ComposerActionItem): boolean {
  return Boolean((item as { template?: unknown }).template);
}
```

- [ ] **Step 4: Run tests to verify Task 1 passes**

Run:

```bash
cd frontend
npm test -- --run src/lib/composer-action-model.test.ts src/lib/composer-commands.test.ts
```

Expected: PASS for the new tests. Existing tests that assert mojibake template descriptions may still fail until Task 2 updates them.

- [ ] **Step 5: Commit Task 1**

```bash
git add frontend/src/lib/composer-action-model.ts frontend/src/lib/composer-action-model.test.ts frontend/src/lib/composer-commands.test.ts
git commit -m "feat: add typed composer action model"
```

---

### Task 2: Make Slash Commands Action-First, Not Template-First

**Files:**
- Modify: `frontend/src/lib/composer-commands.ts`
- Modify: `frontend/src/lib/runtime-commands.ts`
- Modify: `frontend/src/lib/composer-commands.test.ts`
- Modify: `frontend/src/lib/runtime-commands.test.ts`

- [ ] **Step 1: Write failing tests for slash command behavior**

Replace the current "action commands from picker" test in `frontend/src/lib/composer-commands.test.ts` with:

```ts
import { COMPOSER_ACTIONS } from "./composer-action-model";

it("keeps slash picker selections action-first", () => {
  const commands = new Map(COMPOSER_ACTIONS.map((item) => [item.command, item]));

  expect(commands.get("skills")?.action).toEqual({ kind: "open-inline-panel", panel: "skills" });
  expect(commands.get("mcp")?.action).toEqual({ kind: "open-inline-panel", panel: "mcp-status" });
  expect(commands.get("permissions")?.action).toEqual({ kind: "open-permission-panel" });
  expect(commands.get("model")?.action).toEqual({ kind: "open-settings-page", page: "model" });
  expect(commands.get("review")?.action).toEqual({ kind: "draft-chip", chip: "review" });
});
```

Add to `frontend/src/lib/runtime-commands.test.ts`:

```ts
it("does not expand /review into visible prompt text", () => {
  const reviewOption: ComposerSlashCommandOption = {
    id: "action-review",
    command: "review",
    label: "/review",
    description: "检查风险并给出修改建议",
    action: { kind: "draft-chip", chip: "review" },
  };

  const result = resolveComposerCommandInput("/review auth flow", {
    localHandlers: createLocalSlashCommandHandlers(createDeps().deps),
    slashCommandLookup: new Map([["review", reviewOption]]),
  });

  expect(result).toEqual({
    handled: true,
    nextContent: "",
    action: { kind: "draft-chip", chip: "review", query: "auth flow" },
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd frontend
npm test -- --run src/lib/composer-commands.test.ts src/lib/runtime-commands.test.ts
```

Expected: FAIL because `ComposerSlashCommandOption` does not yet include `action`, and `SlashCommandResolution` cannot return an action payload.

- [ ] **Step 3: Update command types**

In `frontend/src/lib/composer-commands.ts`, replace `ComposerSlashCommandOption` with:

```ts
import type { ComposerActionKind } from "./composer-action-model";

export interface ComposerSlashCommandOption {
  id: string;
  command: string;
  label: string;
  description: string;
  searchText?: string;
  type?: string;
  source?: string;
  enabled?: boolean;
  availability?: ComposerCommandAvailability;
  action: ComposerActionKind;
  template?: string;
}
```

Replace `COMPOSER_SLASH_COMMANDS` with action-backed definitions derived from Task 1:

```ts
import { COMPOSER_ACTIONS } from "./composer-action-model";

export const COMPOSER_SLASH_COMMANDS: ComposerSlashCommandOption[] = COMPOSER_ACTIONS.map((item) => ({
  id: item.id,
  command: item.command,
  label: item.label,
  description: item.description,
  searchText: item.searchText,
  action: item.action,
}));
```

Update `normalizeComposerSlashCommands` so remote commands are accepted only when they have a safe template action:

```ts
const template = typeof payload.template === "string" ? payload.template.trimEnd() : "";
const description = typeof payload.description === "string" ? payload.description.trim() : "";
if (!enabled || !description) {
  continue;
}

const option: ComposerSlashCommandOption = {
  id,
  command,
  label,
  description,
  ...(template ? { template } : {}),
  action: template
    ? { kind: "draft-chip", chip: "docs" }
    : { kind: "send-runtime-command", command: "status" },
};
```

Then keep local actions authoritative:

```ts
export function shouldRunSlashCommandOnSelect(command: string): boolean {
  const action = COMPOSER_SLASH_COMMANDS.find((item) => item.command === normalizeSlashCommandToken(command))?.action;
  return Boolean(action && action.kind !== "draft-chip");
}
```

- [ ] **Step 4: Update runtime resolution**

In `frontend/src/lib/runtime-commands.ts`, change:

```ts
export type SlashCommandResolution = { handled: boolean; nextContent: string };
```

to:

```ts
import type { ComposerActionKind } from "./composer-action-model";

export type SlashCommandResolution = {
  handled: boolean;
  nextContent: string;
  action?: ComposerActionKind & { query?: string };
};
```

In `resolveComposerCommandInput`, replace the template fallback:

```ts
const slashTemplate = options.slashCommandLookup.get(command);
if (!slashTemplate) {
  return { handled: false, nextContent: rawContent };
}

if (slashTemplate.action.kind === "draft-chip") {
  return {
    handled: true,
    nextContent: "",
    action: { ...slashTemplate.action, query: remainder },
  };
}

return {
  handled: true,
  nextContent: "",
  action: slashTemplate.action,
};
```

- [ ] **Step 5: Run tests to verify Task 2 passes**

Run:

```bash
cd frontend
npm test -- --run src/lib/composer-action-model.test.ts src/lib/composer-commands.test.ts src/lib/runtime-commands.test.ts
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add frontend/src/lib/composer-commands.ts frontend/src/lib/runtime-commands.ts frontend/src/lib/composer-commands.test.ts frontend/src/lib/runtime-commands.test.ts
git commit -m "refactor: make composer slash commands action-first"
```

---

### Task 3: Restrict `@` Mentions To Files And Folders

**Files:**
- Modify: `frontend/src/lib/composer-mentions.ts`
- Add/Modify: `frontend/src/lib/composer-mentions.test.ts`
- Modify: `frontend/src/components/ChatPanel.tsx`
- Modify: `frontend/src/lib/attachment-ui-regression.test.ts`

- [ ] **Step 1: Write failing tests for file-only mentions**

Create `frontend/src/lib/composer-mentions.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import {
  detectMentionQuery,
  formatMentionToken,
  parseComposerMentions,
  replaceMentionQuery,
  type ComposerMentionOption,
} from "./composer-mentions";

describe("composer mentions", () => {
  it("formats file and folder tokens only", () => {
    expect(formatMentionToken("file", "src/App.tsx")).toBe("@file:src/App.tsx");
    expect(formatMentionToken("folder", "frontend/src")).toBe("@folder:frontend/src");
  });

  it("ignores skill and mcp tokens because slash owns those actions", () => {
    const parsed = parseComposerMentions("check @skill:ui-reviewer and @mcp:github with @file:src/App.tsx");

    expect(parsed.mentions).toEqual([
      { kind: "file", value: "src/App.tsx", token: "@file:src/App.tsx" },
    ]);
    expect(parsed.cleanedText).toBe("check @skill:ui-reviewer and @mcp:github with");
  });

  it("replaces @ query with a file token", () => {
    const query = detectMentionQuery("review @src", 11);
    const option: ComposerMentionOption = {
      id: "file:src/App.tsx",
      kind: "file",
      value: "src/App.tsx",
      label: "src/App.tsx",
    };

    expect(replaceMentionQuery("review @src", query!, option)).toEqual({
      nextInput: "review @file:src/App.tsx ",
      cursor: "review @file:src/App.tsx ".length,
    });
  });
});
```

Add to `frontend/src/lib/attachment-ui-regression.test.ts`:

```ts
it("does not put skills or MCP in the @ mention menu", () => {
  expect(chatPanelSource).not.toContain('kind: "skill"');
  expect(chatPanelSource).not.toContain('kind: "mcp"');
  expect(chatPanelSource).toContain('kind: "file" as const');
  expect(chatPanelSource).toContain('kind: "folder"');
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd frontend
npm test -- --run src/lib/composer-mentions.test.ts src/lib/attachment-ui-regression.test.ts
```

Expected: FAIL because `MentionKind` still includes `skill` and `mcp`, and `ChatPanel.tsx` still adds skill/MCP mention options.

- [ ] **Step 3: Update mention types**

In `frontend/src/lib/composer-mentions.ts`, change:

```ts
export type MentionKind = "file" | "folder" | "skill" | "mcp";

const MENTION_KINDS: MentionKind[] = ["file", "folder", "skill", "mcp"];
const TOKEN_PATTERN = /@(file|folder|skill|mcp):([^\s@]+)/gi;
```

to:

```ts
export type MentionKind = "file" | "folder";

const MENTION_KINDS: MentionKind[] = ["file", "folder"];
const TOKEN_PATTERN = /@(file|folder):([^\s@]+)/gi;
```

- [ ] **Step 4: Remove Skills/MCP from mention options**

In `frontend/src/components/ChatPanel.tsx`, delete the two loops that append skill and mcp entries:

```ts
for (const skill of skills) {
  ...
}

for (const server of mcpServers) {
  ...
}
```

Update the dependency list for `mentionOptions` from:

```ts
}, [mcpServers, skills, workspaceEntries, workspaceMentionSearchResults]);
```

to:

```ts
}, [workspaceEntries, workspaceMentionSearchResults]);
```

- [ ] **Step 5: Run tests to verify Task 3 passes**

Run:

```bash
cd frontend
npm test -- --run src/lib/composer-mentions.test.ts src/lib/attachment-ui-regression.test.ts
```

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

```bash
git add frontend/src/lib/composer-mentions.ts frontend/src/lib/composer-mentions.test.ts frontend/src/components/ChatPanel.tsx frontend/src/lib/attachment-ui-regression.test.ts
git commit -m "refactor: reserve mentions for files and folders"
```

---

### Task 4: Extract Claude Code-Style Command And Mention Palettes

**Files:**
- Create: `frontend/src/components/ComposerCommandPalette.tsx`
- Create: `frontend/src/components/ComposerMentionPalette.tsx`
- Modify: `frontend/src/components/ChatInputBar.tsx`
- Modify: `frontend/styles/components.css`
- Modify: `frontend/src/lib/chat-panel-architecture.test.ts`

- [ ] **Step 1: Write failing architecture tests**

Add to `frontend/src/lib/chat-panel-architecture.test.ts`:

```ts
import composerCommandPaletteSource from "../components/ComposerCommandPalette.tsx?raw";
import composerMentionPaletteSource from "../components/ComposerMentionPalette.tsx?raw";

it("renders slash as a grouped command palette instead of inline prompt insertion UI", () => {
  expect(chatInputBarSource).toContain('import { ComposerCommandPalette } from "./ComposerCommandPalette";');
  expect(chatInputBarSource).toContain("<ComposerCommandPalette");
  expect(chatInputBarSource).not.toContain("replaceSlashCommandQuery(input, query, option)");
  expect(chatInputBarSource).not.toContain('template: "');
  expect(composerCommandPaletteSource).toContain("getSlashActionsByGroup");
  expect(composerCommandPaletteSource).toContain("composer-command-group");
  expect(composerCommandPaletteSource).toContain("composer-command-current");
  expect(composerCommandPaletteSource).toContain("aria-selected");
});

it("renders @ as a file/folder mention palette", () => {
  expect(chatInputBarSource).toContain('import { ComposerMentionPalette } from "./ComposerMentionPalette";');
  expect(chatInputBarSource).toContain("<ComposerMentionPalette");
  expect(composerMentionPaletteSource).toContain("composer-mention-kind");
  expect(composerMentionPaletteSource).toContain("option.kind === \"folder\"");
  expect(composerMentionPaletteSource).not.toContain("skill");
  expect(composerMentionPaletteSource).not.toContain("mcp");
});

it("moves composer menu styling out of inline style blobs", () => {
  expect(chatInputBarSource).not.toContain('boxShadow: "0 18px 46px rgba(0,0,0,0.48)"');
  expect(chatInputBarSource).not.toContain('backdropFilter: "blur(12px)"');
  expect(componentsCssSource).toContain(".composer-command-palette");
  expect(componentsCssSource).toContain(".composer-command-row");
  expect(componentsCssSource).toContain(".composer-mention-palette");
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd frontend
npm test -- --run src/lib/chat-panel-architecture.test.ts
```

Expected: FAIL because the palette components do not exist and `ChatInputBar` still renders inline menu markup.

- [ ] **Step 3: Create command palette component**

Create `frontend/src/components/ComposerCommandPalette.tsx`:

```tsx
import React from "react";
import {
  Activity,
  BadgeCheck,
  Box,
  Brain,
  Bug,
  Database,
  ListChecks,
  Package,
  Plug,
  Plus,
  Shield,
  Sparkles,
  Terminal,
} from "lucide-react";
import { getSlashActionsByGroup, type ComposerActionItem } from "../lib/composer-action-model";

interface ComposerCommandPaletteProps {
  items: ComposerActionItem[];
  activeIndex: number;
  onActiveIndexChange: (index: number) => void;
  onSelect: (item: ComposerActionItem) => void;
  paletteRef: React.RefObject<HTMLDivElement | null>;
}

const ICONS = {
  activity: Activity,
  "badge-check": BadgeCheck,
  box: Box,
  brain: Brain,
  bug: Bug,
  database: Database,
  "list-checks": ListChecks,
  package: Package,
  plug: Plug,
  plus: Plus,
  shield: Shield,
  sparkles: Sparkles,
  terminal: Terminal,
} as const;

export function ComposerCommandPalette({
  items,
  activeIndex,
  onActiveIndexChange,
  onSelect,
  paletteRef,
}: ComposerCommandPaletteProps) {
  const groups = getSlashActionsByGroup(items);
  let rowIndex = -1;

  return (
    <div className="composer-command-palette" role="listbox" aria-label="命令">
      <div className="composer-palette-heading">命令</div>
      <div ref={paletteRef} className="composer-command-scroll">
        {groups.map((group) => (
          <section key={group.id} className="composer-command-group" aria-label={group.label}>
            <div className="composer-command-group-label">{group.label}</div>
            {group.items.map((item) => {
              rowIndex += 1;
              const currentIndex = rowIndex;
              const Icon = ICONS[item.icon as keyof typeof ICONS] ?? Sparkles;
              const active = currentIndex === activeIndex;

              return (
                <button
                  key={item.id}
                  type="button"
                  role="option"
                  aria-selected={active}
                  data-composer-option-active={active ? "true" : "false"}
                  className={active ? "composer-command-row active" : "composer-command-row"}
                  onMouseEnter={() => onActiveIndexChange(currentIndex)}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => onSelect(item)}
                >
                  <span className="composer-command-icon" aria-hidden="true">
                    <Icon size={16} strokeWidth={2.2} />
                  </span>
                  <span className="composer-command-copy">
                    <strong>{item.title}</strong>
                    <span>{item.description}</span>
                  </span>
                  {item.currentValue ? <span className="composer-command-current">{item.currentValue}</span> : null}
                </button>
              );
            })}
          </section>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Create mention palette component**

Create `frontend/src/components/ComposerMentionPalette.tsx`:

```tsx
import React from "react";
import { FileText, FolderClosed } from "lucide-react";
import type { ComposerMentionOption } from "../lib/composer-mentions";

interface ComposerMentionPaletteProps {
  items: ComposerMentionOption[];
  activeIndex: number;
  onActiveIndexChange: (index: number) => void;
  onSelect: (item: ComposerMentionOption) => void;
  paletteRef: React.RefObject<HTMLDivElement | null>;
}

export function ComposerMentionPalette({
  items,
  activeIndex,
  onActiveIndexChange,
  onSelect,
  paletteRef,
}: ComposerMentionPaletteProps) {
  return (
    <div className="composer-mention-palette" role="listbox" aria-label="文件">
      <div className="composer-palette-heading">文件</div>
      <div ref={paletteRef} className="composer-mention-scroll">
        {items.map((option, index) => {
          const active = index === activeIndex;
          const Icon = option.kind === "folder" ? FolderClosed : FileText;
          return (
            <button
              key={option.id}
              type="button"
              role="option"
              aria-selected={active}
              data-composer-option-active={active ? "true" : "false"}
              className={active ? "composer-mention-row active" : "composer-mention-row"}
              onMouseEnter={() => onActiveIndexChange(index)}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => onSelect(option)}
            >
              <span className="composer-mention-kind" data-kind={option.kind}>
                <Icon size={15} strokeWidth={2.2} />
              </span>
              <span className="composer-mention-copy">
                <strong>{option.label}</strong>
                {option.description ? <span>{option.description}</span> : null}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Refactor ChatInputBar to use palette components**

In `frontend/src/components/ChatInputBar.tsx`:

Add imports:

```tsx
import { ComposerCommandPalette } from "./ComposerCommandPalette";
import { ComposerMentionPalette } from "./ComposerMentionPalette";
import type { ComposerActionItem } from "../lib/composer-action-model";
```

Change slash command props:

```ts
slashCommands: ComposerActionItem[];
onSlashCommandSelect: (option: ComposerActionItem) => boolean;
```

Replace the inline slash menu JSX with:

```tsx
{slashVisible ? (
  <ComposerCommandPalette
    items={filteredSlashCommands}
    activeIndex={slashActiveIndex}
    onActiveIndexChange={setSlashActiveIndex}
    onSelect={(option) => applySlashCommand(option)}
    paletteRef={slashMenuRef}
  />
) : null}
```

Replace the inline mention menu JSX with:

```tsx
{mentionVisible ? (
  <ComposerMentionPalette
    items={filteredMentionOptions}
    activeIndex={mentionActiveIndex}
    onActiveIndexChange={setMentionActiveIndex}
    onSelect={(option) => applyMentionOption(option)}
    paletteRef={mentionMenuRef}
  />
) : null}
```

In `applySlashCommand`, remove `replaceSlashCommandQuery` entirely:

```ts
const applySlashCommand = (option: ComposerActionItem, query = currentSlashQuery) => {
  if (!query) return;

  if (onSlashCommandSelect(option)) {
    const nextInput = `${input.slice(0, query.start)}${input.slice(query.end)}`;
    onInputChange(nextInput);
    setCaretPosition(query.start);
    requestAnimationFrame(() => {
      const textarea = textareaRef.current;
      if (!textarea) return;
      setTextareaCaret(textarea, query.start);
      resizeComposerTextarea(textarea, 400);
    });
  }
  setSlashActiveIndex(0);
};
```

- [ ] **Step 6: Add CSS**

Append to `frontend/styles/components.css`:

```css
.composer-command-palette,
.composer-mention-palette {
  position: absolute;
  right: 0;
  bottom: 100%;
  left: 0;
  z-index: 40;
  margin-bottom: 8px;
  overflow: hidden;
  border: 1px solid var(--border-strong);
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface-panel) 96%, transparent);
  box-shadow: 0 18px 46px rgba(0, 0, 0, 0.42);
  backdrop-filter: blur(14px);
  animation: composer-menu-in 140ms ease-out;
}

.composer-palette-heading {
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-muted);
  padding: 9px 12px;
  font-size: 11px;
  font-weight: 700;
}

.composer-command-scroll,
.composer-mention-scroll {
  max-height: 360px;
  overflow-y: auto;
  padding: 8px;
}

.composer-command-group + .composer-command-group {
  margin-top: 8px;
}

.composer-command-group-label {
  color: var(--text-muted);
  padding: 4px 6px 6px;
  font-size: 11px;
  font-weight: 700;
}

.composer-command-row,
.composer-mention-row {
  display: grid;
  width: 100%;
  min-height: 42px;
  align-items: center;
  gap: 10px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  padding: 8px 10px;
  text-align: left;
}

.composer-command-row {
  grid-template-columns: 28px minmax(0, 1fr) auto;
}

.composer-mention-row {
  grid-template-columns: 28px minmax(0, 1fr);
}

.composer-command-row.active,
.composer-mention-row.active {
  background: var(--surface-hover);
  color: var(--text-primary);
}

.composer-command-icon,
.composer-mention-kind {
  display: inline-flex;
  width: 28px;
  height: 28px;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  background: var(--surface-soft);
  color: var(--accent-primary);
}

.composer-command-copy,
.composer-mention-copy {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.composer-command-copy strong,
.composer-mention-copy strong {
  overflow: hidden;
  color: inherit;
  font-size: 13px;
  font-weight: 680;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.composer-command-copy span,
.composer-mention-copy span {
  overflow: hidden;
  color: var(--text-muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.composer-command-current {
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  color: var(--text-muted);
  padding: 3px 8px;
  font-size: 11px;
}
```

- [ ] **Step 7: Run architecture tests**

Run:

```bash
cd frontend
npm test -- --run src/lib/chat-panel-architecture.test.ts
```

Expected: PASS.

- [ ] **Step 8: Commit Task 4**

```bash
git add frontend/src/components/ComposerCommandPalette.tsx frontend/src/components/ComposerMentionPalette.tsx frontend/src/components/ChatInputBar.tsx frontend/styles/components.css frontend/src/lib/chat-panel-architecture.test.ts
git commit -m "feat: add grouped composer command palettes"
```

---

### Task 5: Add Inline Action Panels And Composer Chips

**Files:**
- Create: `frontend/src/components/ComposerActionPanel.tsx`
- Modify: `frontend/src/components/ChatPanel.tsx`
- Modify: `frontend/src/components/ChatInputBar.tsx`
- Modify: `frontend/styles/components.css`
- Modify: `frontend/src/lib/runtime-commands.test.ts`
- Modify: `frontend/src/lib/chat-panel-architecture.test.ts`

- [ ] **Step 1: Write failing tests for chips and panels**

Add to `frontend/src/lib/chat-panel-architecture.test.ts`:

```ts
import composerActionPanelSource from "../components/ComposerActionPanel.tsx?raw";

it("shows action chips and inline panels instead of dumping slash prompt text", () => {
  expect(chatPanelSource).toContain("selectedComposerAction");
  expect(chatPanelSource).toContain("setSelectedComposerAction");
  expect(chatPanelSource).toContain("<ComposerActionPanel");
  expect(chatInputBarSource).toContain("composerActionChip");
  expect(chatInputBarSource).toContain("onClearComposerAction");
  expect(composerActionPanelSource).toContain("权限");
  expect(composerActionPanelSource).toContain("模型");
  expect(composerActionPanelSource).toContain("计划");
  expect(composerActionPanelSource).toContain("选择本轮要让 Agent 优先使用的能力");
  expect(composerActionPanelSource).toContain("新增连接请走商店按钮");
  expect(composerActionPanelSource).not.toContain("/permissions rules add");
  expect(composerActionPanelSource).not.toContain("onOpenSkills");
  expect(composerActionPanelSource).not.toContain("open-marketplace");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd frontend
npm test -- --run src/lib/chat-panel-architecture.test.ts
```

Expected: FAIL because `ComposerActionPanel.tsx` and action chip state do not exist.

- [ ] **Step 3: Create ComposerActionPanel**

Create `frontend/src/components/ComposerActionPanel.tsx`:

```tsx
import React from "react";
import type { ComposerActionKind } from "../lib/composer-action-model";
import type { PermissionMode } from "../lib/runtime-commands";

interface ComposerActionPanelProps {
  action: ComposerActionKind | null;
  currentPermissionMode: PermissionMode;
  onPermissionModeChange: (mode: PermissionMode) => void;
  onOpenModelSettings: () => void;
  skills: Array<{ name: string; description?: string; active?: boolean }>;
  mcpServers: Array<{ name: string; status: string; toolCount?: number }>;
  onSelectSkill: (skillName: string) => void;
  onClose: () => void;
}

export function ComposerActionPanel({
  action,
  currentPermissionMode,
  onPermissionModeChange,
  onOpenModelSettings,
  skills,
  mcpServers,
  onSelectSkill,
  onClose,
}: ComposerActionPanelProps) {
  if (!action) return null;

  if (action.kind === "open-permission-panel") {
    return (
      <section className="composer-action-panel" aria-label="权限">
        <div className="composer-action-panel-head">
          <strong>权限</strong>
          <button type="button" onClick={onClose}>关闭</button>
        </div>
        <div className="composer-action-segments">
          {[
            { mode: "default" as const, label: "日常", hint: "低风险自动执行" },
            { mode: "plan" as const, label: "先计划", hint: "只分析不改动" },
            { mode: "confirm" as const, label: "每步确认", hint: "执行前先问你" },
            { mode: "bypass" as const, label: "少打断", hint: "适合信任任务" },
          ].map((item) => (
            <button
              key={item.mode}
              type="button"
              className={currentPermissionMode === item.mode ? "active" : ""}
              onClick={() => onPermissionModeChange(item.mode)}
            >
              <strong>{item.label}</strong>
              <span>{item.hint}</span>
            </button>
          ))}
        </div>
      </section>
    );
  }

  if (action.kind === "open-settings-page" && action.page === "model") {
    return (
      <section className="composer-action-panel" aria-label="模型">
        <div className="composer-action-panel-head">
          <strong>模型</strong>
          <button type="button" onClick={onClose}>关闭</button>
        </div>
        <p>切换模型、Provider 或推理参数。</p>
        <button type="button" className="composer-action-primary" onClick={onOpenModelSettings}>
          打开模型设置
        </button>
      </section>
    );
  }

  if (action.kind === "open-inline-panel" && action.panel === "plan") {
    return (
      <section className="composer-action-panel" aria-label="计划">
        <div className="composer-action-panel-head">
          <strong>计划</strong>
          <button type="button" onClick={onClose}>关闭</button>
        </div>
        <p>Agent 会先整理方案和风险，等你确认后再执行。</p>
      </section>
    );
  }

  if (action.kind === "open-inline-panel" && action.panel === "skills") {
    return (
      <section className="composer-action-panel" aria-label="技能">
        <div className="composer-action-panel-head">
          <strong>技能</strong>
          <button type="button" onClick={onClose}>关闭</button>
        </div>
        <p>选择本轮要让 Agent 优先使用的能力。安装新能力请走商店按钮。</p>
        <div className="composer-skill-picker">
          {skills.length > 0 ? skills.map((skill) => (
            <button key={skill.name} type="button" onClick={() => onSelectSkill(skill.name)}>
              <strong>{skill.name}</strong>
              <span>{skill.description || "让 Agent 更擅长这一类任务"}</span>
            </button>
          )) : (
            <span className="composer-action-empty">还没有可用技能，去商店安装后再选择。</span>
          )}
        </div>
      </section>
    );
  }

  if (action.kind === "open-inline-panel" && action.panel === "mcp-status") {
    return (
      <section className="composer-action-panel" aria-label="MCP">
        <div className="composer-action-panel-head">
          <strong>MCP</strong>
          <button type="button" onClick={onClose}>关闭</button>
        </div>
        <p>查看外部连接是否可用。新增连接请走商店按钮。</p>
        <div className="composer-mcp-status-list">
          {mcpServers.length > 0 ? mcpServers.map((server) => (
            <div key={server.name} className="composer-mcp-status-row">
              <strong>{server.name}</strong>
              <span>{server.status} · {server.toolCount ?? 0} 个操作</span>
            </div>
          )) : (
            <span className="composer-action-empty">还没有外部连接。</span>
          )}
        </div>
      </section>
    );
  }

  return null;
}
```

- [ ] **Step 4: Add chip props to ChatInputBar**

In `frontend/src/components/ChatInputBar.tsx`, add props:

```ts
composerActionChip?: { label: string; description: string } | null;
onClearComposerAction?: () => void;
```

Render above textarea:

```tsx
{composerActionChip ? (
  <div className="composer-action-chip" aria-label="已选择的操作">
    <span>
      <strong>{composerActionChip.label}</strong>
      <small>{composerActionChip.description}</small>
    </span>
    <button type="button" onClick={onClearComposerAction} aria-label="移除操作">
      <X style={{ width: 13, height: 13 }} />
    </button>
  </div>
) : null}
```

- [ ] **Step 5: Wire action state in ChatPanel**

In `frontend/src/components/ChatPanel.tsx`, import:

```tsx
import { ComposerActionPanel } from "./ComposerActionPanel";
import type { ComposerActionItem, ComposerActionKind } from "../lib/composer-action-model";
```

Add state:

```ts
const [selectedComposerAction, setSelectedComposerAction] = useState<ComposerActionKind | null>(null);
const [composerActionChip, setComposerActionChip] = useState<{ label: string; description: string; instruction: string } | null>(null);
```

Replace `handleSlashCommandSelect`:

```ts
const handleSlashCommandSelect = useCallback((option: ComposerActionItem) => {
  const { action } = option;

  if (action.kind === "open-settings-page") {
    setMainView({ type: "settings" });
    return true;
  }
  if (action.kind === "open-permission-panel" || action.kind === "open-inline-panel") {
    setSelectedComposerAction(action);
    return true;
  }
  if (action.kind === "draft-chip") {
    const instruction = {
      review: "Review the current project or referenced files. Lead with findings by severity and include file references.",
      debug: "Debug the reported issue end to end: reproduce, isolate root cause, fix minimally, and verify.",
      refactor: "Propose a safe refactor, then apply it in small verified steps.",
      test: "Add or update focused tests, run them, and report coverage gaps.",
      docs: "Create concise developer documentation for the referenced area.",
      file: "Create or update the requested file artifact instead of pasting large content into chat.",
      image: "Prepare an image generation task with subject, style, aspect ratio, and required text.",
      commit: "Summarize changes and produce a clean commit message.",
    }[action.chip];
    setComposerActionChip({ label: option.title, description: option.description, instruction });
    return true;
  }
  if (action.kind === "send-runtime-command") {
    send({ type: action.command === "status" ? "session.status.inspect" : "session.tasks.inspect" } as ClientMessage);
    return true;
  }
  if (action.kind === "conversation" && action.operation === "new") {
    createConversationWithMode("none", false);
    return true;
  }
  if (action.kind === "conversation" && action.operation === "clear") {
    setInput("");
    setDraftAttachments([]);
    setComposerActionChip(null);
    return true;
  }
  return false;
}, [createConversationWithMode, handleOpenExtensionsMarketplace, send]);
```

Before `<ChatInputBar`, render:

```tsx
<ComposerActionPanel
  action={selectedComposerAction}
  currentPermissionMode={currentPermissionMode}
  skills={skills}
  mcpServers={mcpServers}
  onPermissionModeChange={(mode) => requestPermissionModeChange(mode, "ui:composer-action-panel")}
  onOpenModelSettings={() => setMainView({ type: "settings" })}
  onSelectSkill={(skillName) => {
    send({ type: "load_skill", skill_name: skillName });
    setComposerActionChip({
      label: skillName,
      description: "本轮优先使用这个技能",
      instruction: `Use the installed skill named "${skillName}" when it is relevant to the user's request.`,
    });
    setSelectedComposerAction(null);
  }}
  onClose={() => setSelectedComposerAction(null)}
/>
```

Pass chip props:

```tsx
composerActionChip={composerActionChip}
onClearComposerAction={() => setComposerActionChip(null)}
```

- [ ] **Step 6: Generate hidden instruction only on send**

In `handleSend`, before `resolveMentionedContent`:

```ts
const visibleContent = slashResolved.nextContent || rawContent;
const normalizedContent = composerActionChip
  ? [composerActionChip.instruction, visibleContent].filter(Boolean).join("\n\nUser request:\n")
  : visibleContent;
```

After successful send:

```ts
setComposerActionChip(null);
setSelectedComposerAction(null);
```

- [ ] **Step 7: Add panel/chip CSS**

Append to `frontend/styles/components.css`:

```css
.composer-action-panel,
.composer-action-chip {
  width: min(1180px, calc(100vw - 24px));
  margin: 0 auto 10px;
  border: 1px solid rgba(232, 134, 58, 0.24);
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface-panel) 95%, transparent);
  box-shadow: 0 16px 38px rgba(0, 0, 0, 0.24);
}

.composer-action-panel {
  display: grid;
  gap: 12px;
  padding: 14px;
}

.composer-action-panel-head,
.composer-action-chip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.composer-action-panel-head strong,
.composer-action-chip strong {
  color: var(--text-primary);
  font-size: 13px;
}

.composer-action-panel-head button,
.composer-action-chip button {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--surface-soft);
  color: var(--text-secondary);
  cursor: pointer;
  padding: 6px 9px;
}

.composer-action-segments {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}

.composer-action-segments button {
  display: grid;
  gap: 4px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--surface-soft);
  color: var(--text-secondary);
  cursor: pointer;
  padding: 10px;
  text-align: left;
}

.composer-action-segments button.active {
  border-color: rgba(232, 134, 58, 0.38);
  background: rgba(232, 134, 58, 0.14);
  color: var(--accent-primary);
}

.composer-skill-picker,
.composer-mcp-status-list {
  display: grid;
  gap: 8px;
}

.composer-skill-picker button,
.composer-mcp-status-row {
  display: grid;
  gap: 4px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--surface-soft);
  color: var(--text-secondary);
  padding: 10px;
  text-align: left;
}

.composer-skill-picker button {
  cursor: pointer;
}

.composer-skill-picker button:hover {
  border-color: var(--border-strong);
  background: var(--surface-hover);
  color: var(--text-primary);
}

.composer-action-empty {
  color: var(--text-muted);
  font-size: 13px;
}

.composer-action-chip {
  padding: 9px 10px;
}

.composer-action-chip span {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.composer-action-chip small {
  overflow: hidden;
  color: var(--text-muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
```

- [ ] **Step 8: Run tests**

Run:

```bash
cd frontend
npm test -- --run src/lib/chat-panel-architecture.test.ts src/lib/runtime-commands.test.ts
```

Expected: PASS.

- [ ] **Step 9: Commit Task 5**

```bash
git add frontend/src/components/ComposerActionPanel.tsx frontend/src/components/ChatPanel.tsx frontend/src/components/ChatInputBar.tsx frontend/styles/components.css frontend/src/lib/chat-panel-architecture.test.ts frontend/src/lib/runtime-commands.test.ts
git commit -m "feat: add composer action panels and chips"
```

---

### Task 6: Fix Broken Chinese Copy In Composer And Palette Surface

**Files:**
- Modify: `frontend/src/components/ChatInputBar.tsx`
- Modify: `frontend/src/components/ChatPanel.tsx`
- Modify: `frontend/src/lib/composer-commands.test.ts`
- Modify: `frontend/src/lib/attachment-ui-regression.test.ts`
- Modify: `frontend/src/lib/ui-copy-regression.test.ts`

- [ ] **Step 1: Write failing copy regression test**

Add to `frontend/src/lib/ui-copy-regression.test.ts`:

```ts
import chatInputBarSource from "../components/ChatInputBar.tsx?raw";
import chatPanelSource from "../components/ChatPanel.tsx?raw";
import composerActionModelSource from "./composer-action-model.ts?raw";

it("keeps composer and slash command copy readable Chinese", () => {
  const combined = [chatInputBarSource, chatPanelSource, composerActionModelSource].join("\n");
  const mojibakePattern = /[\uFFFD\u951F]|\u9409|\u934F|\u6D93|\u93C2|\u7EFE|\u59DD|\u6D63|\u93B5|\u6434|\u60E7|\u6357|\u6E45|\u52EB/;

  expect(combined).toContain("输入任务，或把文件拖进来一起发送");
  expect(combined).toContain("添加文件");
  expect(combined).toContain("发送消息");
  expect(combined).toContain("权限");
  expect(combined).toContain("技能");
  expect(combined).not.toMatch(mojibakePattern);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd frontend
npm test -- --run src/lib/ui-copy-regression.test.ts
```

Expected: FAIL because current files contain mojibake strings.

- [ ] **Step 3: Replace composer copy**

In `frontend/src/components/ChatInputBar.tsx`, replace visible strings:

```tsx
return { icon: ImageIcon, label: "图片", accent: "image" };
return { icon: Archive, label: "压缩包", accent: "archive" };
return { icon: Presentation, label: "演示文稿", accent: "presentation" };
return { icon: Code2, label: "代码", accent: "code" };
if (status === "ready") return "已添加";
if (status === "uploading") return "正在添加文件";
return "添加失败";
```

Replace menu headings/helper text:

```tsx
aria-label="已添加的文件"
title="重新添加"
aria-label={`重新添加 ${draft.fileName}`}
title="移除文件"
aria-label={`移除文件 ${draft.fileName}`}
placeholder="输入任务，或把文件拖进来一起发送"
title="添加文件"
aria-label="添加文件"
<span className="chat-composer-helper-text">/ 命令 · @ 文件</span>
title={streaming ? "停止生成" : "发送消息"}
aria-label={streaming ? "停止生成" : "发送消息"}
```

- [ ] **Step 4: Replace ChatPanel composer-adjacent copy**

In `frontend/src/components/ChatPanel.tsx`, replace permission panel copy:

```tsx
<section className="permission-quick-panel" aria-label="权限设置">
  <div className="permission-quick-copy">
    <span>权限怎么管</span>
    <strong>选择 Agent 做事前要不要先问你</strong>
  </div>
```

Replace upload details:

```ts
detail: payload.indexed_chunks ? "AI 会先读取文件再回答" : "已添加到本轮对话",
detail: "正在添加文件",
detail: "正在重新添加",
```

Replace action labels:

```tsx
label: "先计划",
label: "每步确认",
label: "少打断",
"查看规则"
"收起"
```

- [ ] **Step 5: Run copy tests**

Run:

```bash
cd frontend
npm test -- --run src/lib/ui-copy-regression.test.ts src/lib/attachment-ui-regression.test.ts src/lib/composer-commands.test.ts
```

Expected: PASS.

- [ ] **Step 6: Commit Task 6**

```bash
git add frontend/src/components/ChatInputBar.tsx frontend/src/components/ChatPanel.tsx frontend/src/lib/ui-copy-regression.test.ts frontend/src/lib/attachment-ui-regression.test.ts frontend/src/lib/composer-commands.test.ts
git commit -m "fix: restore readable composer copy"
```

---

### Task 7: Claude Code-Inspired Visual System, Output Readability, And Terminal Polish

**Files:**
- Modify: `frontend/src/index.css`
- Modify: `frontend/style-v2.css`
- Modify: `frontend/styles/components.css`
- Modify: `frontend/styles/layout.css`
- Modify: `frontend/styles/effects-and-overrides.css`
- Modify: `frontend/src/components/MessageBubble.tsx`
- Modify: `frontend/src/components/TerminalPanel.tsx`
- Test: `frontend/src/lib/workbench-visual-system.test.ts`
- Test: `frontend/src/lib/message-bubble-architecture.test.ts`
- Test: `frontend/src/lib/workspace-shell.test.ts`

- [ ] **Step 1: Write failing tests for palette and output readability**

Create `frontend/src/lib/workbench-visual-system.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import indexCss from "../index.css?raw";
import styleV2Css from "../../style-v2.css?raw";
import componentsCss from "../../styles/components.css?raw";
import effectsCss from "../../styles/effects-and-overrides.css?raw";

const allCss = [indexCss, styleV2Css, componentsCss, effectsCss].join("\n");

describe("workbench visual system", () => {
  it("uses a restrained Claude Code-like dark palette instead of orange-heavy chrome", () => {
    expect(indexCss).toContain("--surface-base: #0d0d10");
    expect(indexCss).toContain("--surface-page: #121216");
    expect(indexCss).toContain("--accent-primary: #8b7cf6");
    expect(indexCss).toContain("--accent-secondary: #a78bfa");
    expect(indexCss).toContain("--state-warning");

    const orangeChromeMatches = allCss.match(/232,\s*134,\s*58|#e8863a/gi) ?? [];
    expect(orangeChromeMatches.length).toBeLessThanOrEqual(3);
  });

  it("keeps assistant strong text readable without pure white heavy bold", () => {
    expect(allCss).toContain(".message-bubble--assistant");
    expect(allCss).toContain(".message-bubble--assistant strong");
    expect(allCss).not.toMatch(/\.message-bubble--assistant\s+strong[\s\S]{0,180}color:\s*(#fff|white)/i);
    expect(allCss).not.toMatch(/\.message-bubble--assistant\s+strong[\s\S]{0,180}font-weight:\s*(700|800|900)/i);
  });
});
```

Add to `frontend/src/lib/message-bubble-architecture.test.ts`:

```ts
import messageBubbleSource from "../components/MessageBubble.tsx?raw";
import componentsCssSource from "../../styles/components.css?raw";

it("renders assistant output as readable prose, not high-glare bold blocks", () => {
  expect(messageBubbleSource).toContain("message-bubble--assistant");
  expect(componentsCssSource).toContain(".message-bubble--assistant .markdown-body");
  expect(componentsCssSource).toMatch(/line-height:\s*1\.(6[5-9]|7)/);
  expect(componentsCssSource).toMatch(/max-width:\s*(72ch|76ch|78ch)/);
});
```

- [ ] **Step 2: Write failing tests for terminal structure**

Add to `frontend/src/lib/workspace-shell.test.ts`:

```ts
import terminalPanelSource from "../components/TerminalPanel.tsx?raw";
import componentsCssSource from "../../styles/components.css?raw";

it("treats terminal as a desktop work surface with clear controls", () => {
  expect(terminalPanelSource).toContain("terminal-session-tabs");
  expect(terminalPanelSource).toContain("terminal-context-bar");
  expect(terminalPanelSource).toContain("terminal-shell-select");
  expect(terminalPanelSource).toContain("terminal-action-clear");
  expect(terminalPanelSource).toContain("terminal-action-copy");
  expect(terminalPanelSource).toContain("terminal-status-pill");
  expect(componentsCssSource).toContain(".terminal-panel-v2");
  expect(componentsCssSource).toContain(".terminal-empty-compact");
});

it("keeps terminal labels short and user-facing", () => {
  expect(terminalPanelSource).toContain("新会话");
  expect(terminalPanelSource).toContain("清空");
  expect(terminalPanelSource).toContain("复制");
  expect(terminalPanelSource).toContain("正在运行");
  expect(terminalPanelSource).not.toMatch(/schema|payload|pty|transport|runtime trace/i);
});
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
cd frontend
npm test -- --run src/lib/workbench-visual-system.test.ts src/lib/message-bubble-architecture.test.ts src/lib/workspace-shell.test.ts
```

Expected: FAIL because the visual tokens, assistant output rules, and terminal structure are not yet aligned.

- [ ] **Step 4: Update dark palette tokens**

In `frontend/src/index.css`, set the core tokens to this direction:

```css
:root {
  --surface-base: #0d0d10;
  --surface-page: #121216;
  --surface-panel: #18181d;
  --surface-soft: #202027;
  --surface-hover: #282832;
  --border-subtle: rgba(255, 255, 255, 0.075);
  --border-strong: rgba(255, 255, 255, 0.14);
  --text-primary: #d9d7e2;
  --text-secondary: #aaa6b8;
  --text-muted: #7f7a8e;
  --accent-primary: #8b7cf6;
  --accent-secondary: #a78bfa;
  --ai-accent-rgb: 139, 124, 246;
  --state-warning: #eab86b;
}
```

Then replace chrome-level orange usage in `frontend/style-v2.css`, `frontend/styles/layout.css`, and `frontend/styles/effects-and-overrides.css` with violet/indigo token references:

```css
border-color: color-mix(in srgb, var(--accent-primary) 26%, transparent);
box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent-primary) 16%, transparent);
background: color-mix(in srgb, var(--accent-primary) 12%, var(--surface-soft));
```

Keep warm color only for caution/warning states:

```css
.tone-warning,
.task-indicator--attention {
  color: var(--state-warning);
  border-color: color-mix(in srgb, var(--state-warning) 34%, transparent);
}
```

- [ ] **Step 5: Fix assistant output readability**

In `frontend/src/components/MessageBubble.tsx`, make sure assistant bubbles carry the role class:

```tsx
className={cn(
  "message-bubble",
  role === "assistant" && "message-bubble--assistant",
  role === "user" && "message-bubble--user"
)}
```

Add to `frontend/styles/components.css`:

```css
.message-bubble--assistant {
  color: var(--text-primary);
}

.message-bubble--assistant .markdown-body {
  max-width: 76ch;
  color: var(--text-primary);
  font-size: calc(15px * var(--chat-text-scale, 1));
  line-height: 1.72;
  letter-spacing: 0;
}

.message-bubble--assistant .markdown-body p {
  margin: 0 0 0.72em;
}

.message-bubble--assistant .markdown-body strong {
  color: #ece9f4;
  font-weight: 600;
}

.message-bubble--assistant .markdown-body h1,
.message-bubble--assistant .markdown-body h2,
.message-bubble--assistant .markdown-body h3 {
  color: #f1eef8;
  font-weight: 620;
  line-height: 1.32;
}

.message-bubble--assistant .markdown-body code {
  color: #ddd7ff;
  background: rgba(139, 124, 246, 0.12);
}
```

- [ ] **Step 6: Redesign terminal panel**

In `frontend/src/components/TerminalPanel.tsx`, structure the panel like this:

```tsx
<section className="terminal-panel-v2" aria-label="终端">
  <header className="terminal-context-bar">
    <div className="terminal-session-tabs" role="tablist" aria-label="终端会话">
      {sessions.map((session) => (
        <button key={session.id} type="button" role="tab" aria-selected={session.id === activeSessionId}>
          <TerminalIcon aria-hidden="true" />
          <span>{session.name}</span>
        </button>
      ))}
      <button type="button" className="terminal-new-session">新会话</button>
    </div>
    <div className="terminal-context-meta">
      <span>{workspaceName}</span>
      <span>{branchName}</span>
      <span className="terminal-status-pill">{isRunning ? "正在运行" : "空闲"}</span>
    </div>
  </header>

  <div className="terminal-toolbar">
    <select className="terminal-shell-select" aria-label="选择 Shell" value={shell} onChange={handleShellChange}>
      <option value="powershell">PowerShell</option>
      <option value="cmd">Command Prompt</option>
      <option value="git-bash">Git Bash</option>
    </select>
    <button type="button" className="terminal-action-clear" onClick={handleClear}>清空</button>
    <button type="button" className="terminal-action-copy" onClick={handleCopy}>复制</button>
    <button type="button" onClick={handleStop} disabled={!isRunning}>停止</button>
  </div>

  {hasOutput ? (
    <div className="terminal-output-surface">{terminalOutput}</div>
  ) : (
    <div className="terminal-empty-compact">
      <strong>还没有命令输出</strong>
      <span>运行任务后，输出会显示在这里。</span>
    </div>
  )}
</section>
```

Add terminal CSS to `frontend/styles/components.css`:

```css
.terminal-panel-v2 {
  display: grid;
  min-height: 260px;
  grid-template-rows: auto auto minmax(0, 1fr);
  background: var(--surface-base);
  color: var(--text-primary);
}

.terminal-context-bar,
.terminal-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  border-bottom: 1px solid var(--border-subtle);
  padding: 8px 10px;
}

.terminal-session-tabs {
  display: flex;
  min-width: 0;
  gap: 6px;
  overflow-x: auto;
}

.terminal-session-tabs button,
.terminal-toolbar button,
.terminal-shell-select {
  min-height: 30px;
  border: 1px solid var(--border-subtle);
  border-radius: 7px;
  background: var(--surface-soft);
  color: var(--text-secondary);
}

.terminal-status-pill {
  border: 1px solid color-mix(in srgb, var(--accent-primary) 24%, transparent);
  border-radius: 999px;
  color: var(--accent-secondary);
  padding: 3px 8px;
}

.terminal-output-surface {
  overflow: auto;
  padding: 12px;
  font-family: "JetBrains Mono", "SFMono-Regular", Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.58;
}

.terminal-empty-compact {
  display: grid;
  align-content: center;
  justify-items: center;
  gap: 6px;
  min-height: 180px;
  color: var(--text-muted);
  text-align: center;
}
```

- [ ] **Step 7: Run tests and build**

Run:

```bash
cd frontend
npm test -- --run src/lib/workbench-visual-system.test.ts src/lib/message-bubble-architecture.test.ts src/lib/workspace-shell.test.ts
npm run build
```

Expected: PASS.

- [ ] **Step 8: Commit Task 7**

```bash
git add frontend/src/index.css frontend/style-v2.css frontend/styles/components.css frontend/styles/layout.css frontend/styles/effects-and-overrides.css frontend/src/components/MessageBubble.tsx frontend/src/components/TerminalPanel.tsx frontend/src/lib/workbench-visual-system.test.ts frontend/src/lib/message-bubble-architecture.test.ts frontend/src/lib/workspace-shell.test.ts
git commit -m "style: refine workbench palette output and terminal"
```

---

### Task 8: Playwright Interaction Validation

**Files:**
- No source changes unless validation finds a bug.

- [ ] **Step 1: Identify current MiniCode port without killing services**

Run:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'C:\\Desktop\\MiniCode' -and $_.CommandLine -match 'vite' } |
  Select-Object ProcessId, Name, CommandLine | Format-List
```

Expected: one or more MiniCode Vite processes. Pick the command line port that belongs to `C:\Desktop\MiniCode\frontend`. Do not kill any process.

- [ ] **Step 2: Run full frontend tests**

Run:

```bash
cd frontend
npm test -- --run
npm run build
```

Expected: all Vitest tests pass, Vite build succeeds.

- [ ] **Step 3: Verify `/` palette**

Using Playwright on the real MiniCode frontend URL:

1. Focus composer.
2. Type `/`.
3. Verify groups appear: 上下文, 配置, 工作流, 技能, 会话.
4. Press ArrowDown several times.
5. Press Enter on `技能`.
6. Verify the inline Skills selector opens above the composer and composer text is empty.
7. Select one available Skill.
8. Verify a compact Skill chip appears and no prompt paragraph is inserted.
9. Click the independent store button in the toolbar/sidebar.
10. Verify the Skills marketplace opens from that store entry only.
11. Return to chat.
12. Type `/model`, press Enter.
13. Verify settings/model page opens or inline model action panel opens.
14. Return to chat.
15. Type `/review`, press Enter.
16. Verify a compact chip appears above input; textarea does not contain a prompt paragraph.

- [ ] **Step 4: Verify `@` palette**

Using Playwright:

1. Focus composer.
2. Type `@`.
3. Verify only file/folder rows appear.
4. Confirm no Skills/MCP rows appear.
5. Select a file with Enter.
6. Verify textarea contains only `@file:<path>` plus user text.

- [ ] **Step 5: Verify layout**

Using Playwright viewport sizes:

```text
390x844
768x900
1440x900
2560x1207
```

For each viewport:

- No horizontal overflow.
- Palette does not cover send button.
- Palette remains above composer.
- Long paths truncate cleanly.
- Command current-value pill does not overlap text.
- Action chips fit within composer width.

- [ ] **Step 6: Verify assistant output readability**

Using Playwright:

1. Create or load a conversation with assistant output containing paragraphs, headings, bold text, inline code, and a code block.
2. Verify body text is muted off-white, not pure white.
3. Verify bold text is only slightly stronger than body text and remains readable.
4. Verify paragraph line length does not stretch across the full desktop page.
5. Verify streaming output has a subtle surface/shadow cue without flashing, jumping, or covering previous text.
6. Verify the temporary "回到本次回复开头" button appears after generation when the user is scrolled away, returns to the response start, then disappears.

- [ ] **Step 7: Verify terminal interactions**

Using Playwright:

1. Open the terminal dock.
2. Verify session tabs, cwd/branch context, shell selector, clear, copy, stop, and new-session controls are visible.
3. Click each terminal control and verify immediate visual feedback.
4. Verify empty terminal state is compact and functional, not a large decorative splash.
5. Run a harmless command such as `pwd` or `Get-Location`.
6. Verify output uses the terminal font, wraps or scrolls predictably, and does not create horizontal page overflow.

- [ ] **Step 8: Commit validation-only fixes if needed**

If CSS or small behavior fixes were needed:

```bash
git add frontend/src/components frontend/src/lib frontend/styles
git commit -m "fix: polish workbench interactions"
```

If no fixes were needed, do not create an empty commit.

---

## Self-Review

**Spec coverage:** The plan covers `/` as a command/config/local Skill selection/MCP status palette, `@` as file/folder references only, no visible prompt dumping, Claude Code-style grouped rows, actionable panels, independent store access, copy cleanup, Claude Code-inspired dark palette, assistant output readability, terminal redesign, tests, build, and Playwright validation.

**Placeholder scan:** No `TBD`, `TODO`, or "implement later" placeholders remain. Each task includes exact file paths, code snippets, commands, and expected outcomes.

**Type consistency:** `ComposerActionItem`, `ComposerActionKind`, `SlashCommandResolution.action`, `ComposerMentionOption`, terminal class names, assistant output class names, and UI prop names are consistent across tasks.

**Scope control:** This plan does not rewrite backend protocols. It keeps current WebSocket messages and settings/marketplace pages. The marketplace remains reachable from explicit store buttons, while `/skills` is reserved for selecting already available Skills inside the composer.
