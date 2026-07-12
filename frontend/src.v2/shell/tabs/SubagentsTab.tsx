/**
 * Readable view of delegated work and workflows.
 */
import { ArrowLeft, ChevronDown, ChevronRight, GitBranch, PlayCircle, Square } from 'lucide-react'
import { useState, type CSSProperties } from 'react'
import { useAppStore } from '../../stores'
import type { SubagentState } from '../../stores/types'
import { sendClientCommand } from '../../protocol/ws-outbox'
import { sendChatMessage } from '../../chat/sendChatMessage'
import { EmptyLine, PanelHeader, SmallButton, StatusMark, statusColor } from '../SidebarShared'
import { MarkdownRenderer } from '../../chat/messages/MarkdownRenderer'
import { projectAllAgentViews, type AgentView, type CoordinatorNoticeKind } from '../../lib/agent-view-model'
import { coordinatorNoticeKind, userFacingCoordinatorNotice } from '../../lib/collaborationDisplay'
import './SubagentsTab.css'

const WORKFLOW_CHILD_PREVIEW_LIMIT = 24
const UNGROUPED_PREVIEW_LIMIT = 36
const WORKFLOW_STRIP_PREVIEW_LIMIT = 32

const isWorkflowView = (view: AgentView): boolean => view.isWorkflow

const isActionableView = (view: AgentView): boolean =>
  view.id.startsWith('subagent-')

const isDisplayableView = (view: AgentView): boolean =>
  view.source.role !== 'message' || Boolean(view.resultContent || view.resultError || view.activity)

const statusLabelForView = (view: AgentView): string => view.statusLabel

const modeLabel = (mode?: string): string => {
  switch ((mode || '').toLowerCase()) {
    case 'parallel':
      return '并行'
    case 'pipeline':
      return ''
    case 'phases':
      return '分阶段'
    default:
      return mode || '工作流'
  }
}

const compactList = (items?: string[], limit = 2): string => {
  if (!items?.length) return ''
  const visible = items.slice(0, limit).join(', ')
  return items.length > limit ? `${visible} +${items.length - limit}` : visible
}

const workflowStatusCounts = (children: AgentView[]) => ({
  running: children.filter((child) => child.effectiveStatus === 'running').length,
  pending: children.filter((child) => child.effectiveStatus === 'pending').length,
  blocked: children.filter((child) => child.effectiveStatus === 'blocked').length,
  done: children.filter((child) => child.effectiveStatus === 'done').length,
  error: children.filter((child) => child.effectiveStatus === 'error').length,
})

const workflowStatusMeta = (children: AgentView[]): string => {
  const counts = workflowStatusCounts(children)
  const collectBlocked = children.filter((child) =>
    child.effectiveStatus === 'blocked' &&
    child.coordinatorNoticeKind === 'collect_results'
  ).length
  const dependencyBlocked = children.filter((child) =>
    child.effectiveStatus === 'blocked' &&
    child.coordinatorNoticeKind !== 'collect_results' &&
    Boolean(child.blockedBy?.length)
  ).length
  const otherBlocked = Math.max(0, counts.blocked - collectBlocked - dependencyBlocked)
  return [
    counts.running ? `${counts.running} 运行` : '',
    counts.pending ? `${counts.pending} 等待` : '',
    collectBlocked ? `${collectBlocked} 整理中` : '',
    dependencyBlocked ? `${dependencyBlocked} 依赖` : '',
    otherBlocked ? `${otherBlocked} 待处理` : '',
    counts.done ? `${counts.done} 完成` : '',
    counts.error ? `${counts.error} 失败` : '',
    `${children.length} 项`,
  ].filter(Boolean).join(' · ')
}

