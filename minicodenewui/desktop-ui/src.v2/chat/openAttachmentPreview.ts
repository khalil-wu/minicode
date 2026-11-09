import { getWebSocket } from "../hooks/useWebSocket";
import { pushToast } from "../overlays/ToastContainer";
import {
  apiBase,
  artifactRawResourceUrlWithToken,
  attachmentRawResourceUrlWithToken,
  authHeaders,
  errorMessageFromResponseText,
  fetchAttachmentPreview,
  fetchWithTimeout,
} from "../protocol/api";
import { fileWorkspaceOwner, workspaceFileAbsolutePath } from "../protocol/file-workspace";
import {
  commandResultSucceeded,
  createClientCommandId,
  sendClientCommandAwaitResult,
} from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import type { ArtifactContentState, WorkspaceFileOwner } from "../stores/types";
import { openPreferredFile } from "../lib/open-preferred-target";
import { isDesktop } from "../desktop/runtime";
import {
  isNativePreviewMediaType,
  isTextMediaType,
  kindForMediaType,
  mediaTypeForPath,
} from "../lib/media-types";
import {
  artifactMediaTypeForProjection,
  canonicalArtifactKind,
} from "../lib/artifact-projection";
import {
  beginPreviewRequest,
  isPreviewRequestCurrent,
  setPreviewObjectUrl,
  setPreviewRequestId,
  type PreviewRequestLease,
} from "./previewRequestScope";

export interface AttachmentPreviewTarget {
  artifactId: string;
  name?: string;
  mediaType?: string;
  kind?: string;
  conversationId?: string;
}

export interface ArtifactPreviewTarget extends AttachmentPreviewTarget {
  summary?: string;
}

export interface WorkspaceFilePreviewTarget {
  path: string;
  name?: string;
  mediaType?: string;
  kind?: string;
  workspaceRoot?: string;
  pathRoot?: string;
  conversationId?: string | null;
  environmentId?: string;
  fileOwner?: WorkspaceFileOwner;
  line?: number;
  column?: number;
  endLine?: number;
  endColumn?: number;
  isPreview?: boolean;
}

export interface LocalFilePreviewTarget {
  id: string;
  name: string;
  mediaType?: string;
  kind?: string;
  file?: File;
  url?: string;
  conversationId?: string;
}

export interface ConversationBrowserFilePreviewTarget {
  conversationId: string;
  executionCwd: string;
  path: string;
  name: string;
}

export interface RegisteredSkillPreviewTarget {
  path: string;
  workspaceRoot: string;
  conversationId?: string | null;
}

const normalizedMediaType = (value?: string): string =>
  String(value || "").split(";", 1)[0].trim().toLowerCase();

const supportsNativePreview = isNativePreviewMediaType;

const attachmentNativeUrl = (
  artifactId: string,
  sessionId: string,
  conversationId: string,
  mediaType: string,
  hasNative: boolean,
): string | undefined => {
  if (!hasNative || !supportsNativePreview(mediaType)) return undefined;
  return attachmentRawResourceUrlWithToken(artifactId, sessionId, conversationId) || undefined;
};

const activePreviewConversationId = (explicit?: string): string => {
  const store = useAppStore.getState();
  return String(explicit || store.conversationId || "").trim();
};

const scopedPreviewConversationId = (explicit?: string): string =>
  String(explicit || "").trim();

const openPreviewSurface = (id: string, name: string, conversationId: string): void => {
  const store = useAppStore.getState();
  store.setPreviewOwnerConversationId(conversationId || null);
  store.addPanel({
    id: `artifact-${id}`,
    kind: "preview",
    label: name.slice(0, 24) || "文件",
  });
  store.setRightStackTab("preview");
};

const publishPreview = (
  lease: PreviewRequestLease,
  conversationId: string,
  preview: ArtifactContentState,
): void => {
  if (!isPreviewRequestCurrent(lease)) return;
  const store = useAppStore.getState();
  if (conversationId) {
    store.setConversationPreviewArtifact(conversationId, preview);
    return;
  }
  store.setPreviewArtifact(preview);
};

const failPreview = (
  lease: PreviewRequestLease,
  conversationId: string,
  base: Omit<ArtifactContentState, "content" | "loadedAt">,
  message: string,
): void => {
  publishPreview(lease, conversationId, {
    ...base,
    content: "",
    loading: false,
    error: message,
    loadedAt: Date.now(),
  });
};

