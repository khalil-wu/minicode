/**
 * Context tab — captured files, sources, workspace state, and runtime details.
 */
import { ChevronRight, FileCode2, FileText, FileType, Folder, GitBranch, Image, MessageSquare, MonitorPlay, Layers, Paperclip, SquareTerminal, CalendarClock, Activity } from 'lucide-react'
import { useMemo } from 'react'
import { EmptyState } from '../../components/EmptyState'
import { useAppStore } from '../../stores'
import { openWebTarget } from '../../chat/openWebTarget'
import { openArtifactPreview, openAttachmentPreview, openWorkspaceFilePreview } from '../../chat/openAttachmentPreview'
import { hasVisibleActiveConversation } from '../../chat/activeConversation'
import { openAutomations } from '../../lib/automations-navigation'
import { BrandIcon } from '../../components/BrandIcon'
import { selectActiveConversationPreview } from '../../lib/preview-projection'
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
  InfoCard,
  InfoRow,
  PanelHeader,
  RowCard,
  StatusMark,
} from '../SidebarShared'

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
  const livePreviewUrl = useAppStore((s) => selectActiveConversationPreview(s).livePreviewUrl)
  const previewArtifact = useAppStore((s) => selectActiveConversationPreview(s).previewArtifact)
  const previewVerification = useAppStore((s) => selectActiveConversationPreview(s).previewVerification)
  const previewServers = useAppStore((s) => selectActiveConversationPreview(s).previewServers)
  const previewLaunchProcesses = useAppStore((s) => selectActiveConversationPreview(s).previewLaunchProcesses)
  const terminalSnapshots = useAppStore((s) => s.terminalSnapshots)
  const terminalSessions = useAppStore((s) => s.terminalSessions)
  const activeTerminalSessionId = useAppStore((s) => s.activeTerminalSessionId)
  const backgroundTasks = useAppStore((s) => s.backgroundTasks)
  const subagents = useAppStore((s) => s.subagents)
  const scheduledTasks = useAppStore((s) => s.scheduledTasks)
  const scheduledTaskRuns = useAppStore((s) => s.scheduledTaskRuns)
  const browserAnnotations = useAppStore((s) => s.browserAnnotations)
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
    scheduledTaskRuns,
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
    scheduledTaskRuns,
    browserAnnotations,
  ])

  if (!state.hasConversation) {
    return (
      <div style={activityPanelStyle}>
        <EmptyState compact icon={<MessageSquare size={20} />} title="暂无当前会话" hint="开始对话后，工具调用与产出会显示在这里。" />
      </div>
    )
  }

  const hasEvidence = state.output.length > 0 ||
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
        <EmptyState compact icon={<Activity size={20} />} title="暂无上下文信息" hint="Agent 工作时，读取的文件与执行的命令会显示在这里。" />
      </div>
    )
  }

  return (
    <div style={activityPanelStyle}>
      <ActivityWorkspaceSection items={state.workspace} />
      <ActivityAttachmentsSection items={state.attachments} workingDirectory={workingDirectory} />
      <ActivitySourcesSection items={state.sources} />
      <ActivityOutputSection items={state.output} />
      <ActivityRunsSection items={state.runs} />
      <ActivityBrowserAnnotationsSection items={state.browserAnnotations} />
      <ActivityBrowserSection items={state.browser} />
      <ActivitySummarySection items={state.summary} />
      <ActivityProgressSection items={state.progress} />
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
            label={item.kind === 'goal' ? '目标' : '上下文'}
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
    <div style={{ display: 'grid', gap: 4 }} aria-label="当前上下文">
      {items.map((item) => (
        <RowCard key={item.id} active={false}>
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
          <RowCard active={false}>
            <ActivityIcon>
              {item.kind === 'branch' ? <GitBranch size={14} /> : <Folder size={14} />}
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
      openArtifactPreview({
        artifactId: item.artifactId,
        name: item.label,
        mediaType: item.mediaType,
        kind: item.kind,
        conversationId: store.conversationId || undefined,
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
      openWebTarget(item.url)
    }
  }

  return (
    <ActivitySection title="输出" previewCount={3}>
      {items.map((item) => {
        const Icon = outputIcon(item)
        return (
          <ActivityButtonRow key={item.id} onClick={() => openOutput(item)} title={item.detail || item.label}>
            <ActivityIcon><Icon size={14} /></ActivityIcon>
            <span style={{ flex: 1, minWidth: 0 }}>
              <span style={activityButtonLabelStyle}>{item.label}</span>
              {item.detail ? <span style={activityMetaTextStyle}>{item.detail}</span> : null}
            </span>
            <ChevronRight size={14} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
          </ActivityButtonRow>
        )
      })}
    </ActivitySection>
  )
}

const ActivityBrowserSection = ({ items }: { items: ActivityBrowserItem[] }) => {
  const openBrowser = (item: ActivityBrowserItem) => {
    openWebTarget(item.url)
  }

  return (
    <ActivitySection title="浏览器" previewCount={2}>
      {items.map((item) => (
        <ActivityButtonRow key={item.id} onClick={() => openBrowser(item)} title={item.url}>
          <ActivityIcon><MonitorPlay size={14} /></ActivityIcon>
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
      openWebTarget(item.url)
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
    <ActivitySection title="页面备注" previewCount={4}>
      {items.map((item) => (
        <ActivityButtonRow key={item.id} onClick={() => openAnnotation(item)} title={item.note}>
          <ActivityIcon><MessageSquare size={14} /></ActivityIcon>
          <span style={{ flex: 1, minWidth: 0 }}>
            <span style={activityButtonLabelStyle}>{item.label}</span>
            <span style={activityMetaTextStyle}>{[
              item.selector || (item.xPercent != null && item.yPercent != null)
                ? item.host
                : item.detail || item.host,
              item.screenshotDetail,
            ].filter(Boolean).join(' - ')}</span>
          </span>
          <ChevronRight size={14} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
        </ActivityButtonRow>
      ))}
    </ActivitySection>
  )
}

const ActivityAttachmentsSection = ({
  items,
  workingDirectory,
}: {
  items: ActivityAttachmentItem[]
  workingDirectory: string
}) => {
  const openAttachment = (item: ActivityAttachmentItem) => {
    const store = useAppStore.getState()
    if (item.path) {
      openWorkspaceFilePreview({
        path: item.path,
        name: item.label,
        mediaType: item.mediaType,
        kind: item.kind,
        workspaceRoot: workingDirectory,
        conversationId: store.conversationId || undefined,
      })
      return
    }
    if (item.artifactId) {
      openAttachmentPreview({
        artifactId: item.artifactId,
        name: item.label,
        mediaType: item.mediaType,
        kind: item.kind,
        conversationId: store.conversationId || undefined,
      })
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
            title={item.detail || item.label}
          >
            <ActivityIcon><Icon size={14} /></ActivityIcon>
            <span style={{ flex: 1, minWidth: 0 }}>
              <span style={activityButtonLabelStyle}>{item.label}</span>
              <span style={activityMetaTextStyle}>
                {item.kind}{item.detail ? ` - ${item.detail}` : ''}
              </span>
            </span>
            {(item.artifactId || item.path) && <ChevronRight size={14} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />}
          </ActivityButtonRow>
        )
      })}
    </ActivitySection>
  )
}

const ActivityRunsSection = ({ items }: { items: ActivityRunItem[] }) => {
  const kinds = new Set(items.map((item) => item.kind))
  const title = kinds.size !== 1
    ? '运行项'
    : kinds.has('terminal')
      ? '终端'
      : kinds.has('background-command')
        ? '后台进程'
        : kinds.has('agent')
          ? '子智能体'
          : kinds.has('automation')
            ? '自动化'
            : kinds.has('preview')
              ? '预览服务'
              : '运行项'
  const openRun = (item: ActivityRunItem) => {
    const store = useAppStore.getState()
    if (item.kind === 'terminal') {
      if (item.terminalId) store.setActiveTerminalSession(item.terminalId)
      store.openBottomTab('terminal')
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
          <ActivityIcon>{item.kind === 'automation' ? <CalendarClock size={14} /> : item.kind === 'preview' ? <MonitorPlay size={14} /> : item.kind === 'agent' ? <Layers size={14} /> : <SquareTerminal size={14} />}</ActivityIcon>
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
    openWebTarget(item.url)
  }

  return (
    <ActivitySection title="来源" previewCount={5}>
      {items.slice(0, 10).map((item) => {
        const detail = sourceDetail(item)
        const body = (
          <>
            <ActivityIcon>
              {item.kind === 'file'
                ? <FileText size={14} />
                : <BrandIcon value={`${item.label} ${item.title || ''} ${item.url || ''}`} websiteUrl={item.url} fallback="web" size={14} />}
            </ActivityIcon>
            <span style={{ flex: 1, minWidth: 0 }}>
              <span style={activityButtonLabelStyle}>{item.label}</span>
              {detail ? <span style={activityMetaTextStyle}>{detail}</span> : null}
            </span>
            {item.kind === 'web' && <ChevronRight size={14} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />}
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
    return path ? compactPath(path) : '工作区文件'
  }
  if (item.title && item.title !== item.label) return item.title
  if (item.url) {
    try {
      const parsed = new URL(item.url)
      return parsed.pathname && parsed.pathname !== '/' ? parsed.pathname : parsed.hostname
    } catch {
      return item.url
    }
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
  if (status === 'verified') return '正常'
  if (status === 'failed') return '失败'
  if (status === 'running') return '运行中'
  return '空闲'
}

function statusLabel(status: string): string {
  if (status === 'completed') return '完成'
  if (status === 'failed') return '失败'
  if (status === 'blocked') return '受阻'
  if (status === 'partial') return '部分完成'
  if (status === 'stalled') return '等待输入'
  if (status === 'running') return '运行中'
  if (status === 'cancelled') return '已取消'
  return '信息'
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
  fontWeight: "var(--fw-semibold)",
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
  fontWeight: "var(--fw-semibold)",
}

const activityMetaTextStyle: React.CSSProperties = {
  display: 'block',
  marginTop: 2,
  color: 'var(--text-muted)',
  fontSize: "var(--text-3xs)",
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
  fontSize: "var(--text-3xs)",
  fontFamily: 'var(--font-mono)',
  color: status === 'verified'
    ? 'var(--state-success)'
    : status === 'failed'
      ? 'var(--state-danger)'
      : status === 'stalled'
        ? 'var(--state-warning)'
        : status === 'running'
          ? 'var(--state-info)'
          : 'var(--text-muted)',
  background: 'transparent',
})
