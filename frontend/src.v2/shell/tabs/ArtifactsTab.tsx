import { ChevronRight, FileText, FileType, FolderOpen, Image, Link, Paperclip } from 'lucide-react'
import { useMemo } from 'react'
import { EmptyState } from '../../components/EmptyState'
import { openArtifactPreview, openAttachmentPreview, openWorkspaceFilePreview } from '../../chat/openAttachmentPreview'
import { useAppStore } from '../../stores'
import { selectActiveConversationPreview } from '../../lib/preview-projection'
import type { ArtifactContentState, ChatMessage, MessageAttachmentRef, ReplyAttachmentMeta } from '../../stores/types'
import type { ToolCallRecord } from '../../lib/tool-call-reducer'
import { getToolCallsFromMessage } from '../../lib/content-blocks'
import {
  artifactFallbackLabel,
  artifactMediaTypeForProjection,
  artifactSummaryForRecord,
  canonicalArtifactKind,
  cleanArtifactLabel,
  normalizeArtifactPreview,
} from '../../lib/artifact-projection'
import {
  ActivityButtonRow,
  ActivityIcon,
  ActivitySection,
  InfoCard,
  InfoRow,
  PanelHeader,
} from '../SidebarShared'

type ArtifactItem = {
  id: string
  label: string
  kind: string
  detail?: string
  artifactId?: string
  path?: string
  url?: string
  mediaType?: string
  conversationId?: string
}

export const ArtifactsTab = () => {
  const conversationId = useAppStore((s) => s.conversationId)
  const messages = useAppStore((s) => s.messages)
  const previewArtifact = useAppStore((s) => selectActiveConversationPreview(s).previewArtifact)
  const items = useMemo(
    () => collectArtifacts(messages, previewArtifact, conversationId || undefined),
    [conversationId, messages, previewArtifact],
  )

  if (!conversationId) {
    return (
      <div style={panelStyle}>
        <PanelHeader title="文件" />
        <EmptyState compact icon={<FolderOpen size={20} />} title="暂无活动对话" hint="开始对话后，生成的文件与附件会显示在这里。" />
      </div>
    )
  }

  if (items.length === 0) {
    return (
      <div style={panelStyle}>
        <PanelHeader title="文件" />
        <EmptyState compact icon={<FolderOpen size={20} />} title="暂无生成文件或附件" hint="Agent 生成文件、预览或附件后会显示在这里。" />
      </div>
    )
  }

  const generated = items.filter((item) => item.kind !== 'attachment')
  const attachments = items.filter((item) => item.kind === 'attachment')

  return (
    <div style={panelStyle}>
      <PanelHeader title="文件" meta={`${items.length} 项`} />
      <InfoCard>
        <InfoRow label="生成文件" value={String(generated.length)} />
        <InfoRow label="附件" value={String(attachments.length)} />
      </InfoCard>
      <ArtifactSection title="生成文件" items={generated} />
      <ArtifactSection title="附件" items={attachments} />
    </div>
  )
}

const ArtifactSection = ({ title, items }: { title: string; items: ArtifactItem[] }) => {
  if (items.length === 0) return null
  const openItem = (item: ArtifactItem) => {
    const store = useAppStore.getState()
    if (item.artifactId) {
      if (item.kind === 'attachment') {
        openAttachmentPreview({
          artifactId: item.artifactId,
          name: item.label,
          mediaType: item.mediaType,
          kind: item.kind,
          conversationId: item.conversationId,
        })
        return
      }
      openArtifactPreview({
        artifactId: item.artifactId,
        name: item.label,
        mediaType: item.mediaType,
        kind: item.kind,
        conversationId: item.conversationId,
      })
      return
    }
    if (item.path) {
      openWorkspaceFilePreview({
        path: item.path,
        name: item.label,
        mediaType: item.mediaType,
        kind: item.kind,
        workspaceRoot: store.workingDirectory,
        conversationId: store.conversationId || undefined,
      })
      return
    }
    if (item.url) {
      store.openLivePreview(item.url)
    }
  }

  return (
    <ActivitySection title={title} previewCount={8}>
      {items.map((item) => {
        const Icon = artifactIcon(item)
        return (
          <ActivityButtonRow key={item.id} onClick={() => openItem(item)} title={item.detail || item.label}>
            <ActivityIcon><Icon size={14} /></ActivityIcon>
            <span style={{ flex: 1, minWidth: 0 }}>
              <span style={labelStyle}>{item.label}</span>
              <span style={metaStyle}>{[item.mediaType || item.kind, item.detail].filter(Boolean).join(' - ')}</span>
            </span>
            <ChevronRight size={14} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
          </ActivityButtonRow>
        )
      })}
    </ActivitySection>
  )
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
    const artifactId = item.artifactId
    if (!artifactId) return
    const existingIndex = artifactIndexes.get(artifactId)
    if (existingIndex === undefined) {
      artifactIndexes.set(artifactId, items.length)
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
      })
    }
    for (const record of getToolCallsFromMessage(message)) {
      upsertArtifact(artifactFromToolRecord(message.id, record, ownerConversationId))
    }
  }

  // Attachments are intentionally tracked separately.  A backend artifact id
  // must not suppress an unrelated upload that happens to use the same id.
  for (const message of messages) {
    for (const attachment of message.attachmentRefs ?? []) {
      const item = attachmentFromRef(message.id, attachment)
      if (attachmentKeys.has(item.id)) continue
      attachmentKeys.add(item.id)
      items.push({ ...item, conversationId: ownerConversationId })
    }
    for (const attachment of message.replyAttachments ?? []) {
      const item = attachmentFromReply(message.id, attachment)
      if (attachmentKeys.has(item.id)) continue
      attachmentKeys.add(item.id)
      items.push({ ...item, conversationId: ownerConversationId })
    }
  }

  if (previewArtifact?.artifactId) {
    const artifactId = previewArtifact.artifactId.trim()
    const kind = canonicalArtifactKind(previewArtifact.kind, previewArtifact.mediaType)
    const mediaType = artifactMediaTypeForProjection(previewArtifact.mediaType, kind)
    const previewItem: ArtifactItem = {
      id: artifactId,
      label: cleanArtifactLabel(previewArtifact.name) || artifactFallbackLabel(kind, mediaType),
      kind,
      detail: previewArtifact.mediaType,
      artifactId,
      url: previewArtifact.url,
      mediaType,
      conversationId: ownerConversationId,
    }
    const existingIndex = artifactIndexes.get(artifactId)
    // The currently opened preview is the most recent user-visible artifact.
    // Promote it before applying the cap so a long transcript cannot hide the
    // item the user just opened.
    if (existingIndex === undefined) {
      items.push(previewItem)
    } else {
      const merged = mergeArtifactItems(items[existingIndex], previewItem)
      items.splice(existingIndex, 1)
      items.push(merged)
    }
  }

  return items.slice(-30).reverse()
}

