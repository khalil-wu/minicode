import { ChevronRight, FileText, FileType, FolderOpen, Image, Link, Paperclip, RefreshCw, Terminal } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { EmptyState } from '../../components/EmptyState'
import { openArtifactPreview, openAttachmentPreview, openWorkspaceFilePreview } from '../../chat/openAttachmentPreview'
import { useAppStore } from '../../stores'
import { selectActiveConversationPreview } from '../../lib/preview-projection'
import { mediaTypeForPath } from '../../lib/media-types'
import type { ArtifactContentState, ChatMessage, MessageAttachmentRef, ReplyAttachmentMeta } from '../../stores/types'
import type { ToolCallRecord } from '../../lib/tool-call-reducer'
import { getToolCallsFromMessage } from '../../lib/content-blocks'
import { revealConversationMessage } from '../../chat/revealConversationMessage'
import { fetchConversationResources, type ConversationResource, type ConversationResourcePage } from '../../protocol/conversation-resources'
import {
  artifactFallbackLabel,
  artifactMediaTypeForProjection,
  artifactSummaryForRecord,
  canonicalArtifactKind,
  cleanArtifactLabel,
  isExecutionResultArtifact,
  isExecutionStatusLabel,
  isExecutionOutputMediaType,
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
  executionResult?: boolean
  occurredAt?: number
  messageId?: string
  turnId?: string
  workspaceRoot?: string
}

