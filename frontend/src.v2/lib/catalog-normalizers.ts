import type { SkillInfo, SlashCommand, SlashCommandArg } from "../stores/types";

const asString = (value: unknown): string => (typeof value === "string" ? value.trim() : "");

/**
 * Normalize the runtime.capabilities skills catalog into SkillInfo entries.
 * Backend-provided extra fields (display_name, allow_implicit_invocation, …)
 * are preserved for the composer, unknown/nameless entries are dropped.
 */
export function normalizeSkillList(items: unknown): SkillInfo[] {
  if (!Array.isArray(items)) return [];
  const result: SkillInfo[] = [];
  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    const raw = item as Record<string, unknown>;
    const name = asString(raw.name);
    if (!name) continue;
    result.push({
      ...raw,
      name,
      description: asString(raw.description),
    } as SkillInfo);
  }
  return result;
}

const normalizeArgs = (value: unknown): SlashCommandArg[] | undefined => {
  if (!Array.isArray(value)) return undefined;
  const args = value
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
    .map((item) => ({
      value: asString(item.value),
      description: asString(item.description),
    }))
    .filter((item) => item.value);
  return args.length ? args : undefined;
};

/**
 * Normalize runtime.capabilities composer_commands into SlashCommand entries.
 * Entries explicitly disabled (`enabled: false`) or without a name are dropped.
 */
export function normalizeSlashCommands(items: unknown): SlashCommand[] {
  if (!Array.isArray(items)) return [];
  const result: SlashCommand[] = [];
  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    const raw = item as Record<string, unknown>;
    if (raw.enabled === false) continue;
    const name = asString(raw.name) || asString(raw.command);
    if (!name) continue;
    const command = asString(raw.command) || name;
    const type = asString(raw.type);
    const entry: SlashCommand = {
      name,
      command,
      label: asString(raw.label) || `/${command}`,
      description: asString(raw.description),
      type: type === "template" || type === "protocol" ? type : "local",
    };
    const args = normalizeArgs(raw.args);
    if (args) entry.args = args;
    result.push(entry);
  }
  return result;
}
