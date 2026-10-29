import type { ArtifactContentState, ChatMessage, MessageAttachmentRef, ReplyAttachmentMeta } from "../stores/types";
import type { ToolCallRecord } from "./tool-call-reducer";
import type { ConversationResource } from "../protocol/conversation-resources";
import { getToolCallsFromMessage } from "./content-blocks";
import { mediaTypeForPath } from "./media-types";
import { artifactFallbackLabel, artifactMediaTypeForProjection, artifactSummaryForRecord, canonicalArtifactKind, cleanArtifactLabel, isExecutionResultArtifact, isExecutionStatusLabel, isExecutionOutputMediaType, normalizeArtifactPreview } from "./artifact-projection";

export type ArtifactItem = {
  id: string
  label: string
  kind: string
  detail?: string
  artifactId?: string
  path?: string
  url?: string
  mediaType?: string
  conversationId?: string
  executionResult?: boolean
  occurredAt?: number
  messageId?: string
  turnId?: string
  workspaceRoot?: string
}

export function mergeResourceInventory(liveItems: ArtifactItem[], resources: ConversationResource[]): ArtifactItem[] {
  const keyFor = (item: ArtifactItem) => item.kind === 'attachment' ? `attachment:${item.artifactId || item.id.replace(/^attachment:/, '')}` : item.artifactId ? `artifact:${item.artifactId}` : `workspace:${item.path}`
  const items = new Map<string, ArtifactItem>()
  for (const resource of resources) {
    const mediaType = resource.media_type
    const kind = resource.source === 'attachment' ? 'attachment' : canonicalArtifactKind(resource.kind, mediaType)
    const item: ArtifactItem = {
      id: resource.id, label: resource.name, kind, detail: sizeLabel(resource.size_bytes),
      artifactId: resource.artifact_id, path: resource.path, mediaType,
      conversationId: resource.conversation_id, workspaceRoot: resource.workspace_root,
      executionResult: resource.execution_result, occurredAt: resource.occurred_at,
      messageId: resource.message_id, turnId: resource.turn_id,
    }
    items.set(keyFor(item), item)
  }
  for (const item of liveItems) {
    const key = keyFor(item)
    const stored = items.get(key)
    items.set(key, stored ? { ...mergeArtifactItems(stored, item), label: isPlaceholderLabel(item.label) ? stored.label : item.label } : item)
  }
  return [...items.values()].filter((item) => !item.executionResult).sort((left, right) => (right.occurredAt ?? 0) - (left.occurredAt ?? 0))
}

export function collectArtifacts(
  messages: ChatMessage[],
  previewArtifact: ArtifactContentState | null,
  ownerConversationId?: string,
): ArtifactItem[] {
  const items: ArtifactItem[] = []
  const artifactIndexes = new Map<string, number>()
  const attachmentKeys = new Set<string>()

  const upsertArtifact = (item: ArtifactItem): void => {
    const id = item.artifactId || item.id
    if (!item.artifactId && !item.path) return
    const existingIndex = artifactIndexes.get(id)
    if (existingIndex === undefined) {
      artifactIndexes.set(id, items.length)
      items.push(item)
      return
    }
    items[existingIndex] = mergeArtifactItems(items[existingIndex], item)
  }

  // Generated artifacts and tool records share one identity domain.  Always
  // merge the two projections so a sparse message artifact cannot hide the
  // richer metadata carried by its tool result.
  for (const message of messages) {
    for (const artifact of message.artifacts ?? []) {
      const normalized = normalizeArtifactPreview(artifact)
      const artifactId = normalized.artifactId.trim()
      if (!artifactId) continue
      const kind = canonicalArtifactKind(normalized.kind, normalized.mediaType)
      const mediaType = artifactMediaTypeForProjection(normalized.mediaType, kind)
      upsertArtifact({
        id: artifactId,
        label: cleanArtifactLabel(normalized.summary) || artifactFallbackLabel(kind, mediaType),
        kind,
        detail: sizeLabel(normalized.bytes),
        artifactId,
        url: normalized.url,
        mediaType,
        conversationId: ownerConversationId,
        messageId: message.id, turnId: message.turnId, occurredAt: message.timestamp,
      })
    }
    for (const record of getToolCallsFromMessage(message)) {
      upsertArtifact({ ...artifactFromToolRecord(message.id, record, ownerConversationId), messageId: message.id, turnId: message.turnId, occurredAt: record.startedAt ?? message.timestamp })
      for (const file of record.outputFiles ?? []) {
        upsertArtifact({ ...generatedFileFromReply(message.id, { ...file, isImage: Boolean(file.isImage) }, ownerConversationId), messageId: message.id, turnId: message.turnId, occurredAt: message.timestamp })
      }
    }
    for (const outputFile of message.replyAttachments ?? []) {
      const item = generatedFileFromReply(message.id, outputFile, ownerConversationId)
      upsertArtifact({ ...item, messageId: message.id, turnId: message.turnId, occurredAt: message.timestamp })
    }
  }

  // Attachments are intentionally tracked separately.  A backend artifact id
  // must not suppress an unrelated upload that happens to use the same id.
  for (const message of messages) {
    if (message.role !== 'user') continue
    for (const attachment of message.attachmentRefs ?? []) {
      const item = attachmentFromRef(message.id, attachment)
      if (attachmentKeys.has(item.id)) continue
      attachmentKeys.add(item.id)
      items.push({ ...item, conversationId: ownerConversationId, messageId: message.id, turnId: message.turnId, occurredAt: message.timestamp })
    }
  }

  if (previewArtifact?.artifactId
    && previewArtifact.source !== 'attachment'
    && previewArtifact.source !== 'local'
    && previewArtifact.source !== 'browser'
    && (previewArtifact.source !== 'workspace' || artifactIndexes.has(previewArtifact.artifactId))) {
    const artifactId = previewArtifact.artifactId.trim()
    const kind = canonicalArtifactKind(previewArtifact.kind, previewArtifact.mediaType)
    const mediaType = artifactMediaTypeForProjection(previewArtifact.mediaType, kind)
    const existingIndex = artifactIndexes.get(artifactId)
    const previewItem: ArtifactItem = previewArtifact.source === 'workspace' && existingIndex !== undefined ? {
      ...items[existingIndex], url: previewArtifact.url,
    } : {
      id: artifactId,
      label: cleanArtifactLabel(previewArtifact.name) || artifactFallbackLabel(kind, mediaType),
      kind,
      detail: previewArtifact.mediaType,
      artifactId,
      url: previewArtifact.url,
      mediaType,
      conversationId: ownerConversationId,
    }
    // The currently opened preview is the most recent user-visible artifact.
    // Promote it without trimming the searchable source collection.
    if (existingIndex === undefined) {
      items.push(previewItem)
    } else {
      const merged = mergeArtifactItems(items[existingIndex], previewItem)
      items.splice(existingIndex, 1)
      items.push(merged)
    }
  }

  // Merge explicit deliverable metadata before excluding orchestration text;
  // uploads and workspace outputFiles keep their separate identity domains.
  return items.filter((item) => !item.executionResult).reverse()
}

