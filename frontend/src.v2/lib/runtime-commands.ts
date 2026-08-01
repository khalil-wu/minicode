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

const result = (sent: boolean, reset: RuntimeSlashCommandResult["reset"]): RuntimeSlashCommandResult => ({
  sent,
  reset,
});

export const buildRuntimeSlashMenuItems = (slashCommands: SlashCommand[]): RuntimeSlashMenuItem[] => {
  const seen = new Set<string>();
  return slashCommands.flatMap((command) => {
    const name = command.label?.startsWith("/") ? command.label : `/${command.command}`;
    if (seen.has(name)) return [];
    seen.add(name);
    return [{
      name,
      description: command.description || "",
      section: "Commands" as const,
    }];
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
  return match?.type === "template";
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
    if (isSkillsSlashLine(slashCommandLine)) {
      deps.sendClientCommand({ type: "skills.list" });
    }
    return true;
  }

  if (deps.slashPanelOpen) {
    if (slashCommandLine) {
      deps.setMenuFilter(slashCommandLine);
      return true;
    }

    deps.closeSlashPanel();
    deps.setMenuFilter("");
  }

  return false;
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

  const encodedSkillPath = value.match(/^skill-path:(.+)$/)?.[1];
  const encodedSkillName = value.match(/^skill-name:(.+)$/)?.[1];
  if (encodedSkillPath || encodedSkillName) {
    const skillPath = encodedSkillPath ? decodeURIComponent(encodedSkillPath) : "";
    const skillName = encodedSkillName ? decodeURIComponent(encodedSkillName) : "";
    const selected = (state.availableSkills ?? []).find((skill) => (
      skillPath ? skill.path === skillPath : skill.name === skillName
    ));
    if (selected) {
      return {
        kind: "skill",
        skill: {
          name: selected.name,
          path: selected.path,
          description: selected.description,
          sourceLevel: selected.source_level,
        },
      };
    }
  }

  const command = value.match(/^(\/[a-z][\w-]*)/i)?.[1] ?? value;
  const commandName = command.replace(/^\//, "").toLowerCase();
  const skill = findExactRuntimeSkill(state.availableSkills, commandName);
  if (skill) {
    return {
      kind: "skill",
      skill: {
        name: skill.name,
        path: skill.path,
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
  const sent = await deps.sendChatMessage({
    displayContent: commandLine,
    backendContent: commandLine,
    skipLocalAppend: true,
  });
  return result(sent, sent ? "input" : "none");
};
