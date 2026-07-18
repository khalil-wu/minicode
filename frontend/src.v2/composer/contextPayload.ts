import { fsListTree, fsReadFileInfo, isDesktop } from "../desktop/runtime";
import { apiBase, authHeaders } from "../protocol/api";
import { uploadAttachment } from "../protocol/api";
import { listWorkspaceTree, readWorkspaceFile } from "../protocol/workspace";
import type { FileContextRef, MessageAttachmentRef, MessageContextRef } from "../stores/types";

const MAX_FILE_CONTEXT_CHARS = 40_000;
const MAX_FOLDER_ENTRIES = 80;
const MAX_NATIVE_CONTEXT_ATTACHMENTS = 8;
const MAX_NATIVE_IMAGE_BYTES = 20 * 1024 * 1024;
const MAX_NATIVE_PDF_BYTES = 50 * 1024 * 1024;

interface NativeContextAttachments {
  attachments: Record<string, unknown>[];
  attachmentRefs: MessageAttachmentRef[];
  notes: string;
}

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

export const buildContextNativeAttachments = async (
  refs: MessageContextRef[],
  sessionId?: string,
): Promise<NativeContextAttachments> => {
  if (!sessionId) return emptyNativeContextAttachments();

  const candidates = await collectNativeContextCandidates(refs);
  if (candidates.length === 0) return emptyNativeContextAttachments();

  const attachments: Record<string, unknown>[] = [];
  const attachmentRefs: MessageAttachmentRef[] = [];
  const notes: string[] = [];
  const seen = new Set<string>();

  for (const candidate of candidates) {
    if (attachments.length >= MAX_NATIVE_CONTEXT_ATTACHMENTS) {
      notes.push(`Native context attachments capped at ${MAX_NATIVE_CONTEXT_ATTACHMENTS}; remaining files stay available through workspace tools.`);
      break;
    }
    const key = normalizePathKey(candidate.path);
    if (seen.has(key)) continue;
    seen.add(key);

    const limit = candidate.mediaType === "application/pdf" ? MAX_NATIVE_PDF_BYTES : MAX_NATIVE_IMAGE_BYTES;
    if (candidate.sizeBytes != null && candidate.sizeBytes > limit) {
      notes.push(`Native attachment skipped: ${candidate.path} is ${formatBytes(candidate.sizeBytes)}, above ${formatBytes(limit)}.`);
      continue;
    }

    try {
      const blob = await fetchWorkspaceBlob(candidate.path);
      if (blob.size > limit) {
        notes.push(`Native attachment skipped: ${candidate.path} is ${formatBytes(blob.size)}, above ${formatBytes(limit)}.`);
        continue;
      }
      const file = new File([blob], candidate.name || basename(candidate.path), {
        type: blob.type || candidate.mediaType,
      });
      const result = await uploadAttachment(sessionId, file);
      attachments.push(result.attachment);
      attachmentRefs.push({
        id: String(result.attachment.id || result.artifact_id),
        name: String(result.attachment.file_name || candidate.name || basename(candidate.path)),
        kind: candidate.mediaType.startsWith("image/") ? "image" : "document",
        mediaType: String(result.attachment.media_type || candidate.mediaType),
        sizeBytes: Number(result.attachment.size_bytes || blob.size || 0),
        artifactId: String(result.attachment.artifact_id || result.artifact_id || ""),
        docId: String(result.attachment.doc_id || result.doc_id || ""),
        indexedChunks: Number(result.attachment.indexed_chunks ?? result.indexed_chunks ?? 0),
      });
    } catch {
      notes.push(`Native attachment unavailable: ${candidate.path}`);
    }
  }

  return {
    attachments,
    attachmentRefs,
    notes: notes.length ? `Native context notes:\n${notes.map((note) => `- ${note}`).join("\n")}` : "",
  };
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
  if (!anchor && isNativeContextPath(path)) {
    return `Native file: ${path}\nAttached as native model input when supported.`;
  }
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

interface NativeContextCandidate {
  path: string;
  name: string;
  mediaType: string;
  sizeBytes?: number | null;
}

const emptyNativeContextAttachments = (): NativeContextAttachments => ({
  attachments: [],
  attachmentRefs: [],
  notes: "",
});

const collectNativeContextCandidates = async (refs: MessageContextRef[]): Promise<NativeContextCandidate[]> => {
  const candidates: NativeContextCandidate[] = [];
  for (const ref of refs) {
    if (ref.kind === "file") {
      const { path, anchor } = splitAnchor(ref.path);
      if (!anchor && isNativeContextPath(path)) {
        candidates.push({
          path,
          name: ref.name || basename(path),
          mediaType: mediaTypeForPath(path),
        });
      }
      continue;
    }
    if (ref.kind === "folder") {
      candidates.push(...await nativeFolderCandidates(ref.path));
    }
  }
  return candidates;
};

const nativeFolderCandidates = async (path: string): Promise<NativeContextCandidate[]> => {
  const entries = isDesktop()
    ? (await fsListTree(path)).map((entry) => ({
        name: entry.name,
        path: entry.path,
        isDirectory: entry.isDirectory,
        sizeBytes: entry.sizeBytes,
      }))
    : (await listWorkspaceTree(path))?.children?.map((entry) => ({
        name: entry.name,
        path: entry.path,
        isDirectory: entry.is_dir,
        sizeBytes: entry.size_bytes,
      })) ?? [];

  return entries
    .filter((entry) => !entry.isDirectory && isNativeContextPath(entry.path || entry.name))
    .map((entry) => ({
      path: entry.path || entry.name,
      name: entry.name || basename(entry.path),
      mediaType: mediaTypeForPath(entry.path || entry.name),
      sizeBytes: entry.sizeBytes,
    }));
};

const fetchWorkspaceBlob = async (path: string): Promise<Blob> => {
  const url = `${apiBase()}/api/workspace/raw?path=${encodeURIComponent(path)}`;
  const response = await fetch(url, { headers: authHeaders() });
  if (!response.ok) {
    throw new Error(`Workspace raw request failed (${response.status}).`);
  }
  return response.blob();
};

const isNativeContextPath = (path: string): boolean => {
  const mediaType = mediaTypeForPath(path);
  return mediaType.startsWith("image/") || mediaType === "application/pdf";
};

const mediaTypeForPath = (path: string): string => {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  if (ext === "pdf") return "application/pdf";
  if (ext === "png") return "image/png";
  if (ext === "jpg" || ext === "jpeg") return "image/jpeg";
  if (ext === "gif") return "image/gif";
  if (ext === "webp") return "image/webp";
  if (ext === "bmp") return "image/bmp";
  return "application/octet-stream";
};

const basename = (path: string): string =>
  path.split(/[/\\]/).filter(Boolean).pop() || path;

const normalizePathKey = (path: string): string =>
  path.replace(/\\/g, "/").replace(/\/+/g, "/").toLowerCase();

const formatBytes = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
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
