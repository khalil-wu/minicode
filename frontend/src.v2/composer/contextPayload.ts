import { fsListTree, fsReadFileInfo, isDesktop } from "../desktop/runtime";
import { listWorkspaceTree, readWorkspaceFile } from "../protocol/workspace";
import type { FileContextRef, MessageContextRef } from "../stores/types";

const MAX_FILE_CONTEXT_CHARS = 40_000;
const MAX_FOLDER_ENTRIES = 80;

export const buildContextPayload = async (refs: MessageContextRef[]): Promise<string> => {
  if (refs.length === 0) return "";
  const blocks = await Promise.all(refs.map(contextBlockForRef));
  return blocks.filter(Boolean).join("\n\n");
};

export const buildContextFallback = (refs: MessageContextRef[]): string => {
  const lines: string[] = [];
  const fileRefs = refs.filter((ref): ref is FileContextRef => ref.kind === "file" || ref.kind === "folder" || ref.kind === "url");
  const skillRefs = refs.filter((ref) => ref.kind === "skill");
  if (fileRefs.length > 0) {
    lines.push("Context references:");
    lines.push(...fileRefs.map((item) => `- @${item.kind}:${item.path}`));
  }
  if (skillRefs.length > 0) {
    if (lines.length > 0) lines.push("");
    lines.push("Requested skills:");
    lines.push(...skillRefs.map((skill) => `- ${skill.name}${skill.description ? `: ${skill.description}` : ""}`));
  }
  return lines.join("\n");
};

const contextBlockForRef = async (ref: MessageContextRef): Promise<string> => {
  try {
    if (ref.kind === "skill") {
      return [
        `Skill requested: ${ref.name}`,
        ref.description ? `Description: ${ref.description}` : "",
        ref.sourceLevel ? `Source: ${ref.sourceLevel}` : "",
      ].filter(Boolean).join("\n");
    }
    if (ref.kind === "url") {
      return `URL context: ${ref.path}`;
    }
    if (ref.kind === "folder") {
      return folderContext(ref.path);
    }
    return fileContext(ref.path);
  } catch {
    if (ref.kind === "skill") return `Skill requested: ${ref.name}`;
    return `Context unavailable: @${ref.kind}:${ref.path}`;
  }
};

const fileContext = async (pathWithAnchor: string): Promise<string> => {
  const { path, anchor } = splitAnchor(pathWithAnchor);
  const file = isDesktop()
    ? await fsReadFileInfo(path)
    : await readWorkspaceFile(path);
  if (!file?.content) {
    return `File context unavailable: ${pathWithAnchor}`;
  }
  const content = anchor ? sliceAnchoredContent(file.content, anchor) : file.content;
  const clipped = clipText(content, MAX_FILE_CONTEXT_CHARS);
  const label = anchor ? `${path}#${anchor}` : path;
  return `File: ${label}\n\`\`\`\n${clipped}\n\`\`\``;
};

const folderContext = async (path: string): Promise<string> => {
  const entries = isDesktop()
    ? (await fsListTree(path)).map((entry) => ({
        name: entry.name,
        path: entry.path,
        isDirectory: entry.isDirectory,
      }))
    : (await listWorkspaceTree(path))?.children?.map((entry) => ({
        name: entry.name,
        path: entry.path,
        isDirectory: entry.is_dir,
      })) ?? [];
  if (entries.length === 0) return `Folder context: ${path}\n(no entries found)`;
  const lines = entries
    .slice(0, MAX_FOLDER_ENTRIES)
    .map((entry) => `- ${entry.isDirectory ? "dir " : "file"} ${entry.path || entry.name}`);
  if (entries.length > MAX_FOLDER_ENTRIES) {
    lines.push(`- ... ${entries.length - MAX_FOLDER_ENTRIES} more`);
  }
  return `Folder: ${path}\n${lines.join("\n")}`;
};

const splitAnchor = (value: string): { path: string; anchor: string } => {
  const index = value.lastIndexOf("#");
  if (index < 0) return { path: value, anchor: "" };
  return { path: value.slice(0, index), anchor: value.slice(index + 1) };
};

const sliceAnchoredContent = (content: string, anchor: string): string => {
  const match = anchor.match(/^L?(\d+)(?:-(\d+))?$/i);
  if (!match) return content;
  const start = Math.max(1, Number(match[1]));
  const end = Math.max(start, Number(match[2] ?? match[1]));
  return content.split(/\r?\n/).slice(start - 1, end).join("\n");
};

const clipText = (value: string, maxChars: number): string => {
  if (value.length <= maxChars) return value;
  return `${value.slice(0, maxChars)}\n\n[truncated ${value.length - maxChars} chars]`;
};
