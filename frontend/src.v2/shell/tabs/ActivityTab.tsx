/**
 * Activity tab — compact runtime center for task progress, sources, and run context.
 */
import { ChevronRight, FileCode2, FileText, FileType, Folder, GitBranch, Image, Link, MonitorPlay, Layers, Paperclip, SquareTerminal, MessageSquare, CalendarClock } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useAppStore } from '../../stores'
import { getWebSocket } from '../../hooks/useWebSocket'
import { openWebInPreview } from '../../chat/openWebInPreview'
import { hasVisibleActiveConversation } from '../../chat/activeConversation'
import { openAutomations } from '../../lib/automations-navigation'
import { ImageLightbox } from '../../components/ImageLightbox'
import { previewUrlForPath } from '../fileTreeHelpers'
import {
  buildActivitySidebarState,
  type ActivityBrowserAnnotationItem,
  type ActivityAttachmentItem,
  type ActivityBrowserItem,
  type ActivityOutputItem,
  type ActivityProgressItem,
  type ActivityRunItem,
  type ActivitySummaryItem,
  type ActivitySourceItem,
  type ActivityWorkspaceItem,
} from '../activitySidebarState'
import {
  ActivityButtonRow,
  ActivityIcon,
  ActivitySection,
  EmptyLine,
  InfoCard,
  InfoRow,
  PanelHeader,
  RowCard,
  StatusMark,
} from '../SidebarShared'
import { projectMessagesToActivityItems, visibleActivityItems } from '../../agent-loop/projection/project-activity-items'
import type { ActivityItem } from '../../agent-loop/activity-item'

export const ActivityTab = () => {
  const conversationId = useAppStore((s) => s.conversationId)
  const hasActiveConversation = useAppStore((s) => hasVisibleActiveConversation(s.conversationId, s.conversations))
  const messages = useAppStore((s) => s.messages)
  const isStreaming = useAppStore((s) => s.isStreaming)
  const todos = useAppStore((s) => s.todos)
  const plan = useAppStore((s) => s.plan)
  const agentProgress = useAppStore((s) => s.agentProgress)
  const activeGoal = useAppStore((s) => s.activeGoal)
  const contextUsage = useAppStore((s) => s.contextUsage)
  const currentModel = useAppStore((s) => s.currentModel)
  const currentProvider = useAppStore((s) => s.currentProvider)
  const workspaceGit = useAppStore((s) => s.workspaceGit)
  const workingDirectory = useAppStore((s) => s.workingDirectory)
  const livePreviewUrl = useAppStore((s) => s.livePreviewUrl)
  const previewArtifact = useAppStore((s) => s.previewArtifact)
  const previewVerification = useAppStore((s) => s.previewVerification)
  const previewServers = useAppStore((s) => s.previewServers)
  const previewLaunchProcesses = useAppStore((s) => s.previewLaunchProcesses)
  const terminalSnapshots = useAppStore((s) => s.terminalSnapshots)
  const terminalSessions = useAppStore((s) => s.terminalSessions)
  const activeTerminalSessionId = useAppStore((s) => s.activeTerminalSessionId)
  const backgroundTasks = useAppStore((s) => s.backgroundTasks)
  const subagents = useAppStore((s) => s.subagents)
  const scheduledTasks = useAppStore((s) => s.scheduledTasks)
  const browserAnnotations = useAppStore((s) => s.browserAnnotations)
  const activityItems = useMemo(
    () => projectMessagesToActivityItems(messages, { isStreaming }),
    [isStreaming, messages],
  )

  const state = useMemo(() => buildActivitySidebarState({
    conversationId: hasActiveConversation ? conversationId : null,
    messages,
    isStreaming,
    todos,
    plan,
    agentProgress,
    activeGoal,
    contextUsage,
    currentModel,
    currentProvider,
    workspaceGit,
    workingDirectory,
    livePreviewUrl,
    previewArtifact,
    previewVerification,
    previewServers,
    previewLaunchProcesses,
    terminalSnapshots,
    terminalSessions,
    activeTerminalSessionId,
    backgroundTasks,
    subagents,
    scheduledTasks,
    browserAnnotations,
  }), [
    conversationId,
    hasActiveConversation,
    messages,
    isStreaming,
    todos,
    plan,
    agentProgress,
    activeGoal,
    contextUsage,
    currentModel,
    currentProvider,
    workspaceGit,
    workingDirectory,
    livePreviewUrl,
    previewArtifact,
    previewVerification,
    previewServers,
    previewLaunchProcesses,
    terminalSnapshots,
    terminalSessions,
    activeTerminalSessionId,
    backgroundTasks,
    subagents,
    scheduledTasks,
    browserAnnotations,
  ])

  if (!state.hasConversation) {
    return (
      <div style={activityPanelStyle}>
        <EmptyLine>No active conversation.</EmptyLine>
      </div>
    )
  }

  const hasEvidence = state.output.length > 0 ||
    activityItems.length > 0 ||
    state.summary.length > 0 ||
    state.workspace.length > 0 ||
    state.progress.length > 0 ||
    state.sources.length > 0 ||
    state.attachments.length > 0 ||
    state.runs.length > 0 ||
    state.browserAnnotations.length > 0 ||
    state.browser.length > 0

  if (!hasEvidence) {
    return (
      <div style={activityPanelStyle}>
        <EmptyLine>No outputs or sources yet.</EmptyLine>
      </div>
    )
  }

  return (
    <div style={activityPanelStyle}>
      <ActivitySummarySection items={state.summary} />
      <ActivityProgressSection items={state.progress} />
      <CanonicalActivitySection items={activityItems} />
      <ActivityWorkspaceSection items={state.workspace} />
      <ActivityOutputSection items={state.output} />
      <ActivitySourcesSection items={state.sources} />
      <ActivityAttachmentsSection items={state.attachments} />
      <ActivityRunsSection items={state.runs} />
      <ActivityBrowserAnnotationsSection items={state.browserAnnotations} />
      <ActivityBrowserSection items={state.browser} />
    </div>
  )
}