function artifactFromToolRecord(
  messageId: string,
  record: ToolCallRecord,
  conversationId?: string,
): ArtifactItem {
  const artifactId = String(record.artifactId || '').trim()
  const kind = canonicalArtifactKind(record.artifactKind, record.artifactMediaType, record)
  const mediaType = artifactMediaTypeForProjection(record.artifactMediaType, kind)
  return {
    id: `tool:${messageId}:${artifactId}`,
    label: artifactSummaryForRecord(record),
    kind,
    detail: sizeLabel(record.artifactBytes),
    artifactId,
    mediaType,
    url: undefined,
    conversationId,
  }
}

function mergeArtifactItems(existing: ArtifactItem, incoming: ArtifactItem): ArtifactItem {
  const kind = existing.kind === 'image' || incoming.kind === 'image'
    ? 'image'
    : existing.kind === 'file' && incoming.kind !== 'file'
      ? incoming.kind
      : existing.kind
  return {
    ...existing,
    label: isPlaceholderLabel(existing.label) ? incoming.label : existing.label,
    kind,
    detail: existing.detail || incoming.detail,
    url: incoming.url || existing.url,
    mediaType: incoming.kind === 'image'
      ? incoming.mediaType || existing.mediaType
      : existing.mediaType || incoming.mediaType,
    conversationId: existing.conversationId || incoming.conversationId,
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

function attachmentFromReply(messageId: string, attachment: ReplyAttachmentMeta): ArtifactItem {
  const label = basename(attachment.path) || 'Attachment'
  return {
    id: `reply:${attachment.path || `${messageId}:${label}`}`,
    label,
    kind: 'attachment',
    detail: sizeLabel(attachment.size),
    path: attachment.path,
    mediaType: attachment.isImage ? 'image/*' : undefined,
  }
}

function artifactIcon(item: ArtifactItem) {
  const kind = (item.kind || "").toLowerCase()
  const media = (item.mediaType || "").toLowerCase()
  if (kind === "image" || media.startsWith("image/")) return Image
  if (kind === "pdf" || media.includes("pdf") || item.label.toLowerCase().endsWith(".pdf")) return FileType
  if (kind === "url" || kind === "link" || Boolean(item.url)) return Link
  if (kind === "attachment") return Paperclip
  return FileText
}

function basename(path: string): string {
  return path.replace(/\\/g, '/').split('/').pop() || path
}

function sizeLabel(bytes?: number): string | undefined {
  if (!bytes) return undefined
  if (bytes < 1024) return `${bytes} B`
  return `${(bytes / 1024).toFixed(1)} KB`
}

const panelStyle: React.CSSProperties = {
  display: 'grid',
  gap: 8,
}

const labelStyle: React.CSSProperties = {
  display: 'block',
  color: 'var(--text-primary)',
  fontSize: 'var(--text-xs)',
  lineHeight: 1.3,
  fontWeight: "var(--fw-semibold)",
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
}

const metaStyle: React.CSSProperties = {
  display: 'block',
  marginTop: 2,
  color: 'var(--text-muted)',
  fontSize: "var(--text-3xs)",
  lineHeight: 1.2,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
}
