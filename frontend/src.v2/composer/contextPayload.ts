import { fsListTree, isDesktop } from "../desktop/runtime";
import { apiBase, authHeaders, fetchWithTimeout } from "../protocol/api";
import { uploadAttachment } from "../protocol/api";
import { listWorkspaceTree } from "../protocol/workspace";
import type { MessageAttachmentRef, MessageContextRef } from "../stores/types";
import { MAX_IMAGE_SOURCE_BYTES, prepareNativeImageFile } from "./imagePreparation";
import { workspaceFilePathComparisonKey } from "../lib/workspace-path";
import { mediaTypeForPath } from "../lib/media-types";
import { formatBytes } from "../lib/format-bytes";

// Match MiniCode's provider-facing media envelope. Anthropic's 5 MiB
// encoded-image cap leaves 3.75 MiB for raw bytes after base64 expansion; PDFs
// stay below 20 MiB so the full request remains under 32 MiB. The API accepts
// at most 100 combined images/PDFs per request.
const MAX_NATIVE_CONTEXT_ATTACHMENTS = 100;
const MAX_NATIVE_PDF_BYTES = 20 * 1024 * 1024;

interface NativeContextAttachments {
  attachments: Record<string, unknown>[];
  attachmentRefs: MessageAttachmentRef[];
  notes: string;
  /**
   * The conversation that owns the uploaded native attachments.  A blank
   * composer has no conversation yet; the first successful upload creates one
   * and every subsequent upload in this batch must use it.
   */
  conversationId?: string;
}

export const buildContextPayload = async (refs: MessageContextRef[]): Promise<string> => {
  if (refs.length === 0) return "";
  const blocks = await Promise.all(refs.map(contextBlockForRef));
  return blocks.filter(Boolean).join("\n\n");
};

export const buildContextNativeAttachments = async (
  refs: MessageContextRef[],
  sessionId?: string,
  conversationId = "",
  workspaceRoot = "",
): Promise<NativeContextAttachments> => {
  const initialOwner = conversationId.trim();
  if (!sessionId) return emptyNativeContextAttachments(initialOwner);

  const candidates = await collectNativeContextCandidates(refs, workspaceRoot);
  if (candidates.length === 0) return emptyNativeContextAttachments(initialOwner);

  const attachments: Record<string, unknown>[] = [];
  const attachmentRefs: MessageAttachmentRef[] = [];
  const notes: string[] = [];
  const seen = new Set<string>();
  let ownerConversationId = initialOwner;

  for (const candidate of candidates) {
    if (attachments.length >= MAX_NATIVE_CONTEXT_ATTACHMENTS) {
      notes.push(`Native context attachments capped at ${MAX_NATIVE_CONTEXT_ATTACHMENTS}; remaining files stay available through workspace tools.`);
      break;
    }
    const key = normalizePathKey(candidate.path, workspaceRoot);
    if (seen.has(key)) continue;
    seen.add(key);

    const sourceLimit = candidate.mediaType === "application/pdf"
      ? MAX_NATIVE_PDF_BYTES
      : MAX_IMAGE_SOURCE_BYTES;
    if (candidate.sizeBytes != null && candidate.sizeBytes > sourceLimit) {
      notes.push(`Native attachment skipped: ${candidate.path} is ${formatBytes(candidate.sizeBytes)}, above ${formatBytes(sourceLimit)}.`);
      continue;
    }

    try {
      const blob = await fetchWorkspaceBlob(candidate.path, workspaceRoot);
      if (blob.size > sourceLimit) {
        notes.push(`Native attachment skipped: ${candidate.path} is ${formatBytes(blob.size)}, above ${formatBytes(sourceLimit)}.`);
        continue;
      }
      const originalFile = new File([blob], candidate.name || basename(candidate.path), {
        type: blob.type || candidate.mediaType,
      });
      const file = candidate.mediaType.startsWith("image/")
        ? await prepareNativeImageFile(originalFile)
        : originalFile;
      // The upload endpoint may create a conversation for the first native
      // attachment. Pin that owner immediately and pass it explicitly for all
      // later uploads; relying on server-side "current conversation" state is
      // what caused pasted PDFs to land in a different turn after a switch.
      const result = await uploadAttachment(sessionId, ownerConversationId, file);
      const returnedOwner = String(result.conversation_id || "").trim();
      if (!returnedOwner) {
        throw new Error("附件上传没有返回所属会话。");
      }
      if (ownerConversationId && returnedOwner !== ownerConversationId) {
        const mismatch = new Error("附件所属会话已变化，请切回原会话后重试。");
        mismatch.name = "AttachmentConversationMismatch";
        throw mismatch;
      }
      ownerConversationId = returnedOwner;
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
    } catch (error) {
      // An owner mismatch is a consistency/security failure, not a recoverable
      // per-file parse warning. Abort the whole native batch so the composer
      // cannot send a partially-bound request into the wrong conversation.
      if (error instanceof Error && error.name === "AttachmentConversationMismatch") {
        throw error;
      }
      const detail = error instanceof Error && error.message.trim()
        ? ` (${error.message.trim()})`
        : "";
      notes.push(`Native attachment unavailable: ${candidate.path}${detail}`);
    }
  }

  return {
    attachments,
    attachmentRefs,
    notes: notes.length ? `Native context notes:\n${notes.map((note) => `- ${note}`).join("\n")}` : "",
    conversationId: ownerConversationId || undefined,
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

const emptyNativeContextAttachments = (conversationId = ""): NativeContextAttachments => ({
  attachments: [],
  attachmentRefs: [],
  notes: "",
  ...(conversationId.trim() ? { conversationId: conversationId.trim() } : {}),
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
  const response = await fetchWithTimeout(url, { headers: authHeaders() });
  if (!response.ok) {
    throw new Error(`Workspace raw request failed (${response.status}).`);
  }
  return response.blob();
};

const isNativeContextPath = (path: string): boolean => {
  const mediaType = mediaTypeForPath(path);
  return mediaType.startsWith("image/") || mediaType === "application/pdf";
};

const basename = (path: string): string =>
  path.split(/[/\\]/).filter(Boolean).pop() || path;

const normalizePathKey = (path: string, workspaceRoot = ""): string =>
  workspaceFilePathComparisonKey(path, workspaceRoot);

const splitAnchor = (value: string): { path: string; anchor: string } => {
  const index = value.lastIndexOf("#");
  if (index < 0) return { path: value, anchor: "" };
  return { path: value.slice(0, index), anchor: value.slice(index + 1) };
};