const CanonicalActivitySection = ({ items }: { items: ActivityItem[] }) => {
  const visible = visibleActivityItems(items)
  if (visible.length === 0) return null
  return (
    <div style={{ display: 'grid', gap: 4 }} aria-label="Turn activity">
      <PanelHeader title="活动" />
      {visible.map((item) => (
        <RowCard key={`${item.messageId || item.turnId || 'turn'}:${item.id}`} active={item.status === 'running' || item.status === 'failed' || item.status === 'blocked'}>
          <StatusMark status={item.status} />
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={activityButtonLabelStyle}>{item.title}</div>
            {item.summary ? <div style={activityMetaTextStyle}>{item.summary}</div> : null}
          </div>
        </RowCard>
      ))}
    </div>
  )
}

const ActivitySummarySection = ({ items }: { items: ActivitySummaryItem[] }) => {
  if (items.length === 0) return null
  return (
    <div style={{ display: 'grid', gap: 4 }}>
      <PanelHeader title="任务" />
      <InfoCard>
        {items.map((item) => (
          <InfoRow
            key={item.id}
            label={item.kind === 'goal' ? 'Goal' : 'Context'}
            value={item.detail ? `${item.label} - ${item.detail}` : item.label}
            tone={item.status === 'failed' ? 'warning' : item.status === 'running' ? 'accent' : 'muted'}
          />
        ))}
      </InfoCard>
    </div>
  )
}

const ActivityProgressSection = ({ items }: { items: ActivityProgressItem[] }) => {
  if (items.length === 0) return null
  return (
    <div style={{ display: 'grid', gap: 4 }} aria-label="Current activity">
      {items.map((item) => (
        <RowCard key={item.id} active={item.status === 'running' || item.status === 'failed'}>
          <StatusMark status={item.status} />
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={activityButtonLabelStyle}>{item.label}</div>
            {item.detail ? <div style={activityMetaTextStyle}>{item.detail}</div> : null}
          </div>
        </RowCard>
      ))}
    </div>
  )
}

