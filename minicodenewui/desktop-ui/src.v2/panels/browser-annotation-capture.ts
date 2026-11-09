import { embeddedBrowserCapture, getDesktopPreferences } from "../desktop/runtime";
import { uploadAttachment } from "../protocol/api";
import { getWebSocket } from "../hooks/useWebSocket";
import type { ComposerAttachment, MessageAttachmentRef } from "../stores/types";

type Selection = { selector: string; rect: { x: number; y: number; width: number; height: number }; viewport: { width: number; height: number } };
export type BrowserFeedbackCapture = { attachments: Record<string, unknown>[]; attachmentRefs: MessageAttachmentRef[]; composerAttachments: ComposerAttachment[] };

export async function captureBrowserAnnotationAttachments({ conversationId, workspaceRoot, targetId, url, elements }: {
  conversationId: string; workspaceRoot: string; targetId: string; url: string; elements: Selection[];
}): Promise<BrowserFeedbackCapture> {
  const { annotationScreenshotsMode } = await getDesktopPreferences();
  const regions = elements.filter((element) => !element.selector);
  const captures: Array<Selection | null> = regions.length ? regions : annotationScreenshotsMode === "always" ? [null] : [];
  const result: BrowserFeedbackCapture = { attachments: [], attachmentRefs: [], composerAttachments: [] };
  for (const region of captures) {
    const image = await embeddedBrowserCapture({ id: targetId, conversationId, expectedUrl: url,
      ...(region ? { rect: region.rect, viewport: region.viewport } : {}) });
    const dataUrl = `data:${image.mimeType};base64,${image.data}`;
    const bytes = Uint8Array.from(atob(image.data), (character) => character.charCodeAt(0));
    const file = new File([bytes], `browser-annotation-${Date.now()}-${result.attachments.length + 1}.png`, { type: image.mimeType });
    const uploaded = await uploadAttachment(getWebSocket()!.sessionId, conversationId, file, { workspaceRoot });
    if (uploaded.conversation_id !== conversationId) throw new Error("批注截图所属对话已变化，请重新上传。");
    const id = `browser-screenshot-${crypto.randomUUID()}`;
    const attachment = { ...uploaded.attachment, id };
    result.attachments.push(attachment);
    result.attachmentRefs.push({ id, name: uploaded.file_name, kind: "image", mediaType: image.mimeType, sizeBytes: file.size,
      artifactId: uploaded.artifact_id, docId: uploaded.doc_id, dataUrl });
    result.composerAttachments.push({ id, name: uploaded.file_name, type: image.mimeType, size: file.size, status: "ready", progress: 100,
      conversationId, workspaceRoot, artifactId: uploaded.artifact_id, docId: uploaded.doc_id, attachment, dataUrl });
  }
  return result;
}
