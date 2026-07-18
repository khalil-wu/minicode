import type { ClientCommand } from "../protocol/events";
import type { ChatMessage, FileContextRef, SkillContextRef, SkillInfo, SlashCommand } from "../stores/types";

type SendClientCommand = (command: ClientCommand) => boolean | void;

type SendChatMessage = (options: {
  displayContent: string;
  backendContent: string;
  skipLocalAppend: boolean;
}) => boolean | Promise<boolean>;

type SendUserMessage = (content: string) => Promise<boolean>;

export type RuntimeCommandState = {
  conversationId?: string | null;
  agentMode?: "build" | "plan" | "review" | "explore";
  permissionMode?: "ask_permissions" | "plan" | "auto" | "bypass";
  availableSkills?: SkillInfo[];
  skillsMarketplaceOpen?: boolean;
  automationsOpen?: boolean;
  toggleAutomations?: () => void;
  addSelectedSkill?: (skill: Omit<SkillContextRef, "kind">) => void;
  hydrateConversationMessages?: (
    conversationId: string,
    messages: ChatMessage[],
    options?: { activate?: boolean; isStreaming?: boolean },
  ) => void;
  toggleSkillsMarketplace?: () => void;
  setAgentMode?: (mode: "build" | "plan" | "review" | "explore") => void;
  setPermissionMode?: (mode: "ask_permissions" | "plan" | "auto" | "bypass") => void;
  upsertSystemMessage?: (
    id: string,
    content: string,
    options?: { replacePrefix?: string },
  ) => void;
};

export type RuntimeSlashCommandDeps = {
  getState: () => RuntimeCommandState;
  setState: (patch: { messages?: ChatMessage[]; isStreaming?: boolean }) => void;
  sendClientCommand: SendClientCommand;
  sendChatMessage: SendChatMessage;
  sendUserMessage?: SendUserMessage;
  confirmClear: () => Promise<boolean>;
};

export type RuntimeSlashCommandResult = {
  sent: boolean;
  reset: "none" | "input" | "composer";
};

export type ParsedRuntimeSlashInput = {
  command: string;
  rest: string;
  commandLine: string;
  mentions: FileContextRef[];
};

export type RuntimeSlashMenuItem = {
  name: string;
  description: string;
  section?: "Review" | "Context" | "Skills" | "System" | "Tools" | "Project" | "Workspace" | "Commands";
  keywords?: string[];
};

export type RuntimeSlashPaletteItem = RuntimeSlashMenuItem & {
  id: string;
  commandLine: string;
};

export type RuntimeSlashMenuSelectionState = Pick<RuntimeCommandState, "availableSkills"> & {
  slashCommands?: SlashCommand[];
};

export type RuntimeSlashMenuSelection =
  | { kind: "close" }
  | { kind: "skill"; skill: Omit<SkillContextRef, "kind"> }
  | { kind: "tokenize"; command: string }
  | { kind: "execute"; commandLine: string };

export type RuntimeSlashPanelDraftDeps = {
  slashPanelOpen: boolean;
  openSlashPanel: () => void;
  closeSlashPanel: () => void;
  setMenuFilter: (filter: string) => void;
  sendClientCommand: SendClientCommand;
};