const ActivityWorkspaceSection = ({ items }: { items: ActivityWorkspaceItem[] }) => {
  const visibleItems = items.filter((item) => (
    item.kind !== 'worktree' || item.label !== 'Main workspace' || item.status === 'failed'
  ))
  if (visibleItems.length === 0) return null
  return (
    <div style={{ display: 'grid', gap: 4 }}>
      {visibleItems.map((item) => (
        <div key={item.id} title={item.detail || item.label}>
          <RowCard active={item.status === 'failed' || item.status === 'running'}>
            <ActivityIcon>
              {item.kind === 'branch' ? <GitBranch size={13} /> : <Folder size={13} />}
            </ActivityIcon>
            <span style={activityWorkspaceValueStyle}>{item.label}</span>
          </RowCard>
        </div>
      ))}
    </div>
  )
}

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
      openWebInPreview(item.url)
    }
  }

  return (
    <ActivitySection title="输出" previewCount={3}>
      {items.map((item) => {
        const Icon = outputIcon(item)
        return (
          <ActivityButtonRow key={item.id} onClick={() => openOutput(item)} title={item.detail || item.label}>
            <ActivityIcon><Icon size={13} /></ActivityIcon>
            <span style={{ flex: 1, minWidth: 0 }}>
              <span style={activityButtonLabelStyle}>{item.label}</span>
              {item.detail ? <span style={activityMetaTextStyle}>{item.detail}</span> : null}
            </span>
            <ChevronRight size={12} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
          </ActivityButtonRow>
        )
      })}
    </ActivitySection>
  )
}