const workflowNextAction = (children: AgentView[]): string => {
  if (children.length === 0) return ''
  const counts = workflowStatusCounts(children)
  if (counts.error) return `${counts.error} 项需要处理`
  const collectBlocked = children.filter((child) =>
    child.effectiveStatus === 'blocked' &&
    child.coordinatorNoticeKind === 'collect_results'
  ).length
  if (collectBlocked) return '结果正在整理'
  const dependencyBlocked = children.filter((child) =>
    child.effectiveStatus === 'blocked' &&
    child.coordinatorNoticeKind !== 'collect_results' &&
    Boolean(child.blockedBy?.length)
  ).length
  if (dependencyBlocked) return `${dependencyBlocked} 项暂时等待`
  if (counts.running) return `${counts.running} 项正在处理`
  if (counts.pending) return `${counts.pending} 项等待开始`
  if (counts.done === children.length) return '全部完成'
  return ''
}

const primaryWorkItems = (items: AgentView[]): AgentView[] => {
  const workers = items.filter((view) => !view.isWorkflow)
  return workers.length > 0 ? workers : items
}

const collaborationSummary = (items: AgentView[]): { title: string; meta: string; tone: SubagentState['status'] } => {
  const workItems = primaryWorkItems(items)
  const running = workItems.filter((view) => view.effectiveStatus === 'running').length
  const pending = workItems.filter((view) => view.effectiveStatus === 'pending').length
  const blocked = workItems.filter((view) => view.effectiveStatus === 'blocked').length
  const done = workItems.filter((view) => view.effectiveStatus === 'done').length
  const errors = workItems.filter((view) => view.effectiveStatus === 'error').length
  const active = running + pending + blocked
  const workflowCount = items.filter((view) => view.isWorkflow).length
  const workerCount = workItems.filter((view) => !view.isWorkflow).length
  const blockedNotices = workItems
    .map((view) => view.coordinatorNoticeKind)
    .filter((kind): kind is NonNullable<CoordinatorNoticeKind> => Boolean(kind))
  let title = `${done} 项已完成`
  if (errors) {
    title = `${errors} 项需要处理`
  } else if (blocked && blockedNotices.includes('collect_results')) {
    title = '结果正在整理'
  } else if (blocked && blockedNotices.includes('duplicate_delegation')) {
    title = '任务已在处理中'
  } else if (blocked && blockedNotices.includes('capacity')) {
    title = '任务较多，正在整理'
  } else if (blocked) {
    title = `${blocked} 项等待依赖`
  } else if (active) {
    title = `${active} 项正在处理`
  }
  const meta = [
    workerCount ? `${workerCount} 项任务` : '',
    done ? `${done} 完成` : '',
    running ? `${running} 运行` : '',
  ].filter(Boolean).join(' · ')
  return {
    title,
    meta,
    tone: errors ? 'error' : blocked ? 'blocked' : active ? 'running' : 'done',
  }
}

const canSummarizeCollaboration = (items: AgentView[]): boolean => {
  const workItems = primaryWorkItems(items)
  if (workItems.length === 0) return false
  let hasReadyResult = false
  let hasCollectNotice = false
  for (const view of workItems) {
    const status = view.effectiveStatus
    const notice = view.coordinatorNoticeKind
    if (status === 'running' || status === 'pending') return false
    if (status === 'blocked' && notice !== 'collect_results') return false
    if (notice === 'collect_results') hasCollectNotice = true
    if (
      status === 'done' &&
      (view.resultAvailable || view.resultContent || view.resultError || view.summary)
    ) {
      hasReadyResult = true
    }
    if (status === 'error' && (view.resultError || view.resultContent || view.summary)) {
      hasReadyResult = true
    }
  }
  return hasReadyResult || hasCollectNotice
}

const summarizeCollaborationResults = () => {
  sendChatMessage({
    displayContent: '汇总分工结果',
    backendContent: (
      '请用 agents/workflow 的现有分工上下文汇总当前分工执行结果。'
      + '不要启动重复分工；先收集所有已完成执行项和 workflow 节点的可用结果。'
      + '如果仍有必要执行项在运行或等待，请只给当前进度，不要声称已经完成。'
      + '如果结果齐全，请直接输出面向用户的最终结论和必要后续建议。'
    ),
  })
}

