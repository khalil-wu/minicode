/**
 * Activity tab — shows progress, output, browser, and sources.
 */
import { ExternalLink, FileText, Image, Link, MonitorPlay, Box } from 'lucide-react'
import { useMemo } from 'react'
import { useAppStore } from '../../stores'
import { getWebSocket } from '../../hooks/useWebSocket'
import { hasVisibleActiveConversation } from '../../chat/activeConversation'
import {
  buildActivitySidebarState,
  type ActivityBrowserItem,
  type ActivityOutputItem,
  type ActivityProgressItem,
  type ActivitySourceItem,
} from '../activitySidebarState'
import {
  ActivityButtonRow,
  ActivityIcon,
  ActivitySection,
  EmptyLine,
  PanelHeader,
  StatusMark,
} from '../SidebarShared'

export const ActivityTab = () => {
  const conversationId = useAppStore((s) => s.conversationId)
  const hasActiveConversation = useAppStore((s) => hasVisibleActiveConversation(s.conversationId, s.conversations))
  const messages = useAppStore((s) => s.messages)
  const todos = useAppStore((s) => s.todos)
  const plan = useAppStore((s) => s.plan)
  const agentProgress = useAppStore((s) => s.agentProgress)
  const livePreviewUrl = useAppStore((s) => s.livePreviewUrl)
  const previewArtifact = useAppStore((s) => s.previewArtifact)
  const previewVerification = useAppStore((s) => s.previewVerification)
  const previewServers = useAppStore((s) => s.previewServers)
  const previewLaunchProcesses = useAppStore((s) => s.previewLaunchProcesses)

  const state = useMemo(() => buildActivitySidebarState({
    conversationId: hasActiveConversation ? conversationId : null,
    messages,
    todos,
    plan,
    agentProgress,
    livePreviewUrl,
    previewArtifact,
    previewVerification,
    previewServers,
    previewLaunchProcesses,
  }), [
    conversationId,
    hasActiveConversation,
    messages,
    todos,
    plan,
    agentProgress,
    livePreviewUrl,
    previewArtifact,
    previewVerification,
    previewServers,
    previewLaunchProcesses,
  ])

  if (!state.hasConversation) {
    return (
      <div style={activityPanelStyle}>
        <PanelHeader title="Activity" />
        <EmptyLine>No active conversation.</EmptyLine>
      </div>
    )
  }

  return (
    <div style={activityPanelStyle}>
      <ActivityProgressSection items={state.progress} />
      <ActivityOutputSection items={state.output} />
      <ActivityBrowserSection items={state.browser} />
      <ActivitySourcesSection items={state.sources} />
    </div>
  )
}

const ActivityProgressSection = ({ items }: { items: ActivityProgressItem[] }) => (
  <ActivitySection title="Progress" initialExpanded previewCount={4}>
    {items.map((item) => (
      <div key={item.id} style={activityRowStyle(item.status === 'running')}>
        <StatusMark status={item.status} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div title={item.label} style={activityTitleStyle(item.status === 'completed')}>
            {item.label}
          </div>
          {item.detail && <div title={item.detail} style={activityMetaTextStyle}>{item.detail}</div>}
        </div>
      </div>
    ))}
  </ActivitySection>
)

const ActivityOutputSection = ({ items }: { items: ActivityOutputItem[] }) => {
  const openOutput = (item: ActivityOutputItem) => {
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
    <ActivitySection title="Output" previewCount={3}>
      {items.map((item) => {
        const Icon = outputIcon(item)
        return (
          <ActivityButtonRow key={item.id} onClick={() => openOutput(item)} title={item.detail || item.label}>
            <ActivityIcon><Icon size={13} /></ActivityIcon>
            <span style={{ flex: 1, minWidth: 0 }}>
              <span style={activityButtonLabelStyle}>{item.label}</span>
              <span style={activityMetaTextStyle}>{item.kind}{item.detail ? ` - ${item.detail}` : ''}</span>
            </span>
            <ExternalLink size={12} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
          </ActivityButtonRow>
        )
      })}
    </ActivitySection>
  )
}

const ActivityBrowserSection = ({ items }: { items: ActivityBrowserItem[] }) => {
  const openBrowser = (item: ActivityBrowserItem) => {
    const store = useAppStore.getState()
    store.openLivePreview(item.url)
    store.setRightStackTab('preview')
  }

  return (
    <ActivitySection title="Browser" previewCount={2}>
      {items.map((item) => (
        <ActivityButtonRow key={item.id} onClick={() => openBrowser(item)} title={item.url}>
          <ActivityIcon><MonitorPlay size={13} /></ActivityIcon>
          <span style={{ flex: 1, minWidth: 0 }}>
            <span style={activityButtonLabelStyle}>{item.label}</span>
            <span style={activityMetaTextStyle}>{item.host}{item.detail ? ` - ${item.detail}` : ''}</span>
          </span>
          <span style={activityStatusPillStyle(item.status)}>{browserStatusLabel(item.status)}</span>
        </ActivityButtonRow>
      ))}
    </ActivitySection>
  )
}