export const openAttachmentPreview = (target: AttachmentPreviewTarget): boolean => {
  const artifactId = String(target.artifactId || "").trim();
  if (!artifactId) return false;
  const socket = getWebSocket();
  const sessionId = socket?.sessionId?.trim() || "";
  // Message attachments belong to the conversation that rendered them. Do
  // not fall back to a mutable runtime active-conversation snapshot.
  const conversationId = scopedPreviewConversationId(target.conversationId);
  const name = String(target.name || "附件");
  const base = {
    artifactId,
    name,
    mediaType: target.mediaType,
    kind: target.kind,
    source: "attachment" as const,
  };

  if (!conversationId) {
    pushToast("附件还没有关联到会话，请等待上传完成后重试。", "warning");
    return false;
  }

  const lease = beginPreviewRequest(conversationId, { abortable: true });
  openPreviewSurface(artifactId, name, conversationId);
  publishPreview(lease, conversationId, {
    ...base,
    content: "",
    loading: true,
    loadedAt: Date.now(),
  });

  if (!sessionId) {
    failPreview(lease, conversationId, base, "连接已断开，重连后可预览附件。");
    return false;
  }

  const controller = lease.controller!;
  void fetchAttachmentPreview(sessionId, conversationId, artifactId, controller.signal)
    .then((preview) => {
      if (!isPreviewRequestCurrent(lease)) return;
      const mediaType = normalizedMediaType(preview.media_type || target.mediaType) || "text/plain";
      publishPreview(lease, conversationId, {
        artifactId: preview.artifact_id || artifactId,
        content: preview.content || "",
        preview: preview.summary || undefined,
        warning: preview.parse_error || preview.parse_warning || undefined,
        name: preview.file_name || name,
        mediaType,
        kind: preview.kind || target.kind,
        sizeBytes: preview.size_bytes,
        contentChars: preview.content_chars,
        truncated: preview.truncated,
        hasNative: preview.has_native,
        url: attachmentNativeUrl(
          artifactId,
          sessionId,
          conversationId,
          mediaType,
          preview.has_native,
        ),
        source: "attachment",
        loading: false,
        loadedAt: Date.now(),
      });
    })
    .catch((error: unknown) => {
      if (!isPreviewRequestCurrent(lease)) return;
      if (controller.signal.aborted) return;
      const message = error instanceof Error && error.message.trim()
        ? error.message.trim()
        : "附件预览加载失败。";
      failPreview(lease, conversationId, base, message);
    });
  return true;
};

/** Open a generated artifact in the same File view used by attachments. */
export const openArtifactPreview = (target: ArtifactPreviewTarget): boolean => {
  const artifactId = String(target.artifactId || "").trim();
  if (!artifactId) return false;
  const socket = getWebSocket();
  const sessionId = socket?.sessionId?.trim() || "";
  const conversationId = scopedPreviewConversationId(target.conversationId);
  const name = String(target.name || target.summary || "生成文件");
  const kind = canonicalArtifactKind(target.kind, target.mediaType);
  const mediaType = artifactMediaTypeForProjection(target.mediaType, kind)
    || normalizedMediaType(target.mediaType);
  const base = {
    artifactId,
    name,
    mediaType: mediaType || target.mediaType,
    kind: target.kind || kind,
    source: "artifact" as const,
  };

  if (!conversationId) {
    pushToast("当前没有可读取该文件的会话。", "warning");
    return false;
  }

  const lease = beginPreviewRequest(conversationId);
  const requestId = createClientCommandId();
  setPreviewRequestId(lease, requestId);
  openPreviewSurface(artifactId, name, conversationId);
  publishPreview(lease, conversationId, {
    ...base,
    content: "",
    loading: true,
    loadedAt: Date.now(),
  });
  // Native image artifacts are already owner-scoped in ArtifactStore. Fetch
  // them through the signed raw endpoint instead of sending megabytes of
  // base64 through the WebSocket read_artifact response.
  const nativeUrl = mediaType && (
    supportsNativePreview(mediaType) || target.kind === "binary" || kindForMediaType(mediaType) === "binary"
  ) && sessionId
    ? artifactRawResourceUrlWithToken(artifactId, sessionId, conversationId)
    : "";
  if (nativeUrl) {
    publishPreview(lease, conversationId, {
      ...base,
      mediaType,
      url: nativeUrl,
      content: "",
      loading: false,
      loadedAt: Date.now(),
    });
    return true;
  }
  if (!socket) {
    failPreview(lease, conversationId, base, "连接已断开，重连后可预览文件。");
    return false;
  }
  void sendClientCommandAwaitResult({
    type: "read_artifact",
    artifact_id: artifactId,
    conversation_id: conversationId,
    request_id: requestId,
    client_command_id: requestId,
  }, "read_artifact").then((result) => {
    if (!commandResultSucceeded(result)) {
      failPreview(lease, conversationId, base, result.message || "文件预览加载失败。");
    }
  }).catch((error: unknown) => {
    const message = error instanceof Error && error.message.trim()
      ? error.message.trim()
      : "文件预览加载失败。";
    failPreview(lease, conversationId, base, message);
  });
  return true;
};