export const ArtifactsTab = () => {
  const conversationId = useAppStore((s) => s.conversationId)
  const isConnected = useAppStore((s) => s.isConnected)
  const workspaceRoot = useAppStore((s) => s.workingDirectory)
  const messages = useAppStore((s) => s.messages)
  const previewArtifact = useAppStore((s) => selectActiveConversationPreview(s).previewArtifact)
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState('all')
  const [recentOnly, setRecentOnly] = useState(false)
  const [inventory, setInventory] = useState<{ conversationId: string; workspaceRoot: string; query: string; kind: string; page: ConversationResourcePage } | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [refreshVersion, setRefreshVersion] = useState(0)
  const requestOwner = useRef('')
  requestOwner.current = `${conversationId}\u0000${workspaceRoot}\u0000${query}\u0000${kind}\u0000${refreshVersion}`
  const currentInventory = inventory?.conversationId === conversationId && inventory.workspaceRoot === workspaceRoot && inventory.query === query && inventory.kind === kind ? inventory : null
  useEffect(() => {
    setLoadError('')
    if (!conversationId || !isConnected) { setLoading(false); return }
    const controller = new AbortController()
    const requestKey = requestOwner.current
    setLoading(true)
    const timer = window.setTimeout(() => {
      void fetchConversationResources(conversationId, { query, kind, workspaceRoot, signal: controller.signal }).then((page) => {
        if (requestOwner.current === requestKey) setInventory({ conversationId, workspaceRoot, query, kind, page })
      }).catch((error: unknown) => {
        if (!controller.signal.aborted) setLoadError(error instanceof Error ? error.message : String(error))
      }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    }, 180)
    return () => { window.clearTimeout(timer); controller.abort() }
  }, [conversationId, workspaceRoot, isConnected, query, kind, refreshVersion])
  const loadMore = async () => {
    const owner = currentInventory!
    const requestKey = requestOwner.current
    setLoading(true); setLoadError('')
    try {
      const page = await fetchConversationResources(owner.conversationId, { query: owner.query, kind: owner.kind, workspaceRoot: owner.workspaceRoot, after: owner.page.after })
      setInventory((current) => current === owner ? { ...owner, page: { ...page, items: [...owner.page.items, ...page.items] } } : current)
    } catch (error) { if (requestOwner.current === requestKey) setLoadError(error instanceof Error ? error.message : String(error)) }
    finally { if (requestOwner.current === requestKey) setLoading(false) }
  }
  const items = useMemo(
    () => mergeResourceInventory(collectArtifacts(messages, previewArtifact, conversationId || undefined), currentInventory?.page.items ?? []),
    [conversationId, messages, previewArtifact, currentInventory],
  )

  if (!conversationId) {
    return (
      <div style={panelStyle}>
        <PanelHeader title="文件" />
        <EmptyState compact icon={<FolderOpen size={20} />} title="暂无活动对话" hint="开始对话后，文件、附件与执行结果会显示在这里。" />
      </div>
    )
  }

  const filtered = items.filter((item) => `${item.label} ${item.path || ''} ${item.turnId || ''}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
    && (kind === 'all' || (kind === 'image' ? item.mediaType?.startsWith('image/') : kind === 'attachment' ? item.kind === 'attachment' : kind === 'execution' ? item.executionResult : item.kind !== 'attachment' && !item.executionResult && !item.mediaType?.startsWith('image/'))))
  const visible = recentOnly ? filtered.slice(0, 12) : filtered
  const generated = visible.filter((item) => item.kind !== 'attachment' && !item.executionResult)
  const executionResults = visible.filter((item) => item.executionResult)
  const attachments = visible.filter((item) => item.kind === 'attachment')

  return (
    <div style={panelStyle}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
        <PanelHeader title="文件" meta={`${Math.max(items.length, currentInventory?.page.total ?? 0)} 项`} />
        <button type="button" className="btn-ghost mc-icon-button" aria-label="刷新历史资源" disabled={!isConnected || loading} onClick={() => setRefreshVersion((value) => value + 1)}><RefreshCw size={14} /></button>
      </div>
      <input aria-label="搜索文件、附件与执行结果" placeholder="搜索文件名或轮次…" value={query} onChange={(event) => setQuery(event.target.value)} style={filterInputStyle} />
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <select aria-label="制品类型" value={kind} onChange={(event) => setKind(event.target.value)} style={filterInputStyle}>
          <option value="all">全部类型</option><option value="file">文件</option><option value="image">图像</option><option value="attachment">附件</option><option value="execution">执行结果</option>
        </select>
        <label style={metaStyle}><input type="checkbox" checked={recentOnly} onChange={(event) => setRecentOnly(event.target.checked)} />仅最近 12 项</label>
        <span style={metaStyle}>{filtered.length} 项匹配</span>
      </div>
      <InfoCard>
        <InfoRow label="生成文件" value={String(generated.length)} />
        <InfoRow label="附件" value={String(attachments.length)} />
        {executionResults.length > 0 && <InfoRow label="执行结果" value={String(executionResults.length)} />}
      </InfoCard>
      <ArtifactSection title="生成文件" items={generated} />
      <ArtifactSection title="执行结果" items={executionResults} />
      <ArtifactSection title="附件" items={attachments} />
      {loading && <p role="status" style={metaStyle}>正在读取历史资源…</p>}
      {loadError && <p role="alert" style={{ ...metaStyle, whiteSpace: 'normal' }}>{loadError}<button type="button" className="btn-ghost" onClick={() => setRefreshVersion((value) => value + 1)}>重新读取</button></p>}
      {currentInventory?.page.has_more && <button type="button" className="btn-ghost" disabled={loading} onClick={() => void loadMore()}>读取更多历史资源</button>}
      {!isConnected && <p style={metaStyle}>连接后端后可读取完整历史资源。</p>}
      {filtered.length === 0 && !loading && !loadError && <EmptyState compact icon={<FileText size={18} />} title={query || kind !== 'all' ? '没有匹配的文件' : '暂无文件、附件或执行结果'} hint={query || kind !== 'all' ? '尝试其他文件名或类型。' : '生成文件、上传附件或产生执行输出后会显示在这里。'} />}
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
      const conversation = store.conversations.find((entry) => entry.id === item.conversationId)
      openWorkspaceFilePreview({
        path: item.path,
        name: item.label,
        mediaType: item.mediaType,
        kind: item.kind,
        workspaceRoot: item.workspaceRoot || conversation?.worktreePath || conversation?.workspaceRoot
          || (item.conversationId === store.conversationId ? store.workingDirectory : undefined),
        conversationId: item.conversationId,
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
        const executionTime = item.executionResult && item.occurredAt
          ? new Date(item.occurredAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
          : ''
        return (
          <div key={item.id}>
          <ActivityButtonRow onClick={() => openItem(item)} title={item.path || (executionTime ? `${item.label} · ${executionTime}` : item.detail || item.label)}>
            <ActivityIcon><Icon size={14} /></ActivityIcon>
            <span style={{ flex: 1, minWidth: 0 }}>
              <span style={labelStyle}>{item.label}</span>
              <span style={metaStyle}>{[item.executionResult ? executionTime || '执行输出' : item.path || item.mediaType || item.kind, item.detail].filter(Boolean).join(' - ')}</span>
            </span>
            <ChevronRight size={14} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
          </ActivityButtonRow>
          {item.messageId && item.conversationId && <div style={{ display: 'flex', justifyContent: 'space-between', gap: 6, padding: '0 4px 5px 28px' }}>
            <span style={metaStyle} title={item.turnId || item.messageId}>{item.turnId ? `轮次 ${item.turnId.slice(-8)}` : `消息 ${item.messageId.slice(-8)}`}{item.occurredAt ? ` · ${new Date(item.occurredAt).toLocaleString()}` : ''}</span>
            <button type="button" className="activity-source-locate" onClick={() => revealConversationMessage(item.conversationId!, item.messageId!)}>回到对话</button>
          </div>}
          </div>
        )
      })}
    </ActivitySection>
  )
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
  return [...items.values()].sort((left, right) => (right.occurredAt ?? 0) - (left.occurredAt ?? 0))
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

  return items.reverse()
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

function artifactIcon(item: ArtifactItem) {
  if (item.executionResult) return Terminal
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

const filterInputStyle: React.CSSProperties = { minWidth: 0, padding: '7px 9px', border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-sm)', background: 'var(--surface-base)', color: 'var(--text-primary)', font: 'inherit', fontSize: 'var(--text-xs)' }

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