export const FALLBACK_RUNTIME_SLASH_COMMANDS: RuntimeSlashMenuItem[] = [
  { name: "/plan", description: "Enable plan mode", section: "Project", keywords: ["design", "readonly", "proposal"] },
  { name: "/review", description: "Review code changes", section: "Review", keywords: ["diff", "pr", "quality"] },
  { name: "/debug", description: "Debug the current issue", section: "Review", keywords: ["bug", "failure", "diagnose"] },
  { name: "/refactor", description: "Refactor safely", section: "Review", keywords: ["cleanup", "rewrite"] },
  { name: "/test", description: "Add or update tests", section: "Review", keywords: ["verify", "coverage"] },
  { name: "/docs", description: "Write developer docs", section: "Review", keywords: ["readme", "documentation"] },
  { name: "/explain", description: "Explain code paths", section: "Review", keywords: ["understand", "trace"] },
  { name: "/commit", description: "Prepare a commit summary", section: "Project", keywords: ["git", "changes"] },
  { name: "/skills", description: "Browse skills", section: "Skills", keywords: ["capabilities", "workflow"] },
  { name: "/model", description: "Choose or inspect the active model", section: "System", keywords: ["provider", "gpt", "reasoning"] },
  { name: "/mcp", description: "Show MCP servers and tools", section: "Tools", keywords: ["connectors", "tools", "servers"] },
  { name: "/permissions", description: "Inspect or change permissions", section: "System", keywords: ["sandbox", "approval", "access"] },
  { name: "/effort", description: "Set reasoning effort", section: "System", keywords: ["model", "thinking"] },
  { name: "/goal", description: "Set or manage the thread goal", section: "Project", keywords: ["objective", "task", "long running"] },
  { name: "/new", description: "Start a new conversation", section: "Project", keywords: ["thread", "session"] },
  { name: "/clear", description: "Clear conversation", section: "Project", keywords: ["reset", "messages"] },
  { name: "/compact", description: "Compact context", section: "Context", keywords: ["summary", "compress"] },
  { name: "/summary", description: "Show or update the task summary", section: "Context", keywords: ["recap", "handoff", "transcript"] },
  { name: "/memory", description: "Set memory mode", section: "Context", keywords: ["remember", "preferences"] },
  { name: "/archive", description: "Archive conversation", section: "Project", keywords: ["thread", "hide"] },
  { name: "/unarchive", description: "Unarchive conversation", section: "Project", keywords: ["thread", "restore"] },
  { name: "/tasks", description: "Show running tasks", section: "Tools", keywords: ["background", "jobs"] },
  { name: "/terminal", description: "Inspect terminal sessions and output", section: "Tools", keywords: ["shell", "logs", "build", "tests"] },
  { name: "/browser", description: "Open or inspect the browser preview", section: "Tools", keywords: ["preview", "web", "app"] },
  { name: "/worktree", description: "Inspect or manage worktree isolation", section: "Workspace", keywords: ["branch", "git", "isolation"] },
  { name: "/automation", description: "Create or inspect thread automations", section: "Tools", keywords: ["schedule", "monitor", "wake"] },
  { name: "/status", description: "Show runtime status", section: "System", keywords: ["health", "agent"] },
  { name: "/usage", description: "Show token, context, and cost usage", section: "Context", keywords: ["tokens", "budget"] },
  { name: "/context", description: "Show context token budget", section: "Context", keywords: ["tokens", "window"] },
  { name: "/cost", description: "Show session cost breakdown", section: "Context", keywords: ["usage", "spend"] },
  { name: "/init", description: "Generate a CLAUDE.md for this project", section: "Project", keywords: ["instructions", "repo"] },
  { name: "/help", description: "Show slash command help", section: "System", keywords: ["commands", "shortcuts"] },
];

const SLASH_MENU_ORDER = FALLBACK_RUNTIME_SLASH_COMMANDS.map((item) => item.name);
const FALLBACK_MENU_DESCRIPTION = new Map(
  FALLBACK_RUNTIME_SLASH_COMMANDS.map((item) => [item.name, item.description]),
);
const FALLBACK_MENU_METADATA = new Map(
  FALLBACK_RUNTIME_SLASH_COMMANDS.map((item) => [item.name, item]),
);

const result = (sent: boolean, reset: RuntimeSlashCommandResult["reset"]): RuntimeSlashCommandResult => ({
  sent,
  reset,
});

export const buildRuntimeSlashMenuItems = (slashCommands: SlashCommand[]): RuntimeSlashMenuItem[] => {
  const rawItems = slashCommands.length > 0
    ? slashCommands.map((command) => {
        const name = command.label?.startsWith("/") ? command.label : `/${command.command}`;
        const fallback = FALLBACK_MENU_METADATA.get(name);
        return {
          name,
          description: command.description || fallback?.description || FALLBACK_MENU_DESCRIPTION.get(name) || "",
          section: fallback?.section ?? "Commands",
          keywords: fallback?.keywords,
        };
      })
    : FALLBACK_RUNTIME_SLASH_COMMANDS;

  return rawItems
    .filter((item, index, list) => list.findIndex((other) => other.name === item.name) === index)
    .sort((a, b) => {
      const ai = SLASH_MENU_ORDER.indexOf(a.name);
      const bi = SLASH_MENU_ORDER.indexOf(b.name);
      if (ai !== -1 || bi !== -1) return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
      return a.name.localeCompare(b.name);
    });
};

/**
 * Argument-stage menu: when the typed line is a local command that declares
 * structured args ("/effort", "/effort lo"), return the bare command followed
 * by its argument completions, filtered by the partial argument text.
 * Returns null when the active line is not in an argument stage.
 */