/** Preview a workspace deliverable without opening a browser/default app. */
export const openWorkspaceFilePreview = (target: WorkspaceFilePreviewTarget): boolean => {
  const path = target.path.trim();
  if (!path) return false;
  const state = useAppStore.getState();
  const conversationId = target.fileOwner ? target.fileOwner.conversationId
    : target.conversationId === undefined ? state.conversationId : target.conversationId || null;
  const conversation = state.conversations.find(value => value.id === conversationId);
  const workspaceRoot = target.fileOwner ? target.fileOwner.executionCwd
    : target.workspaceRoot || conversation?.executionCwd || conversation?.worktreePath || conversation?.workspaceRoot || state.workingDirectory;
  if (!workspaceRoot) { pushToast("未找到该文件所属的工作区。", "error"); return false; }
  const owner = target.fileOwner ?? { ...fileWorkspaceOwner(state, conversationId, workspaceRoot),
    ...(target.environmentId ? { environmentId: target.environmentId } : {}) };
  // Deliverable paths are relative to the run's cwd. Browser API paths are
  // relative to its separately declared root; carry the exact file between them.
  const absolute = workspaceFileAbsolutePath({ ...owner, browserRoot: target.pathRoot ?? (target.fileOwner ? owner.browserRoot : workspaceRoot) }, path);
  state.openWorkspaceFileTab(owner, absolute, target.name || basename(path), target.line, {
    column: target.column, endLine: target.endLine, endColumn: target.endColumn, isPreview: target.isPreview,
  });
  return true;
};

/** Local assistant references choose the file panel; modifiers explicitly choose the preferred target. */
export const openWorkspaceFileLink = (target: WorkspaceFilePreviewTarget,
  gesture?: { button?: number; ctrlKey?: boolean; metaKey?: boolean }, openInPreferredApp?: () => void): boolean => {
  const modified = gesture?.button === 1 || gesture?.ctrlKey || gesture?.metaKey;
  if (!modified || !isDesktop()) return openWorkspaceFilePreview({ ...target, isPreview: target.isPreview ?? true });
  const state = useAppStore.getState();
  const conversationId = state.conversationId, draftId = state.activeConversationDraftId;
  const owner = target.fileOwner ?? fileWorkspaceOwner(state,
    target.conversationId === undefined ? conversationId : target.conversationId || null, target.workspaceRoot);
  const absolute = workspaceFileAbsolutePath({ ...owner, browserRoot: target.pathRoot ?? target.workspaceRoot ?? owner.browserRoot }, target.path);
  void openPreferredFile(absolute, owner.executionCwd, target.line, () => {
    const current = useAppStore.getState();
    if (current.conversationId !== conversationId || current.activeConversationDraftId !== draftId || current.pendingConversationSwitchId) return;
    if (openInPreferredApp) openInPreferredApp();
    else openWorkspaceFilePreview({ ...target, fileOwner: owner, path: absolute, isPreview: false });
  }, { column: target.column });
  return true;
};