const workflowNodeLabel = (view: AgentView): string =>
  stripWorkflowPrefix(view.title || view.nodeId || view.taskId || view.id, view.workflowName)

const stableViewOrder = (left: AgentView, right: AgentView): number => {
  const leftOrder = typeof left.order === 'number' ? left.order : Number.POSITIVE_INFINITY
  const rightOrder = typeof right.order === 'number' ? right.order : Number.POSITIVE_INFINITY
  if (leftOrder !== rightOrder) return leftOrder - rightOrder
  return 0
}

const priorityStatusRank = (view: AgentView): number => {
  switch (view.effectiveStatus) {
    case 'error':
      return 0
    case 'running':
      return 1
    case 'blocked':
      return 2
    case 'pending':
      return 3
    default:
      return 4
  }
}

const recentActivityScore = (view: AgentView): number =>
  view.lastProgressAt || view.lastEventAt || 0

const limitedViewWindow = (items: AgentView[], limit: number, expanded: boolean): { visible: AgentView[]; hiddenCount: number } => {
  if (expanded || items.length <= limit) return { visible: items, hiddenCount: 0 }

  const selected = new Set<string>()
  const indexed = items.map((item, index) => ({ item, index }))
  const attentionItems = indexed
    .filter(({ item }) => priorityStatusRank(item) < 4)
    .sort((left, right) => priorityStatusRank(left.item) - priorityStatusRank(right.item) || left.index - right.index)

  for (const entry of attentionItems) {
    selected.add(entry.item.id)
  }

  if (selected.size < limit) {
    const recentItems = indexed
      .filter(({ item }) => !selected.has(item.id))
      .sort((left, right) => recentActivityScore(right.item) - recentActivityScore(left.item) || left.index - right.index)
    for (const entry of recentItems) {
      if (selected.size >= limit) break
      selected.add(entry.item.id)
    }
  }

  const visible = indexed
    .filter(({ item }) => selected.has(item.id))
    .map(({ item }) => item)
  return { visible, hiddenCount: items.length - visible.length }
}

function summaryTitle(summary?: string): string {
  const text = String(summary || '').trim()
  if (!text) return ''
  const firstLine = text.split(/\r?\n/).find(Boolean) || text
  return firstLine
    .replace(/^(?:ready|blocked|pending|task updated|task output|task created):\s*/i, '')
    .replace(/^\[[^\]]+\]\s*/, '')
    .trim()
}

function stripWorkflowPrefix(value: string, workflowName?: string): string {
  const text = value.trim()
  if (!text) return ''
  const prefix = String(workflowName || '').trim()
  if (prefix && text.toLowerCase().startsWith(`${prefix.toLowerCase()}:`)) {
    return text.slice(prefix.length + 1).trim()
  }
  return text
}