const ActivityBrowserSection = ({ items }: { items: ActivityBrowserItem[] }) => {
  const openBrowser = (item: ActivityBrowserItem) => {
    openWebInPreview(item.url)
  }

  return (
    <ActivitySection title="浏览器" previewCount={2}>
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

const ActivityBrowserAnnotationsSection = ({ items }: { items: ActivityBrowserAnnotationItem[] }) => {
  const openAnnotation = (item: ActivityBrowserAnnotationItem) => {
    const store = useAppStore.getState()
    if (item.url) {
      openWebInPreview(item.url)
    }
    store.addInspectorEntry({
      targetKind: 'message',
      targetId: item.id,
      payload: {
        kind: 'browser_annotation',
        id: item.id,
        label: item.label,
        url: item.url,
        host: item.host,
        title: item.title,
        selector: item.selector,
        xPercent: item.xPercent,
        yPercent: item.yPercent,
        note: item.note,
        createdAt: item.createdAt,
        screenshot: item.screenshotDetail,
      },
      timestamp: Date.now(),
    })
    store.setInspectorFocus({ kind: 'browser_annotation', id: item.id })
    store.setRightStackTab('inspector')
  }

  return (
    <ActivitySection title="页面记录" previewCount={4}>
      {items.map((item) => (
        <ActivityButtonRow key={item.id} onClick={() => openAnnotation(item)} title={item.note}>
          <ActivityIcon><MessageSquare size={13} /></ActivityIcon>
          <span style={{ flex: 1, minWidth: 0 }}>
            <span style={activityButtonLabelStyle}>{item.label}</span>
            <span style={activityMetaTextStyle}>{[
              item.selector || (item.xPercent != null && item.yPercent != null)
                ? item.host
                : item.detail || item.host,
              item.screenshotDetail,
            ].filter(Boolean).join(' - ')}</span>
          </span>
          <ChevronRight size={12} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
        </ActivityButtonRow>
      ))}
    </ActivitySection>
  )
}

const ActivityAttachmentsSection = ({ items }: { items: ActivityAttachmentItem[] }) => {
  const [preview, setPreview] = useState<{ src: string; name: string } | null>(null)
  const [pendingPreviewArtifactId, setPendingPreviewArtifactId] = useState<string | null>(null)
  const [failedPreviewArtifactId, setFailedPreviewArtifactId] = useState<string | null>(null)
  const pendingPreviewArtifactIdRef = useRef<string | null>(null)

  useEffect(() => {
    const onArtifactImagePreview = (event: Event) => {
      const detail = (event as CustomEvent<{ artifactId?: string; url?: string }>).detail
      const pendingArtifactId = pendingPreviewArtifactIdRef.current
      if (!pendingArtifactId || detail?.artifactId !== pendingArtifactId || !detail.url) return
      const attachment = items.find((item) => item.artifactId === pendingArtifactId)
      setPreview({ src: detail.url, name: attachment?.label || 'image' })
      pendingPreviewArtifactIdRef.current = null
      setPendingPreviewArtifactId(null)
      setFailedPreviewArtifactId(null)
    }
    window.addEventListener('artifact:image-preview', onArtifactImagePreview)
    return () => window.removeEventListener('artifact:image-preview', onArtifactImagePreview)
  }, [items])

  useEffect(() => {
    if (!pendingPreviewArtifactId) return
    const artifactId = pendingPreviewArtifactId
    const timeout = window.setTimeout(() => {
      if (pendingPreviewArtifactIdRef.current !== artifactId) return
      pendingPreviewArtifactIdRef.current = null
      setPendingPreviewArtifactId(null)
      setFailedPreviewArtifactId(artifactId)
    }, 10_000)
    return () => window.clearTimeout(timeout)
  }, [pendingPreviewArtifactId])

  const openAttachment = (item: ActivityAttachmentItem) => {
    const store = useAppStore.getState()
    const isImage = item.kind === 'image' || item.mediaType?.startsWith('image/')
    if (isImage) {
      if (item.path) {
        setPreview({ src: previewUrlForPath(item.path), name: item.label })
        return
      }
      if (item.artifactId) {
        setFailedPreviewArtifactId(null)
        pendingPreviewArtifactIdRef.current = item.artifactId
        setPendingPreviewArtifactId(item.artifactId)
        getWebSocket()?.send({ type: 'read_artifact', artifact_id: item.artifactId, purpose: 'image_preview' })
        return
      }
    }
    if (item.path) {
      store.openEditorFile(item.path, item.label)
      return
    }
    if (item.artifactId) {
      store.addInspectorEntry({
        targetKind: 'message',
        targetId: item.messageId,
        payload: {
          kind: 'attachment',
          attachmentId: item.id,
          messageId: item.messageId,
          label: item.label,
          attachmentKind: item.kind,
          detail: item.detail,
          artifactId: item.artifactId,
          docId: item.docId,
          mediaType: item.mediaType,
        },
        timestamp: Date.now(),
      })
      store.setInspectorFocus({ kind: 'attachment', id: item.id })
      store.setRightStackTab('inspector')
      return
    }
    store.addInspectorEntry({
      targetKind: 'message',
      targetId: item.messageId,
      payload: {
        kind: 'attachment',
        attachmentId: item.id,
        messageId: item.messageId,
        label: item.label,
        attachmentKind: item.kind,
        detail: item.detail,
        docId: item.docId,
        mediaType: item.mediaType,
      },
      timestamp: Date.now(),
    })
    store.setInspectorFocus({ kind: 'attachment', id: item.id })
    store.setRightStackTab('inspector')
  }

  return (
    <ActivitySection title="附件" previewCount={4}>
      {items.map((item) => {
        const Icon = item.kind === 'image' ? Image : Paperclip
        return (
          <ActivityButtonRow
            key={item.id}
            onClick={() => openAttachment(item)}
            title={failedPreviewArtifactId === item.artifactId ? '图片加载失败，点击重试' : item.detail || item.label}
          >
            <ActivityIcon><Icon size={13} /></ActivityIcon>
            <span style={{ flex: 1, minWidth: 0 }}>
              <span style={activityButtonLabelStyle}>{item.label}</span>
              <span style={activityMetaTextStyle}>
                {pendingPreviewArtifactId === item.artifactId
                  ? 'loading'
                  : failedPreviewArtifactId === item.artifactId
                    ? 'load failed'
                    : item.kind}{item.detail ? ` - ${item.detail}` : ''}
              </span>
            </span>
            {(item.artifactId || item.path) && <ChevronRight size={12} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />}
          </ActivityButtonRow>
        )
      })}
      {preview ? (
        <ImageLightbox
          src={preview.src}
          alt={preview.name}
          title={preview.name}
          onClose={() => setPreview(null)}
        />
      ) : null}
    </ActivitySection>
  )
}

const ActivityRunsSection = ({ items }: { items: ActivityRunItem[] }) => {
  const title = items.every((item) => item.kind === 'terminal') ? 'Terminal' : 'Processes'
  const openRun = (item: ActivityRunItem) => {
    const store = useAppStore.getState()
    if (item.kind === 'terminal') {
      if (item.terminalId) store.setActiveTerminalSession(item.terminalId)
      store.setRightStackTab('terminal')
      return
    }
    if (item.kind === 'automation') {
      openAutomations()
      return
    }
    if (item.kind === 'preview') {
      store.setRightStackTab('preview')
      return
    }
    if (item.kind === 'agent') {
      if (item.agentId) store.setFocusedSubagentId(item.agentId)
      store.setRightStackTab('subagents')
      return
    }
    store.setRightStackTab('tasks')
  }

  return (
    <ActivitySection title={title} previewCount={4}>
      {items.map((item) => (
        <ActivityButtonRow key={item.id} onClick={() => openRun(item)} title={item.detail || item.label}>
          <ActivityIcon>{item.kind === 'automation' ? <CalendarClock size={13} /> : item.kind === 'preview' ? <MonitorPlay size={13} /> : item.kind === 'agent' ? <Layers size={13} /> : <SquareTerminal size={13} />}</ActivityIcon>
          <span style={{ flex: 1, minWidth: 0 }}>
              <span style={activityButtonLabelStyle}>{item.label}</span>
              {item.detail ? <span style={activityMetaTextStyle}>{item.detail}</span> : null}
          </span>
          <span style={activityStatusPillStyle(item.status)}>{statusLabel(item.status)}</span>
        </ActivityButtonRow>
      ))}
    </ActivitySection>
  )
}

const ActivitySourcesSection = ({ items }: { items: ActivitySourceItem[] }) => {
  const openWebSource = (item: ActivitySourceItem) => {
    if (!item.url) return
    openWebInPreview(item.url)
  }

  return (
    <ActivitySection title="来源" previewCount={5}>
      {items.slice(0, 10).map((item) => {
        const Icon = item.kind === 'file' ? FileText : Link
        const detail = sourceDetail(item)
        const body = (
          <>
            <ActivityIcon><Icon size={13} /></ActivityIcon>
            <span style={{ flex: 1, minWidth: 0 }}>
              <span style={activityButtonLabelStyle}>{item.label}</span>
              {detail ? <span style={activityMetaTextStyle}>{detail}</span> : null}
            </span>
            {item.kind === 'web' && <ChevronRight size={12} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />}
          </>
        )

        return (
          <ActivityButtonRow
            key={item.id}
            onClick={item.kind === 'file' && item.path
              ? () => useAppStore.getState().openEditorFile(item.path!, item.label)
              : item.kind === 'web' && item.url
                ? () => openWebSource(item)
                : undefined}
            title={item.kind === 'file' ? item.path : item.url}
          >
            {body}
          </ActivityButtonRow>
        )
      })}
    </ActivitySection>
  )
}

function sourceDetail(item: ActivitySourceItem): string {
  if (item.kind === 'file') {
    const path = item.path || [item.title, item.label].filter(Boolean).join('/')
    return path ? compactPath(path) : 'workspace file'
  }
  return ''
}

function compactPath(path: string): string {
  const normalized = path.replace(/\\/g, '/')
  const parts = normalized.split('/').filter(Boolean)
  if (parts.length <= 3) return normalized
  return `.../${parts.slice(-3).join('/')}`
}

function outputIcon(item: ActivityOutputItem) {
  if (item.kind === 'image' || item.mediaType?.startsWith('image/')) return Image
  if (item.kind === 'pdf' || item.mediaType === 'application/pdf') return FileType
  if (item.kind === 'code' || item.mediaType?.includes('javascript') || item.mediaType?.includes('typescript') || item.mediaType?.includes('python')) return FileCode2
  if (item.kind === 'file' || item.kind === 'text') return FileText
  return Layers
}

function browserStatusLabel(status: ActivityBrowserItem['status']): string {
  if (status === 'verified') return 'ok'
  if (status === 'failed') return 'fail'
  if (status === 'running') return 'run'
  return 'idle'
}

function statusLabel(status: string): string {
  if (status === 'completed') return 'ok'
  if (status === 'failed') return 'fail'
  if (status === 'blocked') return 'block'
  if (status === 'partial') return 'part'
  if (status === 'running') return 'run'
  return 'info'
}

// ── Styles ───────────────────────────────────────────────────────

const activityPanelStyle: React.CSSProperties = {
  display: 'grid',
  gap: 7,
}

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

const activityWorkspaceValueStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  color: 'var(--text-secondary)',
  fontSize: 'var(--text-xs)',
  fontWeight: 600,
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

const activityStatusPillStyle = (status: string): React.CSSProperties => ({
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
