import { collectArtifacts, mergeResourceInventory, type ArtifactItem } from "../../lib/conversation-artifact-items";
export { collectArtifacts, mergeResourceInventory } from "../../lib/conversation-artifact-items";
import { ChevronRight, FileText, FileType, FolderOpen, Image, Link, Paperclip } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { usePanelMessages } from '../usePanelMessages'
import { EmptyState } from '../../components/EmptyState'
import { openArtifactPreview, openAttachmentPreview, openWorkspaceFilePreview } from '../../chat/openAttachmentPreview'
import { useAppStore } from '../../stores'
import { selectActiveConversationPreview } from '../../lib/preview-projection'
import { revealConversationMessage } from '../../chat/revealConversationMessage'
import { fetchConversationResources, type ConversationResourcePage } from '../../protocol/conversation-resources'
import {
  ActivityButtonRow,
  ActivityIcon,
  ActivitySection,
  PanelHeader,
} from '../SidebarShared'
import { FilesPanel } from '../../panels/FilesPanel'

export const ArtifactsTab = () => {
  const conversationId = useAppStore((s) => s.conversationId)
  const isConnected = useAppStore((s) => s.isConnected)
  const conversation = useAppStore((s) => s.conversations.find((item) => item.id === s.conversationId))
  const pendingConversationSwitchId = useAppStore((s) => s.pendingConversationSwitchId)
  const executionCwd = conversation?.executionCwd || conversation?.worktreePath || conversation?.workspaceRoot || ''
  const messages = usePanelMessages()
  const previewArtifact = useAppStore((s) => selectActiveConversationPreview(s).previewArtifact)
  const [inventory, setInventory] = useState<{ conversationId: string; executionCwd: string; page: ConversationResourcePage } | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [refreshVersion, setRefreshVersion] = useState(0)
  const requestOwner = useRef('')
  requestOwner.current = `${conversationId}\u0000${executionCwd}\u0000${pendingConversationSwitchId}\u0000${refreshVersion}`
  const currentInventory = inventory?.conversationId === conversationId && inventory.executionCwd === executionCwd ? inventory : null
  useEffect(() => {
    setLoadError('')
    if (!conversationId || !executionCwd || !isConnected || pendingConversationSwitchId) { setLoading(false); return }
    const controller = new AbortController()
    const requestKey = requestOwner.current
    setLoading(true)
    void fetchConversationResources(conversationId, { workspaceRoot: executionCwd, signal: controller.signal }).then((page) => {
        if (!controller.signal.aborted && requestOwner.current === requestKey) {
          setInventory({ conversationId, executionCwd, page })
          setLoadError((page.read_errors ?? []).map((error) => error.message).join('\n'))
        }
      }).catch((error: unknown) => {
        if (!controller.signal.aborted && requestOwner.current === requestKey) setLoadError(error instanceof Error ? error.message : String(error))
      }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [conversationId, executionCwd, isConnected, pendingConversationSwitchId, refreshVersion])
  const loadMore = async () => {
    const owner = currentInventory!
    const requestKey = requestOwner.current
    setLoading(true); setLoadError('')
    try {
      const page = await fetchConversationResources(owner.conversationId, { workspaceRoot: owner.executionCwd, after: owner.page.after })
      if (requestOwner.current === requestKey) {
        const readErrors = [...(owner.page.read_errors ?? []), ...(page.read_errors ?? [])]
        setInventory((current) => current === owner ? { ...owner, page: { ...page, items: [...owner.page.items, ...page.items], read_errors: readErrors } } : current)
        setLoadError([...new Set(readErrors.map((error) => error.message))].join('\n'))
      }
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
        <EmptyState compact icon={<FolderOpen size={20} />} title="暂无活动对话" hint="开始对话后，文件与附件会显示在这里。" />
      </div>
    )
  }

  const generated = items.filter((item) => item.kind !== 'attachment')
  const attachments = items.filter((item) => item.kind === 'attachment')

  return (
    <div style={panelStyle}>
      <PanelHeader title="文件" />
      {executionCwd && !pendingConversationSwitchId && <FilesPanel />}
      <ArtifactSection title="生成文件" items={generated} />
      <ArtifactSection title="附件" items={attachments} />
      {loading && <p role="status" style={metaStyle}>正在读取历史资源…</p>}
      {loadError && <p role="alert" style={{ ...metaStyle, whiteSpace: 'normal' }}>{loadError}<button type="button" className="btn-ghost" onClick={() => setRefreshVersion((value) => value + 1)}>重新读取</button></p>}
      {currentInventory?.page.has_more && <button type="button" className="btn-ghost" disabled={loading} onClick={() => void loadMore()}>读取更多历史资源</button>}
      {!isConnected && <p style={metaStyle}>连接后端后可读取完整历史资源。</p>}
      {!executionCwd && items.length === 0 && !loading && !loadError && <EmptyState compact icon={<FileText size={18} />} title="暂无文件或附件" hint="生成文件或上传附件后会显示在这里。" />}
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
        workspaceRoot: item.workspaceRoot || conversation?.executionCwd || conversation?.worktreePath || conversation?.workspaceRoot
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
        return (
          <div key={item.id}>
          <ActivityButtonRow onClick={() => openItem(item)} title={item.path || item.detail || item.label}>
            <ActivityIcon><Icon size={14} /></ActivityIcon>
            <span style={{ flex: 1, minWidth: 0 }}>
              <span style={labelStyle}>{item.label}</span>
              <span style={metaStyle}>{[item.path, item.detail].filter(Boolean).join(' · ')}</span>
            </span>
            <ChevronRight size={14} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
          </ActivityButtonRow>
          {item.messageId && item.conversationId && <div style={{ display: 'flex', justifyContent: 'space-between', gap: 6, padding: '0 4px 5px 28px' }}>
            <span style={metaStyle}>{item.occurredAt ? new Date(item.occurredAt).toLocaleString() : ''}</span>
            <button type="button" className="activity-source-locate" onClick={() => revealConversationMessage(item.conversationId!, item.messageId!)}>回到对话</button>
          </div>}
          </div>
        )
      })}
    </ActivitySection>
  )
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
