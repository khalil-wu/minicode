import type { SkillInfo, SlashCommand } from "../stores/types";
import { skillAssetResourceUrlWithToken } from "../protocol/api";

const stringValue = (value: unknown): string => (typeof value === "string" ? value.trim() : "");
const stringArray = (value: unknown): string[] | undefined => {
  if (!Array.isArray(value)) return undefined;
  const items = value.map(stringValue).filter(Boolean);
  return items.length > 0 ? items : [];
};

export const normalizeSkillInfo = (skill: unknown): SkillInfo | null => {
  if (!skill || typeof skill !== "object") return null;
  const payload = skill as Record<string, unknown>;
  const name = stringValue(payload.name);
  if (!name) return null;
  const path = stringValue(payload.path);
  const icon = stringValue(payload.icon);
  const iconLarge = stringValue(payload.icon_large);
  return {
    name,
    description: stringValue(payload.description),
    path: path || undefined,
    display_name: stringValue(payload.display_name) || undefined,
    short_description: stringValue(payload.short_description) || undefined,
    icon: icon && path ? skillAssetResourceUrlWithToken(path, "small") : undefined,
    icon_large: iconLarge && path ? skillAssetResourceUrlWithToken(path, "large") : undefined,
    brand_color: stringValue(payload.brand_color) || undefined,
    version: stringValue(payload.version) || undefined,
    mcp_dependencies: stringArray(payload.mcp_dependencies),
    allow_implicit_invocation: typeof payload.allow_implicit_invocation === "boolean"
      ? payload.allow_implicit_invocation
      : undefined,
    user_invocable: typeof payload.user_invocable === "boolean"
      ? payload.user_invocable
      : undefined,
    default_prompt: stringValue(payload.default_prompt) || undefined,
    source_level: stringValue(payload.source_level ?? payload.level) || undefined,
    active: Boolean(payload.active),
  };
};

export const normalizeSkillList = (skills: unknown): SkillInfo[] => (
  Array.isArray(skills)
    ? skills.map(normalizeSkillInfo).filter((skill): skill is SkillInfo => Boolean(skill))
    : []
);

const commandType = (value: unknown): SlashCommand["type"] => {
  const raw = stringValue(value);
  if (raw === "template" || raw === "protocol") return raw;
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

const commandAvailability = (value: unknown): SlashCommand["availability"] => {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const payload = value as Record<string, unknown>;
  const kind = stringValue(payload.kind);
  const scope = stringValue(payload.scope);
  if (!kind || !scope) return undefined;
  return {
    kind,
    scope,
    reason: stringValue(payload.reason) || undefined,
  };
};

export const normalizeSlashCommands = (commands: unknown): SlashCommand[] => {
  if (!Array.isArray(commands)) return [];
  const seen = new Set<string>();
  return commands
    .filter((command): command is Record<string, unknown> => Boolean(command && typeof command === "object"))
    .filter((command) => command.enabled !== false)
    .map((command) => {
      const rawName = stringValue(command.name) || stringValue(command.command) || stringValue(command.label);
      const commandName = stringValue(command.command) || rawName.replace(/^\//, "");
      const label = stringValue(command.label) || (commandName ? `/${commandName}` : rawName);
      return {
        id: stringValue(command.id) || undefined,
        name: rawName || commandName || label,
        command: commandName || rawName.replace(/^\//, ""),
        label,
        description: stringValue(command.description),
        type: commandType(command.type),
        kind: stringValue(command.kind) || undefined,
        source: stringValue(command.source) || undefined,
        availability: commandAvailability(command.availability),
        panel: stringValue(command.panel) || undefined,
        args: commandArgs(command.args),
        extensionPath: stringValue(command.extension_path) || undefined,
        sourcePath: stringValue(command.source_path) || undefined,
        template: typeof command.template === "string" ? command.template : undefined,
        searchText: stringValue(command.search_text) || undefined,
        argumentHint: stringValue(command.argument_hint) || undefined,
        argumentNames: stringArray(command.argument_names),
        baseDir: stringValue(command.base_dir) || undefined,
        isSkillFile: typeof command.is_skill_file === "boolean" ? command.is_skill_file : undefined,
      };
    })
    .filter((command) => command.name && command.command)
    // Extension and project commands intentionally precede builtins. Preserve
    // that executable precedence while preventing duplicate palette entries.
    .filter((command) => {
      const key = command.command.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
};
