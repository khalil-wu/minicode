import { getWebSocket } from "../hooks/useWebSocket";
import { uploadAttachment } from "../protocol/api";
import { useAppStore } from "../stores";
import { pushToast } from "../overlays/ToastContainer";
import { getPastedTextMetadata, PASTED_TEXT_INPUT_SOURCE } from "./pastedText";

const activeUploads = new Map<string, AbortController>();

const attachmentId = (): string => {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `att-${crypto.randomUUID()}`;
  }
  return `att-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
};

const uploadWarning = (
  file: File,
  result: { attachment?: Record<string, unknown> },
): string | undefined => {
  if (!/pdf/i.test(file.type || file.name)) return undefined;
  const parseError = String(result.attachment?.parse_error ?? "").trim();
  if (parseError) return shortUploadError(parseError);
  return undefined;
};

const shortUploadError = (value: string): string => {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (!normalized) return "Upload failed";
  if (normalized.toLowerCase().includes("session")) return "Session disconnected";
  if (normalized.toLowerCase().includes("pdf")) return "PDF parse failed";
  if (normalized.length > 90) return `${normalized.slice(0, 87)}...`;
  return normalized;
};

const startUpload = (id: string, file: File, sessionId: string) => {
  const controller = new AbortController();
  activeUploads.get(id)?.abort();
  activeUploads.set(id, controller);
  const pastedText = getPastedTextMetadata(file);

  uploadAttachment(sessionId, file, controller.signal)
    .then((result) => {
      if (activeUploads.get(id) !== controller) return;
      const attachment = {
        ...result.attachment,
        ...(pastedText ? {
          input_source: PASTED_TEXT_INPUT_SOURCE,
          source_char_count: pastedText.charCount,
        } : {}),
      };
      useAppStore.getState().updateAttachment(id, {
        status: "ready",
        artifactId: result.artifact_id,
        docId: result.doc_id,
        attachment,
        error: uploadWarning(file, result),
      });
      activeUploads.delete(id);
    })
    .catch((error: unknown) => {
      if (activeUploads.get(id) !== controller) return;
      activeUploads.delete(id);
      if (controller.signal.aborted) return;
      const message = error && typeof error === "object" && "message" in error
        ? String((error as { message?: unknown }).message)
        : "Upload failed";
      useAppStore.getState().updateAttachment(id, { status: "error", error: shortUploadError(message) });
    });
};

export const uploadComposerFiles = (files: File[]) => {
  const sessionId = getWebSocket()?.sessionId;
  const createdIds: string[] = [];

  for (const file of files) {
    const id = attachmentId();
    const isImage = file.type.startsWith("image/");
    const dataUrl = isImage ? URL.createObjectURL(file) : undefined;
    const pastedText = getPastedTextMetadata(file);
    createdIds.push(id);

    useAppStore.getState().addAttachment({
      id,
      name: file.name || (isImage ? "pasted-image.png" : "file"),
      type: file.type || "application/octet-stream",
      size: file.size,
      status: sessionId ? "uploading" : "error",
      dataUrl,
      inputSource: pastedText?.inputSource ?? "upload",
      sourceCharCount: pastedText?.charCount,
      localFile: file,
      ...(!sessionId ? { error: "Session disconnected. Reconnect before uploading." } : {}),
    });

    if (pastedText) {
      pushToast(
        `Long paste (${pastedText.charCount.toLocaleString()} characters) attached as ${file.name}. It will be treated as your message.`,
        "info",
        4200,
      );
    }

    if (sessionId) startUpload(id, file, sessionId);
  }

  return () => {
    for (const id of createdIds) cancelComposerUpload(id);
  };
};

export const retryComposerAttachment = (id: string): boolean => {
  const attachment = useAppStore.getState().attachments.find((item) => item.id === id);
  if (!attachment?.localFile) return false;
  const sessionId = getWebSocket()?.sessionId;
  if (!sessionId) {
    useAppStore.getState().updateAttachment(id, {
      status: "error",
      error: "Session disconnected. Reconnect before uploading.",
    });
    return false;
  }
  useAppStore.getState().updateAttachment(id, { status: "uploading", error: undefined });
  startUpload(id, attachment.localFile, sessionId);
  return true;
};

export const cancelComposerUpload = (id: string): void => {
  activeUploads.get(id)?.abort();
  activeUploads.delete(id);
};