const ActivitySourcesSection = ({ items }: { items: ActivitySourceItem[] }) => (
  <ActivitySection title="Sources" previewCount={5}>
    {items.slice(0, 10).map((item) => {
      const Icon = item.kind === 'file' ? FileText : Link
      const detail = sourceDetail(item)
      const body = (
        <>
          <ActivityIcon><Icon size={13} /></ActivityIcon>
          <span style={{ flex: 1, minWidth: 0 }}>
            <span style={activityButtonLabelStyle}>{item.label}</span>
            <span style={activityMetaTextStyle}>{detail}</span>
          </span>
          {item.kind === 'web' && <ExternalLink size={12} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />}
        </>
      )

      if (item.kind === 'file' && item.path) {
        return (
          <button
            key={item.id}
            type="button"
            onClick={() => useAppStore.getState().openEditorFile(item.path!, item.label)}
            style={activityButtonRowStyle}
            title={item.path}
          >
            {body}
          </button>
        )
      }

      return (
        <a key={item.id} href={item.url} target="_blank" rel="noreferrer" style={activityButtonRowStyle} title={item.url}>
          {body}
        </a>
      )
    })}
  </ActivitySection>
)

function sourceDetail(item: ActivitySourceItem): string {
  if (item.kind === 'file') {
    const path = item.path || [item.title, item.label].filter(Boolean).join('/')
    return path ? compactPath(path) : 'workspace file'
  }
  return item.title || item.host || item.url || 'web source'
}

function compactPath(path: string): string {
  const normalized = path.replace(/\\/g, '/')
  const parts = normalized.split('/').filter(Boolean)
  if (parts.length <= 3) return normalized
  return `.../${parts.slice(-3).join('/')}`
}

function outputIcon(item: ActivityOutputItem) {
  if (item.kind === 'image' || item.mediaType?.startsWith('image/')) return Image
  if (item.kind === 'file' || item.kind === 'text' || item.kind === 'code' || item.kind === 'pdf') return FileText
  return Box
}

function browserStatusLabel(status: ActivityBrowserItem['status']): string {
  if (status === 'verified') return 'ok'
  if (status === 'failed') return 'fail'
  if (status === 'running') return 'run'
  return 'idle'
}

// ── Styles ───────────────────────────────────────────────────────

const activityPanelStyle: React.CSSProperties = {
  display: 'grid',
  gap: 7,
}

const activityRowStyle = (active: boolean): React.CSSProperties => ({
  display: 'flex',
  alignItems: 'flex-start',
  gap: 8,
  minWidth: 0,
  minHeight: 24,
  padding: '4px 0',
  borderTop: '1px solid var(--border-subtle)',
  background: active ? 'color-mix(in oklch, var(--accent-orange) 6%, transparent)' : 'transparent',
})

const activityTitleStyle = (done: boolean): React.CSSProperties => ({
  color: done ? 'var(--text-muted)' : 'var(--text-primary)',
  textDecoration: done ? 'line-through' : 'none',
  fontSize: 'var(--text-xs)',
  lineHeight: 1.45,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
})

const activityButtonLabelStyle: React.CSSProperties = {
  display: 'block',
  color: 'var(--text-primary)',
  fontSize: 'var(--text-xs)',
  lineHeight: 1.3,
  fontWeight: 650,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
}

const activityMetaTextStyle: React.CSSProperties = {
  display: 'block',
  marginTop: 2,
  color: 'var(--text-muted)',
  fontSize: 10,
  lineHeight: 1.2,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
}

const activityStatusPillStyle = (status: ActivityBrowserItem['status']): React.CSSProperties => ({
  flexShrink: 0,
  minWidth: 24,
  height: 17,
  padding: '0 5px',
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  border: '1px solid var(--border-subtle)',
  borderRadius: 'var(--radius-sm, 4px)',
  fontSize: 10,
  fontFamily: 'var(--font-mono)',
  color: status === 'verified'
    ? 'var(--state-success)'
    : status === 'failed'
      ? 'var(--state-danger)'
      : status === 'running'
        ? 'var(--state-info)'
        : 'var(--text-muted)',
  background: 'transparent',
})

const activityButtonRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  minWidth: 0,
  minHeight: 28,
  width: '100%',
  padding: '4px 0',
  border: 0,
  borderTop: '1px solid var(--border-subtle)',
  borderRadius: 0,
  background: 'transparent',
  color: 'var(--text-primary)',
  cursor: 'pointer',
  textAlign: 'left',
  textDecoration: 'none',
}