function artifactFromToolRecord(
  messageId: string,
  record: ToolCallRecord,
  conversationId?: string,
): ArtifactItem {
  const artifactId = String(record.artifactId || '').trim()
  const kind = canonicalArtifactKind(record.artifactKind, record.artifactMediaType, record)
  const mediaType = artifactMediaTypeForProjection(record.artifactMediaType, kind)
  const executionResult = isExecutionResultArtifact(record)
  return {
    id: `tool:${messageId}:${artifactId}`,
    label: artifactSummaryForRecord(record),
    kind,
    detail: sizeLabel(record.artifactBytes),
    artifactId,
    mediaType,
    url: undefined,
    conversationId,
    executionResult,
    occurredAt: executionResult ? record.startedAt : undefined,
  }
}

function mergeArtifactItems(existing: ArtifactItem, incoming: ArtifactItem): ArtifactItem {
  const kind = existing.kind === 'image' || incoming.kind === 'image'
    ? 'image'
    : existing.kind === 'file' && incoming.kind !== 'file'
      ? incoming.kind
      : existing.kind
  const executionResult = kind !== 'image'
    && isExecutionOutputMediaType(existing.mediaType || incoming.mediaType)
    && Boolean(existing.executionResult || incoming.executionResult)
  return {
    ...existing,
    label: (isPlaceholderLabel(existing.label) || (incoming.executionResult && isExecutionStatusLabel(existing.label)))
      && !(incoming.executionResult && !executionResult)
      ? incoming.label : existing.label,
    kind,
    detail: existing.detail || incoming.detail,
    url: incoming.url || existing.url,
    mediaType: incoming.kind === 'image'
      ? incoming.mediaType || existing.mediaType
      : existing.mediaType || incoming.mediaType,
    conversationId: existing.conversationId || incoming.conversationId,
    messageId: incoming.messageId || existing.messageId,
    turnId: incoming.turnId || existing.turnId,
    executionResult,
    occurredAt: incoming.occurredAt ?? existing.occurredAt,
  }
}

function isPlaceholderLabel(value: string): boolean {
  return !cleanArtifactLabel(value) || value === '未命名产物' || value === '生成文件' || value === '生成图片'
}

function attachmentFromRef(messageId: string, attachment: MessageAttachmentRef): ArtifactItem {
  const sourceId = attachment.artifactId || attachment.docId || attachment.id || `${messageId}:${attachment.name}`
  return {
    id: `attachment:${sourceId}`,
    label: cleanArtifactLabel(attachment.name) || '附件',
    kind: 'attachment',
    detail: attachment.kind,
    artifactId: attachment.artifactId,
    mediaType: attachment.mediaType,
  }
}

function generatedFileFromReply(
  messageId: string,
  attachment: ReplyAttachmentMeta,
  conversationId?: string,
): ArtifactItem {
  const label = basename(attachment.path) || '生成文件'
  const mediaType = mediaTypeForPath(attachment.path)
  const kind = canonicalArtifactKind(attachment.isImage ? 'image' : 'file', mediaType)
  return {
    id: `workspace:${attachment.path || `${messageId}:${label}`}`,
    label,
    kind,
    detail: sizeLabel(attachment.size),
    path: attachment.path,
    mediaType,
    conversationId,
  }
}

function basename(path: string): string {
  return path.replace(/\\/g, '/').split('/').pop() || path
}

function sizeLabel(bytes?: number): string | undefined {
  if (!bytes) return undefined
  if (bytes < 1024) return `${bytes} B`
  return `${(bytes / 1024).toFixed(1)} KB`
}