export const buildRuntimeSlashArgMenuItems = (
  slashFilter: string,
  slashCommands: SlashCommand[],
): RuntimeSlashMenuItem[] | null => {
  const match = slashFilter.match(/^\/([a-z][\w-]*)(\s+(\S*))?$/i);
  if (!match) return null;
  const commandName = match[1].toLowerCase();
  const hasSpace = match[2] !== undefined;
  const partialArg = (match[3] || "").toLowerCase();

  const command = slashCommands.find((item) =>
    item.command.toLowerCase() === commandName ||
    item.name.toLowerCase() === commandName ||
    item.label.toLowerCase() === `/${commandName}`,
  );
  if (!command || command.type !== "local" || !command.args?.length) return null;

  // Argument stage requires the command name to be fully typed: "/effort" or
  // "/effort lo" — a prefix like "/eff" stays in normal command filtering.
  const argItems = command.args
    .filter((arg) => !partialArg || arg.value.toLowerCase().startsWith(partialArg))
    .map((arg) => ({
      name: `/${command.command} ${arg.value}`,
      description: arg.description,
    }));
  if (argItems.length === 0) return null;

  if (hasSpace) return argItems;
  return [
    { name: `/${command.command}`, description: command.description },
    ...argItems,
  ];
};

export const buildRuntimeSlashPaletteItems = (
  slashCommands: SlashCommand[],
  options?: { exclude?: string[] },
): RuntimeSlashPaletteItem[] => {
  const excluded = new Set(
    (options?.exclude ?? []).map((command) => command.replace(/^\//, "").toLowerCase()),
  );
  return buildRuntimeSlashMenuItems(slashCommands)
    .filter((item) => !excluded.has(item.name.replace(/^\//, "").toLowerCase()))
    .map((item) => {
      const commandName = item.name.replace(/^\//, "");
      return {
        ...item,
        id: `slash.${commandName}`,
        commandLine: `/${commandName}`,
      };
    });
};

export const parseRuntimeSlashInput = (content: string): ParsedRuntimeSlashInput | null => {
  const slashMatch = content.trim().match(/^(\/[a-z][\w-]*)(?:\s+(.*))?$/is);
  if (!slashMatch) return null;

  const command = slashMatch[1].toLowerCase();
  const rawRest = (slashMatch[2] || "").trim();
  const mentions: FileContextRef[] = [];
  for (const match of rawRest.matchAll(/@(file|folder|url):([^\s]+)/g)) {
    const kind = match[1] as "file" | "folder" | "url";
    const path = match[2];
    const name = kind === "url" ? path : (path.split(/[/\\]/).pop() || path);
    mentions.push({ path, name, kind });
  }
  const rest = rawRest.replace(/@(file|folder|url):[^\s]+/g, "").trim();
  return {
    command,
    rest,
    commandLine: [command, rest].filter(Boolean).join(" "),
    mentions,
  };
};

export const isRuntimeSlashCommandInput = (content: string): boolean => {
  if (!content.startsWith("/") || content.startsWith("//")) return false;
  return /^\/[a-z][\w-]*(?:\s+.*)?$/i.test(content.trimEnd());
};

export const getRuntimeSlashCommandLine = (value: string): string | null => {
  const lines = value.split("\n");
  if (lines.length > 1 && lines.slice(0, -1).some((line) => line.trim().length > 0)) return null;
  const line = lines[lines.length - 1];
  if (line === "/") return line;
  if (!isRuntimeSlashCommandInput(line)) return null;
  return line;
};

export const getActiveRuntimeSlashCommand = (value: string): string | null => {
  const line = getRuntimeSlashCommandLine(value);
  if (!line || line === "/") return null;
  return line.match(/^(\/[a-z][\w-]*)/i)?.[1] ?? null;
};

export const shouldTokenizeRuntimeSlashCommand = (
  command: string,
  slashCommands: SlashCommand[],
): boolean => {
  const normalized = command.replace(/^\//, "").toLowerCase();
  const match = slashCommands.find((item) =>
    item.command.toLowerCase() === normalized ||
    item.name.toLowerCase() === normalized ||
    item.label.toLowerCase() === `/${normalized}`
  );
  if (match) return match.type === "template";
  return new Set(["review", "debug", "refactor", "test", "docs", "explain", "commit"]).has(normalized);
};

const isSkillsSlashLine = (commandLine: string): boolean => /^\/skills(?:\s|$)/i.test(commandLine);

export const syncRuntimeSlashPanelForDraft = (
  value: string,
  deps: RuntimeSlashPanelDraftDeps,
): boolean => {
  const slashCommandLine = getRuntimeSlashCommandLine(value);
  if (slashCommandLine && !deps.slashPanelOpen) {
    deps.openSlashPanel();
    deps.setMenuFilter(slashCommandLine);
    deps.sendClientCommand({ type: "skills.list" });
    if (isSkillsSlashLine(slashCommandLine)) {
      deps.sendClientCommand({ type: "skills.list" });
    }
    return true;
  }

  if (deps.slashPanelOpen) {
    if (slashCommandLine) {
      deps.setMenuFilter(slashCommandLine);
      if (isSkillsSlashLine(slashCommandLine)) {
        deps.sendClientCommand({ type: "skills.list" });
      }
      return true;
    }

    deps.closeSlashPanel();
    deps.setMenuFilter("");
  }

  return false;
};

const findRuntimeSkill = (skills: SkillInfo[] | undefined, query: string): SkillInfo | null => {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return null;
  const items = skills ?? [];
  return (
    items.find((skill) => skill.name.toLowerCase() === normalized) ??
    items.find((skill) => skill.name.toLowerCase().includes(normalized)) ??
    null
  );
};

const findExactRuntimeSkill = (skills: SkillInfo[] | undefined, query: string): SkillInfo | null => {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return null;
  return (skills ?? []).find((skill) => skill.name.toLowerCase() === normalized) ?? null;
};

export const resolveRuntimeSlashMenuSelection = (
  value: string,
  state: RuntimeSlashMenuSelectionState,
): RuntimeSlashMenuSelection => {
  if (!value) return { kind: "close" };

  const command = value.match(/^(\/[a-z][\w-]*)/i)?.[1] ?? value;
  const commandName = command.replace(/^\//, "").toLowerCase();
  const skill = findExactRuntimeSkill(state.availableSkills, commandName);
  if (skill) {
    return {
      kind: "skill",
      skill: {
        name: skill.name,
        description: skill.description,
        sourceLevel: skill.source_level,
      },
    };
  }

  if (shouldTokenizeRuntimeSlashCommand(command, state.slashCommands ?? [])) {
    return { kind: "tokenize", command };
  }

  return { kind: "execute", commandLine: value };
};

export const executeRuntimeSlashCommand = async (
  commandLine: string,
  deps: RuntimeSlashCommandDeps,
): Promise<RuntimeSlashCommandResult> => {
  const [cmdRaw, ...restParts] = commandLine.trim().split(/\s+/);
  const cmd = cmdRaw.toLowerCase();
  const rest = restParts.join(" ");

  if (cmd === "/plan") {
    const state = deps.getState();
    const enteringPlan = state.permissionMode !== "plan" || state.agentMode !== "plan";
    if (enteringPlan) {
      state.setAgentMode?.("plan");
      state.setPermissionMode?.("plan");
      state.upsertSystemMessage?.(
        "system-plan-mode-status",
        "Plan mode enabled. The agent will inspect and propose a plan before making changes.",
        { replacePrefix: "Plan mode" },
      );
    } else if (!rest) {
      state.upsertSystemMessage?.(
        "system-plan-mode-status",
        "Already in plan mode.",
        { replacePrefix: "Plan mode" },
      );
    }
    if (rest && rest.toLowerCase() !== "open") {
      const sent = await deps.sendUserMessage?.(rest);
      return result(Boolean(sent), sent ? "composer" : "none");
    }
    return result(true, "input");
  }

  const skill = findRuntimeSkill(deps.getState().availableSkills, cmd.replace(/^\//, ""));
  if (skill) {
    deps.getState().addSelectedSkill?.({
      name: skill.name,
      description: skill.description,
      sourceLevel: skill.source_level,
    });
    deps.sendClientCommand({ type: "load_skill", skill_name: skill.name });
    if (rest) {
      const sent = await deps.sendUserMessage?.(rest);
      return result(Boolean(sent), sent ? "composer" : "none");
    }
    return result(true, "input");
  }

  if (cmd === "/clear") {
    const ok = await deps.confirmClear();
    if (!ok) return result(false, "none");
    const state = deps.getState();
    if (state.conversationId) {
      deps.sendClientCommand({ type: "conversation.clear", conversation_id: state.conversationId });
      state.hydrateConversationMessages?.(state.conversationId, [], { activate: true, isStreaming: false });
    } else {
      deps.setState({ messages: [], isStreaming: false });
    }
    return result(true, "input");
  }

  if (cmd === "/skills" && !rest) {
    const state = deps.getState();
    if (!state.skillsMarketplaceOpen) state.toggleSkillsMarketplace?.();
    deps.sendClientCommand({ type: "skills.list" });
    deps.sendClientCommand({ type: "skills.marketplace.list" });
    return result(true, "input");
  }

  if ((cmd === "/automation" || cmd === "/automations") && !rest) {
    const state = deps.getState();
    if (!state.automationsOpen) state.toggleAutomations?.();
    deps.sendClientCommand({ type: "scheduler.list" });
    return result(true, "input");
  }

  if (cmd === "/compact") {
    deps.getState().upsertSystemMessage?.(
      "system-compact-status",
      "Compacting context...",
      { replacePrefix: "Context compact" },
    );
  }

  const sent = await deps.sendChatMessage({
    displayContent: commandLine,
    backendContent: commandLine,
    skipLocalAppend: true,
  });
  return result(sent, sent ? "input" : "none");
};
