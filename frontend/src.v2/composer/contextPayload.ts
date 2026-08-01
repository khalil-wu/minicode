import { fsListTree, isDesktop } from "../desktop/runtime";
import { apiBase, authHeaders } from "../protocol/api";
import { uploadAttachment } from "../protocol/api";
import { listWorkspaceTree } from "../protocol/workspace";
import type { MessageAttachmentRef, MessageContextRef } from "../stores/types";

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

export const buildContextNativeAttachments = async (
  refs: MessageContextRef[],
  sessionId?: string,
  workspaceRoot = "",
): Promise<NativeContextAttachments> => {
  if (!sessionId) return emptyNativeContextAttachments();

  const candidates = await collectNativeContextCandidates(refs, workspaceRoot);
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
      const blob = await fetchWorkspaceBlob(candidate.path, workspaceRoot);
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
    if (ref.kind === "skill" || ref.kind === "plugin") return "";
    if (ref.kind === "url") {
      return `URL context: ${ref.path}`;
    }
    if (ref.kind === "browser_annotation") {
      return [
        `Browser annotation: ${ref.url}`,
        ref.selector ? `Target: ${ref.selector}` : "",
        ref.xPercent != null && ref.yPercent != null
          ? `Viewport target: ${(ref.xPercent * 100).toFixed(1)}%, ${(ref.yPercent * 100).toFixed(1)}%${ref.widthPercent != null && ref.heightPercent != null ? `; size ${(ref.widthPercent * 100).toFixed(1)}% x ${(ref.heightPercent * 100).toFixed(1)}%` : ""}${ref.viewportWidth && ref.viewportHeight ? ` in ${ref.viewportWidth}x${ref.viewportHeight}` : ""}`
          : "",
        `Comment: ${ref.note}`,
      ].filter(Boolean).join("\n");
    }
    if (ref.kind === "folder") return `Directory reference: ${ref.path}`;
    return `File reference: ${ref.path}`;
  } catch {
    if (ref.kind === "skill" || ref.kind === "plugin") return "";
    return `Context unavailable: @${ref.kind}:${ref.path}`;
  }
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

const collectNativeContextCandidates = async (
  refs: MessageContextRef[],
  workspaceRoot: string,
): Promise<NativeContextCandidate[]> => {
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
      candidates.push(...await nativeFolderCandidates(ref.path, workspaceRoot));
    }
  }
  return candidates;
};

const nativeFolderCandidates = async (path: string, workspaceRoot: string): Promise<NativeContextCandidate[]> => {
  const entries = isDesktop()
    ? (await fsListTree(path)).map((entry) => ({
        name: entry.name,
        path: entry.path,
        isDirectory: entry.isDirectory,
        sizeBytes: entry.sizeBytes,
      }))
    : (await listWorkspaceTree(workspaceRoot, path))?.children?.map((entry) => ({
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

const fetchWorkspaceBlob = async (path: string, workspaceRoot: string): Promise<Blob> => {
  const params = new URLSearchParams({ path });
  if (workspaceRoot.trim()) params.set("workspace_root", workspaceRoot.trim());
  const url = `${apiBase()}/api/workspace/raw?${params.toString()}`;
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