/** Open the exact catalog file without changing the chat, workspace or editor. */
export const openRegisteredSkillPreview = (target: RegisteredSkillPreviewTarget): boolean => {
  const state = useAppStore.getState();
  const conversationId = target.conversationId === undefined ? state.conversationId ?? "" : target.conversationId ?? "";
  if (conversationId !== (state.conversationId ?? "")) {
    pushToast("聊天已切换，请回到原聊天后打开技能。", "error");
    return false;
  }
  const base = { artifactId: `skill:${target.workspaceRoot}:${target.path}`, name: basename(target.path),
    source: "skill" as const, sourcePath: target.path, workspaceRoot: target.workspaceRoot,
    mediaType: "text/markdown", kind: "document" };
  const lease = beginPreviewRequest(conversationId, { abortable: true });
  openPreviewSurface(base.artifactId, base.name, conversationId);
  publishPreview(lease, conversationId, { ...base, content: "", loading: true, loadedAt: Date.now() });
  const query = new URLSearchParams({ skill_path: target.path, workspace_root: target.workspaceRoot });
  void fetchWithTimeout(`${apiBase()}/api/skills/detail?${query}`, {
    headers: authHeaders(), cache: "no-store", signal: lease.controller!.signal,
  }).then(async response => {
    if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
    const detail = await response.json() as { path: string; raw_content: string };
    if (!isPreviewRequestCurrent(lease)) return;
    publishPreview(lease, conversationId, { ...base, sourcePath: detail.path, content: detail.raw_content,
      sizeBytes: new TextEncoder().encode(detail.raw_content).byteLength, contentChars: detail.raw_content.length,
      loading: false, loadedAt: Date.now() });
  }).catch(error => {
    if (!isPreviewRequestCurrent(lease) || lease.controller!.signal.aborted) return;
    failPreview(lease, conversationId, base, error instanceof Error ? error.message : String(error));
  });
  return true;
};

/** Browse a file without changing the conversation's execution or editor root. */
export const openConversationBrowserFilePreview = (target: ConversationBrowserFilePreviewTarget): boolean => {
  const state = useAppStore.getState();
  state.openWorkspaceFileTab(fileWorkspaceOwner(state, target.conversationId, target.executionCwd), target.path, target.name);
  return true;
};

/** Preview a not-yet-uploaded composer file in the shared File view. */
export const openLocalFilePreview = (target: LocalFilePreviewTarget): boolean => {
  const id = String(target.id || "").trim();
  const name = String(target.name || "附件");
  if (!id) return false;
  const conversationId = activePreviewConversationId(target.conversationId);
  const mediaType = normalizedMediaType(target.mediaType || target.file?.type)
    || mediaTypeForPath(name);
  const kind = target.kind || kindForMediaType(mediaType);
  let url = String(target.url || "").trim() || undefined;
  const lease = beginPreviewRequest(conversationId);

  if (!url && target.file && supportsNativePreview(mediaType)) {
    const objectUrl = URL.createObjectURL(target.file);
    if (setPreviewObjectUrl(lease, objectUrl)) url = objectUrl;
  }

  const base = {
    artifactId: `local:${id}`,
    name,
    mediaType,
    kind,
    source: "local" as const,
  };
  openPreviewSurface(`local-${id}`, name, conversationId);

  const isText = isTextMediaType(mediaType, name);
  publishPreview(lease, conversationId, {
    ...base,
    content: "",
    url: supportsNativePreview(mediaType) ? url : undefined,
    loading: Boolean(isText && target.file),
    warning: !supportsNativePreview(mediaType) && !isText
      ? "文件上传并完成解析后，可在这里查看提取内容。"
      : undefined,
    loadedAt: Date.now(),
  });

  if (isText && target.file) {
    void target.file.text()
      .then((content) => {
        if (!isPreviewRequestCurrent(lease)) return;
        const visible = content.slice(0, 2 * 1024 * 1024);
        publishPreview(lease, conversationId, {
          ...base,
          content: visible,
          url: supportsNativePreview(mediaType) ? url : undefined,
          sizeBytes: target.file?.size,
          contentChars: content.length,
          truncated: visible.length < content.length,
          loading: false,
          loadedAt: Date.now(),
        });
      })
      .catch(() => {
        failPreview(lease, conversationId, base, "无法读取本地文件预览。");
      });
  }
  return true;
};

const basename = (path: string): string =>
  path.replace(/\\/g, "/").split("/").filter(Boolean).pop() || path;
