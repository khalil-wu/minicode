import { ChevronRight, FileText, FileType, Image, Link, Paperclip } from 'lucide-react'
import { useMemo } from 'react'
import { getWebSocket } from '../../hooks/useWebSocket'
import { useAppStore } from '../../stores'
import type { ArtifactContentState, ChatMessage, MessageAttachmentRef, ReplyAttachmentMeta } from '../../stores/types'
import {
  ActivityButtonRow,
  ActivityIcon,
  ActivitySection,
  EmptyLine,
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
}

export const ArtifactsTab = () => {
  const conversationId = useAppStore((s) => s.conversationId)
  const messages = useAppStore((s) => s.messages)
  const previewArtifact = useAppStore((s) => s.previewArtifact)
  const items = useMemo(() => collectArtifacts(messages, previewArtifact), [messages, previewArtifact])

  if (!conversationId) {
    return (
      <div style={panelStyle}>
        <PanelHeader title="文件" />
        <EmptyLine>No active conversation.</EmptyLine>
      </div>
    )
  }

  if (items.length === 0) {
    return (
      <div style={panelStyle}>
        <PanelHeader title="文件" />
        <EmptyLine>No generated files, previews, or attachments yet.</EmptyLine>
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
      store.setPreviewArtifact(null)
      store.addPanel({ id: `artifact-${item.artifactId}`, kind: 'preview', label: item.label.slice(0, 24) || 'Artifact' })
      store.setRightStackTab('preview')
      getWebSocket()?.send({ type: 'read_artifact', artifact_id: item.artifactId })
      return
    }
    if (item.path) {
      store.openEditorFile(item.path, item.label)
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

function collectArtifacts(messages: ChatMessage[], previewArtifact: ArtifactContentState | null): ArtifactItem[] {
  const items: ArtifactItem[] = []
  const seen = new Set<string>()

  for (const message of messages) {
    for (const artifact of message.artifacts ?? []) {
      if (!artifact.artifactId || seen.has(artifact.artifactId)) continue
      seen.add(artifact.artifactId)
      items.push({
        id: artifact.artifactId,
        label: cleanLabel(artifact.summary) || artifactFallbackLabel(artifact.kind, artifact.mediaType),
        kind: artifact.kind || kindFromMediaType(artifact.mediaType) || 'artifact',
        detail: sizeLabel(artifact.bytes),
        artifactId: artifact.artifactId,
        url: artifact.url,
        mediaType: artifact.mediaType,
      })
    }
    for (const attachment of message.attachmentRefs ?? []) {
      const item = attachmentFromRef(message.id, attachment)
      if (seen.has(item.id)) continue
      seen.add(item.id)
      items.push(item)
    }
    for (const attachment of message.replyAttachments ?? []) {
      const item = attachmentFromReply(message.id, attachment)
      if (seen.has(item.id)) continue
      seen.add(item.id)
      items.push(item)
    }
  }

  if (previewArtifact?.artifactId && !seen.has(previewArtifact.artifactId)) {
    items.unshift({
      id: previewArtifact.artifactId,
      label: previewArtifact.name || artifactFallbackLabel(undefined, previewArtifact.mediaType),
      kind: kindFromMediaType(previewArtifact.mediaType) || 'artifact',
      detail: previewArtifact.mediaType,
      artifactId: previewArtifact.artifactId,
      url: previewArtifact.url,
      mediaType: previewArtifact.mediaType,
    })
  }

  return items.slice(-30).reverse()
}

function attachmentFromRef(messageId: string, attachment: MessageAttachmentRef): ArtifactItem {
  const id = attachment.artifactId || attachment.docId || attachment.id || `${messageId}:${attachment.name}`
  return {
    id,
    label: cleanLabel(attachment.name) || '附件',
    kind: 'attachment',
    detail: attachment.kind,
    artifactId: attachment.artifactId,
    mediaType: attachment.mediaType,
  }
}

function attachmentFromReply(messageId: string, attachment: ReplyAttachmentMeta): ArtifactItem {
  const label = basename(attachment.path) || 'Attachment'
  return {
    id: attachment.path || `${messageId}:reply:${label}`,
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

function kindFromMediaType(mediaType?: string): string {
  if (!mediaType) return ''
  if (mediaType.startsWith('image/')) return 'image'
  if (mediaType === 'application/pdf') return 'pdf'
  if (mediaType.startsWith('text/')) return 'text'
  return mediaType
}

function artifactFallbackLabel(kind?: string, mediaType?: string): string {
  const normalized = `${kind || ''} ${mediaType || ''}`.toLowerCase()
  if (normalized.includes('image')) return '生成图片'
  if (normalized.includes('pdf')) return '生成的 PDF'
  if (normalized.includes('file') || normalized.includes('text')) return '生成文件'
  return '未命名产物'
}

function cleanLabel(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
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
  fontWeight: 650,
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
