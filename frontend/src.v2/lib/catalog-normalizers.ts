import type { SkillInfo, SlashCommand } from "../stores/types";

const stringValue = (value: unknown): string => (typeof value === "string" ? value.trim() : "");
const numberValue = (value: unknown): number | undefined => {
  const num = typeof value === "number" ? value : Number(value);
  return Number.isFinite(num) ? num : undefined;
};

const stringArray = (value: unknown): string[] | undefined => {
  if (!Array.isArray(value)) return undefined;
  const items = value.map(stringValue).filter(Boolean);
  return items.length > 0 ? items : [];
};

const skillUsage = (value: unknown): SkillInfo["usage"] | undefined => {
  if (!value || typeof value !== "object") return undefined;
  const payload = value as Record<string, unknown>;
  const usage = {
    load_count: numberValue(payload.load_count),
    reuse_count: numberValue(payload.reuse_count),
    failure_count: numberValue(payload.failure_count),
    unload_count: numberValue(payload.unload_count),
    last_event: stringValue(payload.last_event) || undefined,
    last_invoked_at: stringValue(payload.last_invoked_at) || undefined,
  };
  return Object.values(usage).some((item) => item !== undefined) ? usage : undefined;
};

export const normalizeSkillInfo = (skill: unknown): SkillInfo | null => {
  if (!skill || typeof skill !== "object") return null;
  const payload = skill as Record<string, unknown>;
  const name = stringValue(payload.name);
  if (!name) return null;
  return {
    name,
    description: stringValue(payload.description),
    display_name: stringValue(payload.display_name) || undefined,
    icon: stringValue(payload.icon) || undefined,
    version: stringValue(payload.version) || undefined,
    triggers: stringArray(payload.triggers),
    tools_required: stringArray(payload.tools_required),
    mcp_required: stringArray(payload.mcp_required),
    mcp_dependencies: stringArray(payload.mcp_dependencies),
    allow_implicit_invocation: typeof payload.allow_implicit_invocation === "boolean"
      ? payload.allow_implicit_invocation
      : undefined,
    default_prompt: stringValue(payload.default_prompt) || undefined,
    source_level: stringValue(payload.source_level ?? payload.level) || undefined,
    active: Boolean(payload.active),
    usage: skillUsage(payload.usage),
  };
};

export const normalizeSkillList = (skills: unknown): SkillInfo[] => (
  Array.isArray(skills)
    ? skills.map(normalizeSkillInfo).filter((skill): skill is SkillInfo => Boolean(skill))
    : []
);

const commandType = (value: unknown): SlashCommand["type"] => {
  const raw = stringValue(value);
  if (raw === "template" || raw === "protocol" || raw === "local-jsx") return raw;
  return "local";
};

const commandArgs = (value: unknown): SlashCommand["args"] => {
  if (!Array.isArray(value)) return undefined;
  const args = value
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
    .map((item) => ({
      value: stringValue(item.value),
      description: stringValue(item.description),
    }))
    .filter((item) => item.value);
  return args.length > 0 ? args : undefined;
};

export const normalizeSlashCommands = (commands: unknown): SlashCommand[] => {
  if (!Array.isArray(commands)) return [];
  return commands
    .filter((command): command is Record<string, unknown> => Boolean(command && typeof command === "object"))
    .filter((command) => command.enabled !== false)
    .map((command) => {
      const rawName = stringValue(command.name) || stringValue(command.command) || stringValue(command.label);
      const commandName = stringValue(command.command) || rawName.replace(/^\//, "");
      const label = stringValue(command.label) || (commandName ? `/${commandName}` : rawName);
      return {
        name: rawName || commandName || label,
        command: commandName || rawName.replace(/^\//, ""),
        label,
        description: stringValue(command.description),
        type: commandType(command.type),
        panel: stringValue(command.panel) || undefined,
        args: commandArgs(command.args),
      };
    })
    .filter((command) => command.name && command.command);
};
