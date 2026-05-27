import { getWebSocket } from "../hooks/useWebSocket";
import { uploadAttachment } from "../protocol/api";
import { useAppStore } from "../stores";

const uploadWarning = (
  file: File,
  result: { doc_id?: string; indexed_chunks?: number; attachment?: Record<string, unknown> },
): string | undefined => {
  if (!/pdf/i.test(file.type || file.name)) return undefined;
  const parseError = String(result.attachment?.parse_error ?? "").trim();
  if (parseError) return shortUploadError(parseError);
  if (result.doc_id && (result.indexed_chunks ?? 0) > 0) return undefined;
  return "PDF attached; extracted text is not indexed.";
};

const shortUploadError = (value: string): string => {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (!normalized) return "Upload failed";
  if (normalized.toLowerCase().includes("session")) return "Session disconnected";
  if (normalized.toLowerCase().includes("pdf")) return "PDF parse failed";
  if (normalized.length > 90) return `${normalized.slice(0, 87)}...`;
  return normalized;
};

export const uploadComposerFiles = (files: File[]) => {
  const sessionId = getWebSocket()?.sessionId;
  if (!sessionId) {
    for (const file of files) {
      const id = `att-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
      useAppStore.getState().addAttachment({
        id,
        name: file.name || "file",
        type: file.type || "application/octet-stream",
        size: file.size,
        status: "error",
        error: "Session disconnected. Reconnect before uploading.",
      });
    }
    return;
  }

  for (const file of files) {
    const id = `att-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
    const isImage = file.type.startsWith("image/");
    const dataUrl = isImage ? URL.createObjectURL(file) : undefined;

    useAppStore.getState().addAttachment({
      id,
      name: file.name || (isImage ? "pasted-image.png" : "file"),
      type: file.type || "application/octet-stream",
      size: file.size,
      status: "uploading",
      dataUrl,
    });

    uploadAttachment(sessionId, file)
      .then((result) => {
        useAppStore.getState().updateAttachment(id, {
          status: "ready",
          artifactId: result.artifact_id,
          docId: result.doc_id,
          indexedChunks: result.indexed_chunks,
          attachment: result.attachment,
          error: uploadWarning(file, result),
        });
      })
      .catch((error: unknown) => {
        const message = error && typeof error === "object" && "message" in error
          ? String((error as { message?: unknown }).message)
          : "Upload failed";
        useAppStore.getState().updateAttachment(id, { status: "error", error: shortUploadError(message) });
      });
  }
};