function cleanedResultContent(content: string): string {
  const text = content.trim()
  if (!text) return ''
  const coordinatorNotice = userFacingCoordinatorNotice(coordinatorNoticeKind(text))
  if (coordinatorNotice) return coordinatorNotice
  const keyFindingsIndex = text.search(/(?:^|\n)(?:Key findings I can synthesize:|Key UX issues I can identify:|#+\s*(?:Findings|Key findings|关键发现|结论))/i)
  const source = keyFindingsIndex >= 0 ? text.slice(keyFindingsIndex).replace(/^Key findings I can synthesize:/i, '### Key findings') : text
  const lines = source.split(/\r?\n/)
  const kept: string[] = []
  for (const line of lines) {
    const trimmed = line.trim()
    if (isLowValueResultLine(trimmed)) continue
    kept.push(line)
  }
  const cleaned = kept.join('\n').replace(/\n{3,}/g, '\n\n').trim()
  if (!cleaned || isMostlyRecoverySummary(cleaned)) {
    return '未形成可读报告。这个子任务只返回了执行日志或恢复摘要，已隐藏。'
  }
  return cleaned
}

function isLowValueResultLine(line: string): boolean {
  if (!line) return false
  return (
    /\bcall_[a-z0-9_-]{8,}\b/i.test(line) ||
    /^\d+(?:\.\d+)?s elapsed$/i.test(line) ||
    /^Subagent\s+subagent-[\w-]+.*completed/i.test(line) ||
    /^已达到最大迭代次数限制/i.test(line) ||
    /^Recovery summary based on completed tool results:?$/i.test(line) ||
    /^Tools used \(\d+ total\):/i.test(line) ||
    /^Error:\s*已达到最大迭代次数限制/i.test(line) ||
    /^Internal artifact was read/i.test(line) ||
    /^read artifact\b/i.test(line) ||
    /^read file\b/i.test(line) ||
    /^grep files\b/i.test(line) ||
    /^glob files\b/i.test(line) ||
    /^\d+\.\s*(?:read file|grep files|glob files|read artifact)\b/i.test(line) ||
    /^[-*]\s*(?:read_file|grep_files|glob_files|read_artifact|read file|grep files|glob files)\(/i.test(line)
  )
}

function isMostlyRecoverySummary(content: string): boolean {
  const meaningful = content
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
  if (meaningful.length === 0) return true
  const technical = meaningful.filter((line) =>
    /^(?:\d+\.\s*)?(?:read file|grep files|glob files|read artifact)|→|C:\\|frontend\/|backend\/|Tools used|Recovery summary/i.test(line),
  )
  return technical.length / meaningful.length > 0.55
}

const WorkflowOverview = ({
  nodes,
  mode,
  onSelectNode,
}: {
  nodes: AgentView[]
  mode?: string
  onSelectNode: (id: string) => void
}) => {
  const [stripExpanded, setStripExpanded] = useState(false)
  if (nodes.length === 0) return null
  const counts = workflowStatusCounts(nodes)
  const completePercent = Math.max(0, Math.min(100, Math.round((counts.done / nodes.length) * 100)))
  const nextAction = workflowNextAction(nodes)
  const stripCanExpand = nodes.length > WORKFLOW_STRIP_PREVIEW_LIMIT
  const stripNodes = stripExpanded ? nodes : limitedViewWindow(nodes, WORKFLOW_STRIP_PREVIEW_LIMIT, false).visible
  const stripHiddenCount = nodes.length - stripNodes.length
  return (
    <div className="subagents-workflow-overview" aria-label="流程概览">
      <div className="subagents-workflow-progress-row">
        <span className="subagents-workflow-mode">{modeLabel(mode)}</span>
        <span className="subagents-workflow-progress-label">{counts.done}/{nodes.length} 完成</span>
        <span className="subagents-progress-track" role="progressbar" aria-label="流程完成进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={completePercent}>
          <span className="subagents-progress-fill" style={{ '--subagents-progress': String(completePercent / 100) } as CSSProperties} />
        </span>
      </div>
      {nextAction && <div className="subagents-workflow-next">{nextAction}</div>}
      <div className="subagents-node-strip">
        {stripNodes.map((node) => {
          const status = node.effectiveStatus
          const noticeKind = node.coordinatorNoticeKind
          const label = workflowNodeLabel(node)
          const hint = node.blockedBy?.length
            ? `等待 ${compactList(node.blockedBy)}`
            : noticeKind
              ? node.statusLabel
            : node.dependsOn?.length
              ? `依赖 ${compactList(node.dependsOn)}`
              : node.statusLabel
          return (
            <button
              key={node.id}
              type="button"
              className="subagents-node-chip subagents-node-button"
              title={`${label}: ${hint}`}
              aria-label={`展开节点：${label}`}
              onClick={() => onSelectNode(node.id)}
            >
              <span className="subagents-node-dot" style={{ background: statusColor(status) }} aria-hidden="true" />
              <span>{label}</span>
            </button>
          )
        })}
        {stripCanExpand && (
          <button
            type="button"
            className="subagents-node-chip subagents-node-button subagents-node-more"
            aria-expanded={stripExpanded}
            aria-label={stripExpanded ? '收起流程节点' : `显示全部 ${nodes.length} 个流程节点`}
            onClick={() => setStripExpanded((value) => !value)}
          >
            {stripExpanded ? '收起节点' : `+${stripHiddenCount} 节点`}
          </button>
        )}
      </div>
    </div>
  )
}

const ListWindowControl = ({
  expanded,
  hiddenCount,
  label,
  onToggle,
  total,
}: {
  expanded: boolean
  hiddenCount: number
  label: string
  onToggle: () => void
  total: number
}) => (
  <button
    type="button"
    className="subagents-list-window-control"
    aria-expanded={expanded}
    onClick={onToggle}
  >
    {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
    <span>{expanded ? `收起${label}` : `显示全部${label}`}</span>
    <span className="subagents-list-window-count">{expanded ? `${total} 项` : `还有 ${hiddenCount} 项`}</span>
  </button>
)

const AgentRow = ({
  view,
  expanded,
  onOpen,
  onRevealResult,
  onStop,
  nested = false,
}: {
  view: AgentView
  expanded: boolean
  onOpen: () => void
  onRevealResult: () => void
  onStop: () => void
  nested?: boolean
}) => {
  const sub = view.source
  const status = view.effectiveStatus
  const resultText = sub.resultError || sub.resultContent || ''
  const hasResult = view.hasResult && !view.coordinatorNoticeKind
  const cleanedResult = hasResult && sub.resultContent ? cleanedResultContent(sub.resultContent) : ''
  const cleanedError = sub.resultError ? cleanedResultContent(sub.resultError) : ''
  const chips = view.metadataChips
  const title = view.title
  const secondary = view.summary
  const displaySecondary = secondary && secondary !== title ? secondary : ''
  const detail = view.detail
  const progressTrace = view.progressTrace
  const hasDetails = Boolean((!view.coordinatorNoticeKind && (detail || progressTrace)) || hasResult)
  const hasKnownProgress = view.hasKnownProgress
  const percent = view.progressPercent
  return (
    <article className="subagents-row" data-active={status === 'running'} data-nested={nested} data-status={status}>
      <StatusMark status={status} />
      <div className="subagents-row-main">
        <button
          type="button"
          onClick={onOpen}
          className="subagents-row-button"
          aria-label={`打开 Agent：${title}`}
        >
          <ChevronRight size={13} className="subagents-row-chevron" data-open={expanded ? 'true' : 'false'} />
          <span className="subagents-role-chip">{view.roleLabel}</span>
          <span className="subagents-row-title">{title}</span>
          <span className="subagents-row-status">{view.statusLabel}</span>
        </button>

        {displaySecondary && <div className="subagents-row-secondary">{displaySecondary}</div>}

        {status === 'running' && (
          <div className="subagents-row-progress" aria-label="任务正在运行">
            <span
              className="subagents-progress-track"
              role="progressbar"
              aria-label="Agent 执行进度"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={hasKnownProgress ? percent : undefined}
            >
              <span
                className="subagents-progress-fill"
                data-indeterminate={status === 'running' && !hasKnownProgress ? 'true' : 'false'}
                style={{ '--subagents-progress': String((hasKnownProgress || status !== 'running' ? percent : 34) / 100) } as CSSProperties}
              />
            </span>
          </div>
        )}

        {expanded && chips.length > 0 && (
          <div className="subagents-chip-row">
            {chips.map((chip) => <span key={chip} className="subagents-meta-chip">{chip}</span>)}
          </div>
        )}

        <div className="subagents-actions">
          {hasResult && (
            <SmallButton
              icon={expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              label={expanded ? '收起结果' : '查看结果'}
              onClick={onRevealResult}
            />
          )}
          {view.canStop && (
            <SmallButton icon={<Square size={12} />} label="停止" onClick={onStop} />
          )}
        </div>

        {hasDetails && (
          <div className="subagents-row-details" data-open={expanded ? 'true' : 'false'}>
            <div className="subagents-row-details-inner">
              {detail && <div className="subagents-detail-line">{detail}</div>}
              {progressTrace && <div className="subagents-detail-muted">{progressTrace}</div>}
              {cleanedError && (
                <pre className="subagents-result subagents-result-raw" data-error="true">
                  {cleanedError}
                </pre>
              )}
              {cleanedResult && (
                <div className="subagents-result">
                  <MarkdownRenderer content={cleanedResult} />
                </div>
              )}
              {sub.resultAvailable && !resultText && (
                <div className="subagents-detail-muted">正在获取结果...</div>
              )}
            </div>
          </div>
        )}
      </div>
    </article>
  )
}

const AgentDetail = ({
  view,
  onBack,
  onRefresh,
  onStop,
}: {
  view: AgentView
  onBack: () => void
  onRefresh: () => void
  onStop: () => void
}) => {
  const sub = view.source
  const status = view.effectiveStatus
  const result = sub.resultContent ? cleanedResultContent(sub.resultContent) : ''
  const resultError = sub.resultError ? cleanedResultContent(sub.resultError) : ''
  const activity = view.activity
  const trace = view.progressTrace
  const needsResult = view.needsResult
  const milestones = view.milestones

  return (
    <section className="subagents-detail-view" aria-label={`Agent 详情：${view.title}`}>
      <header className="subagents-detail-header">
        <button type="button" className="subagents-detail-back" onClick={onBack} aria-label="返回 Agents 列表">
          <ArrowLeft size={16} />
        </button>
        <span className="subagents-detail-glyph" data-status={status} aria-hidden="true">✳</span>
        <div className="subagents-detail-heading">
          <strong>{view.title}</strong>
          <span>{view.statusLabel}</span>
        </div>
      </header>
      <div className="subagents-detail-scroll">
        <p className="subagents-detail-brief">{view.title || view.summary || activity || '正在处理当前任务。'}</p>

        {view.metadataChips.length > 0 && (
          <div className="subagents-chip-row">
            {view.metadataChips.map((chip) => <span key={chip} className="subagents-meta-chip">{chip}</span>)}
          </div>
        )}

        <div className="subagents-milestones" aria-label="Agent 里程碑">
          {milestones.map((milestone, index) => (
            <div key={`${index}:${milestone.label}`} className="subagents-milestone" data-done={milestone.done ? 'true' : 'false'}>
              <span className="subagents-milestone-dot" aria-hidden="true" />
              <span>{milestone.label}</span>
            </div>
          ))}
        </div>

        {(activity || trace || sub.detail) && (
          <details className="subagents-activity-group">
            <summary>活动记录</summary>
            <div className="subagents-activity-lines">
              {activity && <div>{activity}</div>}
              {view.detail && <div>{view.detail}</div>}
              {trace && <div className="subagents-detail-muted">{trace}</div>}
            </div>
          </details>
        )}

        {(result || resultError || needsResult) && (
          <section className="subagents-detail-result" aria-label="Agent 结果">
            <h3>结果</h3>
            {resultError && <div className="subagents-detail-error">{resultError}</div>}
            {result && <MarkdownRenderer content={result} />}
            {needsResult && <div className="subagents-detail-muted">结果已就绪，获取后会显示在这里。</div>}
          </section>
        )}

        <div className="subagents-detail-actions">
          {needsResult && <SmallButton icon={<ChevronRight size={12} />} label="获取结果" onClick={onRefresh} />}
          {view.canStop && <SmallButton icon={<Square size={12} />} label="停止 Agent" onClick={onStop} />}
        </div>
      </div>
    </section>
  )
}

export const SubagentsTab = () => {
  const subagents = useAppStore((s) => s.subagents)
  const selectedAgentId = useAppStore((s) => s.focusedSubagentId)
  const setSelectedAgentId = useAppStore((s) => s.setFocusedSubagentId)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [expandedLists, setExpandedLists] = useState<Record<string, boolean>>({})
  const allViews = projectAllAgentViews(subagents)
  const visibleViews = allViews.filter(isDisplayableView)
  const viewsById = new Map(visibleViews.map((view) => [view.id, view]))

  if (visibleViews.length === 0) {
    return (
      <div>
        <PanelHeader title="子智能体" meta="" />
        <div className="subagents-section-label">已开启</div>
        <EmptyLine>没有正在执行的子智能体</EmptyLine>
      </div>
    )
  }

  const workItems = primaryWorkItems(visibleViews)
  const running = workItems.filter((view) => view.effectiveStatus === 'running').length
  const blocked = workItems.filter((view) => view.effectiveStatus === 'blocked').length
  const collectBlocked = workItems.filter((view) =>
    view.effectiveStatus === 'blocked' &&
    view.coordinatorNoticeKind === 'collect_results'
  ).length
  const otherBlocked = Math.max(0, blocked - collectBlocked)
  const errors = workItems.filter((view) => view.effectiveStatus === 'error').length
  const workflows = visibleViews.filter(isWorkflowView)
  const workflowIds = new Set(workflows.map((workflow) => workflow.workflowId || workflow.id))
  const ungrouped = visibleViews.filter((view) =>
    !view.isWorkflow &&
    (!view.workflowId || !workflowIds.has(view.workflowId))
  )
  const summary = collaborationSummary(visibleViews)

  const stopSubagent = (subagentId: string) => {
    sendClientCommand({ type: 'subagent.cancel', subagent_id: subagentId })
  }
  const refreshSubagent = (subagentId: string) => {
    sendClientCommand({ type: 'subagent.status', subagent_id: subagentId, include_result: true })
  }
  const resumeWorkflow = (workflowId: string) => {
    sendClientCommand({ type: 'workflow.resume', workflow_id: workflowId })
  }
  const selectedView = visibleViews.find((view) => view.id === selectedAgentId)
  if (selectedView && !selectedView.isWorkflow) {
    return (
      <AgentDetail
        view={selectedView}
        onBack={() => setSelectedAgentId(null)}
        onRefresh={() => refreshSubagent(selectedView.id)}
        onStop={() => stopSubagent(selectedView.id)}
      />
    )
  }
  const hasExpandedOverride = (id: string): boolean =>
    Object.prototype.hasOwnProperty.call(expanded, id)
  const defaultExpanded = (view: AgentView, nested = false): boolean =>
    !nested &&
    !view.coordinatorNoticeKind &&
    view.effectiveStatus !== 'running' &&
    Boolean(view.resultContent || view.resultError)
  const isExpanded = (view: AgentView, nested = false): boolean =>
    hasExpandedOverride(view.id) ? Boolean(expanded[view.id]) : defaultExpanded(view, nested)
  const toggle = (id: string, defaultValue = false) => {
    setExpanded((current) => {
      const currentValue = Object.prototype.hasOwnProperty.call(current, id)
        ? Boolean(current[id])
        : defaultValue
      return { ...current, [id]: !currentValue }
    })
  }
  const revealResult = (view: AgentView) => {
    if (view.resultContent || view.resultError) {
      toggle(view.id, defaultExpanded(view))
      return
    }
    setExpanded((current) => ({ ...current, [view.id]: true }))
    if (isActionableView(view)) refreshSubagent(view.id)
  }
  const isListExpanded = (key: string): boolean => Boolean(expandedLists[key])
  const toggleListExpanded = (key: string) => {
    setExpandedLists((current) => ({ ...current, [key]: !current[key] }))
  }

  const renderRow = (view: AgentView, nested = false) => (
    <AgentRow
      key={view.id}
      view={view}
      nested={nested}
      expanded={isExpanded(view, nested)}
      onOpen={() => {
        setSelectedAgentId(view.id)
        if (view.needsResult && isActionableView(view)) refreshSubagent(view.id)
      }}
      onRevealResult={() => revealResult(view)}
      onStop={() => stopSubagent(view.id)}
    />
  )

  return (
    <div className="subagents-tab">
      <PanelHeader
        title="子智能体"
        meta={[
          running ? `${running} 运行` : '',
          collectBlocked ? `${collectBlocked} 整理中` : '',
          otherBlocked ? `${otherBlocked} 等待` : '',
          errors ? `${errors} 失败` : '',
          `${workItems.length} 项`,
        ].filter(Boolean).join(' · ')}
      />
      <div className="subagents-summary-bar" data-status={summary.tone}>
        <div className="subagents-summary-copy">
          <div className="subagents-summary-title">{summary.title}</div>
          {summary.meta && <div className="subagents-summary-meta">{summary.meta}</div>}
        </div>
      </div>
      <div className="subagents-stack">
        {workflows.map((workflow) => {
          const workflowId = workflow.workflowId || workflow.id
          const children = visibleViews
            .filter((view) => !view.isWorkflow && view.workflowId === workflowId)
            .sort(stableViewOrder)
          const childrenExpanded = isListExpanded(`workflow:${workflowId}`)
          const childWindow = limitedViewWindow(children, WORKFLOW_CHILD_PREVIEW_LIMIT, childrenExpanded)
          const canResume = children.some((child) => child.effectiveStatus === 'pending')
          const status = workflow.effectiveStatus
          return (
            <section key={workflow.id} className="subagents-workflow-card" aria-label={workflow.workflowName || workflow.title} data-status={status}>
              <div className="subagents-workflow-header">
                <GitBranch size={14} />
                <span className="subagents-workflow-title">{workflow.workflowName || workflow.title}</span>
                <span className="subagents-workflow-meta">{workflowStatusMeta(children)}</span>
                {canResume && (
                  <SmallButton
                    icon={<PlayCircle size={12} />}
                    label="继续"
                    onClick={() => resumeWorkflow(workflowId)}
                  />
                )}
              </div>
              {workflow.resultError && (
                <div className="subagents-detail-error">{cleanedResultContent(workflow.resultError)}</div>
              )}
              {workflow.resultContent && (
                <div className="subagents-result"><MarkdownRenderer content={cleanedResultContent(workflow.resultContent)} /></div>
              )}
              <div className="subagents-workflow-nodes">
                {childWindow.visible.map((child) => renderRow(child, true))}
                {children.length > WORKFLOW_CHILD_PREVIEW_LIMIT && (
                  <ListWindowControl
                    expanded={childrenExpanded}
                    hiddenCount={childWindow.hiddenCount}
                    label="任务"
                    total={children.length}
                    onToggle={() => toggleListExpanded(`workflow:${workflowId}`)}
                  />
                )}
              </div>
            </section>
          )
        })}
        {(() => {
          const ungroupedExpanded = isListExpanded('ungrouped')
          const ungroupedWindow = limitedViewWindow(ungrouped, UNGROUPED_PREVIEW_LIMIT, ungroupedExpanded)
          return (
            <>
              {ungroupedWindow.visible.map((view) => renderRow(view))}
              {ungrouped.length > UNGROUPED_PREVIEW_LIMIT && (
                <ListWindowControl
                  expanded={ungroupedExpanded}
                  hiddenCount={ungroupedWindow.hiddenCount}
                  label="未分组任务"
                  total={ungrouped.length}
                  onToggle={() => toggleListExpanded('ungrouped')}
                />
              )}
            </>
          )
        })()}
      </div>
    </div>
  )
}
