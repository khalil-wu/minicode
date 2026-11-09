import type { FileContextRef, PluginContextRef, SkillContextRef, SkillInfo } from "../stores/types";
import type { ComposerPlugin } from "./useComposerPlugins";
import type { ComposerSelection } from "./inputSelection";
import { workspaceFilePathComparisonKey } from "../lib/workspace-path";

export type InlineComposerReference = FileContextRef | PluginContextRef | SkillContextRef;
export interface ComposerReferenceInsertion { reference: InlineComposerReference; selection: ComposerSelection }

export const composerReferenceKey = (reference: InlineComposerReference) => `${reference.kind}:${reference.kind === "file" || reference.kind === "folder" || reference.kind === "skill"
  ? workspaceFilePathComparisonKey(reference.path ?? reference.name, "") : reference.path}`;

export const composerPluginReference = (plugin: ComposerPlugin, labels?: { computerUse: string }): PluginContextRef => ({
  kind: "plugin", name: plugin.id === "computer-use@minicode-bundled" && plugin.marketplace === "minicode-bundled" && labels
    ? labels.computerUse : plugin.displayName || plugin.name, configName: plugin.id, path: `plugin://${plugin.id}`,
  description: plugin.shortDescription || plugin.description, pluginPath: plugin.path,
  iconUrl: plugin.path && plugin.iconVariant ? undefined : plugin.iconUrl, iconVariant: plugin.iconVariant, iconVariants: plugin.iconVariants,
});

export const composerSkillReference = (skill: SkillInfo): SkillContextRef => ({
  kind: "skill", name: skill.name, path: skill.path, description: skill.description, sourceLevel: skill.source_level,
  displayName: skill.display_name, icon: skill.path ? undefined : skill.icon, iconVariant: skill.path && skill.icon ? "small" : undefined,
});

export function composerReferenceLabel(reference: InlineComposerReference): string {
  if (reference.kind === "skill") return reference.displayName || reference.name;
  if (reference.kind !== "file" || !reference.range) return reference.name;
  const { startLineNumber, endLineNumber, endColumn } = reference.range;
  const end = endLineNumber > startLineNumber && endColumn === 1 ? endLineNumber - 1 : endLineNumber;
  return `${reference.name}:${startLineNumber}${end === startLineNumber ? "" : `–${end}`}`;
}

export function conversationIdFromReference(path: string): string | null {
  const codex = /^codex(?:-dev)?:\/\/threads\/([^/?#]+)/.exec(path);
  if (codex) return decodeURIComponent(codex[1]);
  if (path.startsWith("minicode://conversation?")) return new URL(path).searchParams.get("conversation_id");
  return null;
}

export function composerReferenceTextParts(text: string, references: InlineComposerReference[]): Array<string | InlineComposerReference> {
  const parts: Array<string | InlineComposerReference> = [];
  const tokens = references.map(reference => ({ reference, markdown: composerReferenceMarkdown(reference) }));
  let offset = 0;
  while (offset < text.length) {
    const matches = tokens.map(item => ({ ...item, index: text.indexOf(item.markdown, offset) }))
      .filter(item => item.index >= 0).sort((left, right) => left.index - right.index);
    const match = matches[0];
    if (!match) { parts.push(text.slice(offset)); break; }
    if (match.index > offset) parts.push(text.slice(offset, match.index));
    parts.push(match.reference);
    offset = match.index + match.markdown.length;
  }
  return parts;
}

export function composerReferenceMarkdown(reference: InlineComposerReference): string {
  const label = reference.kind === "skill" ? `$${reference.name}` : reference.kind === "plugin" ? `@${reference.name}` : composerReferenceLabel(reference);
  const originalPath = reference.kind === "folder" && !/[\\/]$/.test(reference.path) ? `${reference.path}/` : reference.path ?? "";
  const path = reference.kind === "file" || reference.kind === "folder" || reference.kind === "skill" ? originalPath.replaceAll("\\", "/") : originalPath;
  return `[${label.replace(/[\\[\]]/g, "\\$&")}](${/[\s<>]/.test(path) ? `<${path}>` : path.replace(/[\\()]/g, "\\$&")})`;
}

export function composerReferenceFromLink(label: string, path: string): InlineComposerReference | null {
  if (path.startsWith("plugin://")) return { kind: "plugin", name: label.replace(/^@/, ""), configName: path.slice("plugin://".length), path };
  if (label.startsWith("$")) return { kind: "skill", name: label.slice(1), path: path || undefined };
  if (conversationIdFromReference(path)) return { kind: "url", name: label, path };
  if (/^(?:file:|[a-z]:[/\\]|\.{0,2}[/\\])/i.test(path) || (path && !path.startsWith("#") && !/^[a-z][a-z\d+.-]*:/i.test(path))) {
    const filePath = path.startsWith("file://") ? decodeURIComponent(new URL(path).pathname).replace(/^\/([a-z]:)/i, "$1") : path;
    return { kind: /[/\\]$/.test(filePath) ? "folder" : "file", name: label, path: filePath };
  }
  return null;
}
