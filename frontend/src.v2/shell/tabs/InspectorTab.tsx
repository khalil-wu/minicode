/**
 * Inspector tab — combines ContextTab (session/workspace/runtime info)
 * and DetailsTab (inspector entries / recent tool calls).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, Copy, Database, Download, FolderOpen, GitBranch, TerminalSquare, Upload } from 'lucide-react'
import { isDesktop, revealPath } from '../../desktop/runtime'
import { fetchWorkspaceGitStatus, type WorkspaceGitStatusResponse } from '../../protocol/workspace'
import { useAppStore } from '../../stores'
import { focusInspectorEntry } from '../../chat/inspectorEntries'
import type { AgentProgressEntry, ChatMessage, InspectorEntry, ProviderRawMetadata } from '../../stores/types'
import { branchDisplayName, workspaceDisplayName } from '../../lib/workspace-display'
import { hasProviderContainerMetadata, hasProviderRefusalMetadata, providerCacheDiagnosis, providerCacheHitRate, providerContainerSummary, providerCurlSkeleton, providerDuplicateInputSummary, providerInstructionsTransportSummary, providerLargestInputItemsSummary, providerLargestToolsSummary, providerLoopMetricsSummary, providerNativeUsageDetails, providerOutputPhaseCounts, providerOutputSequence, providerPromptCacheDiagnosticSummary, providerPromptLargestSections, providerPromptSectionDeltaSummary, providerPromptSectionSummary, providerRefusalSummary, providerRequestDiff, providerRequestDiffSummary, providerRequestModeSummary, providerResponseLifecycle, providerSafeRequestJson, providerSearchSourcesSummary, providerTimelineEventCounts, providerTimelineRows, providerTimelineSequence, providerTraceDiagnostics, providerTraceExportJson, providerTraceExportJsonl, providerTracePayloadFromExport, providerUsageSummary, sanitizeProviderTraceExportValue } from '../../chat/providerTrace'
import { promptCacheEffectivePromptTokens, promptCacheOrdinaryInputTokens } from '../../chat/cacheUsage'
import { capabilityFeatureEnabled } from '../../protocol/capabilities'
import { fetchReplayExport } from '../../protocol/replay'
import { getWebSocket } from '../../hooks/useWebSocket'
import { InfoCard, InfoRow, SectionLabel, SmallButton } from '../SidebarShared'
import { ToolCallTimeline, buildRunReplayEvents, buildRunReplaySummary, runTimelineExportJsonl, runTimelineReplayJsonl, type RunReplayEvent, type RunReplaySummary } from '../../chat/tool-calls/ToolCallTimeline'
import { getToolCallsFromMessage } from '../../lib/content-blocks'
import { safeJsonParse } from '../../lib/safe-parse'
import { commandResultSucceeded, sendClientCommand, sendClientCommandAwaitResult } from '../../protocol/ws-outbox'

export const InspectorTab = () => (
  <InspectorTabContent />
)

const InspectorTabContent = () => {
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities)
  const traceExportEnabled = capabilityFeatureEnabled(runtimeCapabilities, 'agent_trace_export_v1', false)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  return (
    <div style={{ display: 'grid', gap: 14 }}>
      <ContextTab />
      <ControlPlaneSection />
      <RunOverviewSection />
      <button
        type="button"
        aria-expanded={advancedOpen}
        onClick={() => setAdvancedOpen((value) => !value)}
        style={advancedToggleStyle}
      >
        <span style={{ display: 'grid', gap: 2 }}>
          <strong style={{ color: 'var(--text-primary)', fontSize: 'var(--text-sm)' }}>高级诊断</strong>
          <small style={{ color: 'var(--text-muted)', fontSize: 'var(--text-xs)' }}>面向开发者的事件、性能和原始请求信息</small>
        </span>
        <ChevronDown size={16} style={{ transform: advancedOpen ? 'rotate(180deg)' : 'none', transition: 'transform var(--transition-fast)' }} />
      </button>
      {advancedOpen && (
        <section aria-label="高级诊断" style={{ display: 'grid', gap: 16 }}>
          <RunTimelineSection traceExportEnabled={traceExportEnabled} />
          <RuntimeMetricsSection />
          <DetailsTab traceExportEnabled={traceExportEnabled} />
        </section>
      )}
    </div>
  )
}

const ControlPlaneSection = () => {
  const conversationId = useAppStore((state) => state.conversationId)
  const conversations = useAppStore((state) => state.conversations)
  const workingDirectory = useAppStore((state) => state.workingDirectory)
  const isConnected = useAppStore((state) => state.isConnected)
  const hydration = useAppStore((state) => conversationId ? state.conversationHydration[conversationId] : undefined)
  const permissions = useAppStore((state) => conversationId ? state.permissionRulesByConversation[conversationId] : undefined)
  const checkpointCollection = useAppStore((state) => conversationId ? state.checkpointsByConversation[conversationId] : undefined)
  const runCheckpointCollection = useAppStore((state) => conversationId ? state.runCheckpointsByConversation[conversationId] : undefined)
  const resume = useAppStore((state) => conversationId ? state.checkpointResumeByConversation[conversationId] : undefined)
  const guidelineReload = useAppStore((state) => conversationId ? state.guidelineReloadsByConversation[conversationId] : undefined)
  const recentWorkspaces = useAppStore((state) => state.recentWorkspaces)
  const conversation = conversations.find((item) => item.id === conversationId)
  const workspaceRoot = conversation?.worktreePath || conversation?.workspaceRoot || workingDirectory
  const [controlPlaneRefreshError, setControlPlaneRefreshError] = useState<string | null>(null)
  const [isRefreshingControlPlane, setIsRefreshingControlPlane] = useState(false)
  const refreshRequestRef = useRef(0)

  const refreshControlPlane = useCallback(async (silent: boolean) => {
    if (!isConnected || !conversationId || !workspaceRoot) return
    const requestId = ++refreshRequestRef.current
    setControlPlaneRefreshError(null)
    setIsRefreshingControlPlane(true)

    const checkpointListSent = sendClientCommand({
      type: 'checkpoint.list',
      conversation_id: conversationId,
      workspace_root: workspaceRoot,
      limit: 50,
    }, { silent })
    const runCheckpointListSent = sendClientCommand({
      type: 'checkpoint.run.list',
      conversation_id: conversationId,
      workspace_root: workspaceRoot,
    }, { silent })

    try {
      if (!checkpointListSent || !runCheckpointListSent) {
        throw new Error('连接已中断，未能发送全部刷新请求')
      }
      const permissionResult = await sendClientCommandAwaitResult({
        type: 'conversation.permission.rules.list',
        conversation_id: conversationId,
        source: 'frontend.inspector',
      }, 'permissions.rules.list', { silent })
      const resultConversationId = String(permissionResult.data?.conversation_id || '').trim()
      const hasRulesPayload = Boolean(
        permissionResult.data?.rules
        && typeof permissionResult.data.rules === 'object'
        && !Array.isArray(permissionResult.data.rules),
      )
      if (!commandResultSucceeded(permissionResult) || resultConversationId !== conversationId || !hasRulesPayload) {
        throw new Error(permissionResult.message || '权限规则读取失败')
      }
    } catch (error) {
      if (refreshRequestRef.current === requestId) {
        setControlPlaneRefreshError(error instanceof Error ? error.message : String(error))
      }
    } finally {
      if (refreshRequestRef.current === requestId) setIsRefreshingControlPlane(false)
    }
  }, [conversationId, isConnected, workspaceRoot])

  useEffect(() => {
    if (!isConnected || !conversationId || !workspaceRoot) {
      refreshRequestRef.current += 1
      setIsRefreshingControlPlane(false)
      setControlPlaneRefreshError(null)
      return
    }
    void refreshControlPlane(true)
  }, [conversationId, isConnected, refreshControlPlane, workspaceRoot])

  if (!conversationId && recentWorkspaces.length === 0) return null
  const latestCheckpoints = checkpointCollection?.checkpoints.slice(0, 3) ?? []
  const permissionPatterns = [
    ...(permissions?.sessionDeny ?? []).map((rule) => rule.pattern).filter(Boolean),
    ...(permissions?.sessionOverrides ?? []).map((rule) => `${rule.pattern}: ${rule.level}`).filter(Boolean),
  ]

  return (
    <div style={{ display: 'grid', gap: 8 }}>
      <div style={sectionHeaderWithActionsStyle}>
        <SectionLabel label="安全与恢复" />
        {conversationId && workspaceRoot && (
          <SmallButton
            icon={<Database size={14} />}
            label={isRefreshingControlPlane ? '正在刷新…' : '刷新检查点'}
            onClick={() => void refreshControlPlane(false)}
            disabled={!isConnected || isRefreshingControlPlane}
          />
        )}
      </div>
      <InfoCard>
        {!isConnected && conversationId && workspaceRoot && (
          <InfoRow label="控制面" value="后端未连接 · 重连后自动刷新" tone="warning" />
        )}
        {isConnected && isRefreshingControlPlane && (
          <InfoRow label="控制面" value="正在读取权限规则和检查点…" tone="accent" />
        )}
        {controlPlaneRefreshError && (
          <InfoRow
            label="控制面"
            value={`刷新失败 · ${controlPlaneRefreshError}`}
            title={controlPlaneRefreshError}
            tone="warning"
          />
        )}
        {hydration && (
          <InfoRow
            label="会话恢复"
            value={hydration.isHydrating ? '正在恢复上下文和运行状态' : `已完成 · ${new Date(hydration.updatedAt).toLocaleTimeString()}`}
            tone={hydration.isHydrating ? 'accent' : 'muted'}
          />
        )}
        {permissions && (
          <>
            <InfoRow label="权限模式" value={`${permissions.mode} · ${permissions.contextSource}`} mono />
            <InfoRow
              label="权限规则"
              value={`会话拒绝 ${permissions.sessionDeny.length} · 覆盖 ${permissions.sessionOverrides.length} · 系统拒绝 ${permissions.systemDeny.length} · 临时允许 ${permissions.sessionPromptRules.length}`}
              tone={permissions.sessionDeny.length + permissions.systemDeny.length > 0 ? 'warning' : 'muted'}
              title={permissionPatterns.join('\n') || undefined}
            />
          </>
        )}
        {guidelineReload && (
          <InfoRow
            label="项目指令"
            value={`${guidelineReload.path || '指令源'} 已重载 · 下一回合生效 · ${new Date(guidelineReload.updatedAt).toLocaleTimeString()}`}
            tone="accent"
            mono
          />
        )}
        <InfoRow
          label="文件检查点"
          value={checkpointCollection ? `${checkpointCollection.checkpoints.length} 个 · ${new Date(checkpointCollection.updatedAt).toLocaleTimeString()} 更新` : '尚未读取'}
          tone={checkpointCollection?.checkpoints.length ? 'accent' : 'muted'}
        />
        {latestCheckpoints.map((checkpoint) => (
          <InfoRow
            key={checkpoint.id}
            label={checkpoint.toolName || '写入保护'}
            value={`${checkpoint.paths.length} 个路径 · ${checkpoint.paths.slice(0, 2).join(', ') || '无路径'} · ${new Date(checkpoint.createdAt).toLocaleTimeString()}`}
            title={`${checkpoint.id}\n${checkpoint.paths.join('\n')}`}
            mono
          />
        ))}
        {runCheckpointCollection && (
          <InfoRow
            label="运行检查点"
            value={`${runCheckpointCollection.checkpoints.length} 个快照 · ${runCheckpointCollection.runs.length} 个运行 · ${runCheckpointCollection.subagents.length} 个子 Agent`}
          />
        )}
        {resume && (
          <InfoRow
            label="最近恢复"
            value={resume.resumed
              ? `${resume.runId || '未知运行'} · 第 ${resume.iteration ?? 0} 轮${resume.stoppedReason ? ` · 原因 ${resume.stoppedReason}` : ''}`
              : resume.message || '没有可恢复运行'}
            tone={resume.resumed ? 'accent' : 'muted'}
          />
        )}
        {recentWorkspaces.length > 0 && <InfoRow label="最近工作区" value={`${recentWorkspaces.length} 个已记录`} />}
      </InfoCard>
    </div>
  )
}

const RunOverviewSection = () => {
  const messages = useAppStore((s) => s.messages)
  const agentProgress = useAppStore((s) => s.agentProgress)
  const replayEvents = useMemo(() => buildRunReplayEvents(messages, agentProgress), [messages, agentProgress])
  const summary = useMemo(() => buildRunReplaySummary(replayEvents), [replayEvents])
  const status = summary.outcome === 'needs_attention'
    ? `${summary.failedOrBlocked} 项需要处理`
    : summary.outcome === 'running'
      ? `${summary.running} 项进行中`
      : summary.outcome === 'completed'
        ? '已完成'
        : '尚未开始'
  return (
    <div style={{ display: 'grid', gap: 8 }}>
      <SectionLabel label="本次运行" />
      <InfoCard>
        <InfoRow label="状态" value={status} tone={summary.outcome === 'needs_attention' ? 'warning' : summary.outcome === 'running' ? 'accent' : 'muted'} />
        {replayEvents.length > 0 && <InfoRow label="活动" value={`${replayEvents.length} 条`} />}
        {summary.spanMs != null && <InfoRow label="耗时" value={`${(summary.spanMs / 1000).toFixed(1)} 秒`} mono />}
      </InfoCard>
    </div>
  )
}

const RunTimelineSection = ({ traceExportEnabled }: { traceExportEnabled: boolean }) => {
  const messages = useAppStore((s) => s.messages)
  const agentProgress = useAppStore((s) => s.agentProgress)
  const conversationId = useAppStore((s) => s.conversationId)
  const [expanded, setExpanded] = useState(false)
  const [sessionReplayStatus, setSessionReplayStatus] = useState('')
  const replayEvents = useMemo(() => buildRunReplayEvents(messages, agentProgress), [messages, agentProgress])
  const replaySummary = useMemo(() => buildRunReplaySummary(replayEvents), [replayEvents])
  const exportJsonl = () => runTimelineExportJsonl(messages, agentProgress)
  const replayJsonl = () => runTimelineReplayJsonl(messages, agentProgress)
  const copySessionReplay = async () => {
    const sessionId = getWebSocket()?.sessionId
    if (!sessionId) {
      setSessionReplayStatus('当前没有活动会话')
      return
    }
    setSessionReplayStatus('正在读取会话记录…')
    try {
      const payload = await fetchReplayExport({ sessionId, conversationId, limit: 500 })
      await navigator.clipboard?.writeText(JSON.stringify(payload, null, 2))
      setSessionReplayStatus(`已复制 ${payload.event_count} 条会话事件`)
    } catch (error) {
      setSessionReplayStatus(error instanceof Error ? error.message : String(error))
    }
  }
  return (
    <div style={{ display: 'grid', gap: 8 }}>
      <div style={sectionHeaderWithActionsStyle}>
        <SectionLabel label="运行记录" />
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          <SmallButton
            icon={<TerminalSquare size={14} />}
            label={expanded ? '收起事件' : '查看最近事件'}
            onClick={() => setExpanded((value) => !value)}
          />
        {traceExportEnabled && (
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            <SmallButton icon={<Copy size={14} />} label="复制 JSONL" onClick={() => void navigator.clipboard?.writeText(exportJsonl())} />
            <SmallButton icon={<Download size={14} />} label="下载 JSONL" onClick={() => downloadText('minicode-run-timeline.jsonl', exportJsonl(), 'application/x-ndjson')} />
            <SmallButton icon={<Copy size={14} />} label="复制回放" onClick={() => void navigator.clipboard?.writeText(replayJsonl())} />
            <SmallButton icon={<Download size={14} />} label="下载回放" onClick={() => downloadText('minicode-run-replay.jsonl', replayJsonl(), 'application/x-ndjson')} />
            <SmallButton icon={<Copy size={14} />} label="复制会话回放" onClick={() => void copySessionReplay()} />
          </div>
        )}
        </div>
      </div>
      {traceExportEnabled && sessionReplayStatus && <div style={sessionReplayStatusStyle}>{sessionReplayStatus}</div>}
      <InfoCard>
        <InfoRow label="事件" value={replayEvents.length === 0 ? '暂无运行事件' : `${replayEvents.length} 条`} mono />
        <InfoRow label="状态" value={replaySummary.outcome === 'needs_attention' ? `${replaySummary.failedOrBlocked} 项需要处理` : replaySummary.outcome === 'running' ? `${replaySummary.running} 项进行中` : replaySummary.outcome === 'completed' ? '已完成' : '空闲'} tone={replaySummary.outcome === 'needs_attention' ? 'warning' : replaySummary.outcome === 'running' ? 'accent' : 'muted'} />
        <InfoRow label="耗时" value={replaySummary.spanMs == null ? '—' : `${(replaySummary.spanMs / 1000).toFixed(1)} 秒`} mono />
      </InfoCard>
      {expanded && (
        <div style={timelineShellStyle}>
          <ToolCallTimeline limit={40} />
        </div>
      )}
      {expanded && traceExportEnabled && replayEvents.length > 0 && (
        <ReplayPreview events={replayEvents} summary={replaySummary} />
      )}
    </div>
  )
}

const ReplayPreview = ({ events, summary }: { events: RunReplayEvent[]; summary: RunReplaySummary }) => {
  const visibleEvents = events.slice(0, 8)
  return (
    <div style={{ display: 'grid', gap: 8 }}>
      <SectionLabel label="回放预览" />
      <InfoCard>
        <InfoRow label="事件" value={`${summary.events} 条可回放`} mono />
        <InfoRow label="覆盖率" value={`${summary.coveragePercent}% 有计时 · ${summary.phases.join(', ') || '无阶段'}`} tone={summary.coveragePercent >= 95 ? 'accent' : 'warning'} mono />
        <InfoRow label="结果" value={replayOutcomeLabel(summary)} tone={summary.outcome === 'needs_attention' ? 'warning' : summary.outcome === 'completed' ? 'accent' : 'muted'} />
        <InfoRow label="耗时" value={summary.spanMs == null ? '—' : `${(summary.spanMs / 1000).toFixed(2)} 秒`} mono />
      </InfoCard>
      <div style={replayListStyle}>
        {visibleEvents.map((event) => (
          <div key={event.seq} style={replayRowStyle(event.status)}>
            <span style={replaySeqStyle}>#{event.seq}</span>
            <span style={replayEventStyle}>{event.event}</span>
            <span style={replayLabelStyle} title={event.summary || event.label}>{event.label}</span>
            <span style={replayDurationStyle}>{event.duration_ms == null ? 'open' : `${event.duration_ms}ms`}</span>
          </div>
        ))}
        {events.length > visibleEvents.length && (
          <div style={replayMoreStyle}>另有 {events.length - visibleEvents.length} 条回放事件</div>
        )}
      </div>
    </div>
  )
}

// ── Context Tab ────────────────────────────────────────────────

const ContextTab = () => {
  const conversationId = useAppStore((s) => s.conversationId)
  const conversations = useAppStore((s) => s.conversations)
  const workingDirectory = useAppStore((s) => s.workingDirectory)
  const workspaceGit = useAppStore((s) => s.workspaceGit)
  const contextUsage = useAppStore((s) => s.contextUsage)
  const terminalSessions = useAppStore((s) => s.terminalSessions)
  const activeEditorPath = useAppStore((s) => s.activeEditorPath)
  const setRightStackTab = useAppStore((s) => s.setRightStackTab)

  const [gitStatus, setGitStatus] = useState<WorkspaceGitStatusResponse | null>(null)
  const [gitLoading, setGitLoading] = useState(false)
  const [gitError, setGitError] = useState<string | null>(null)

  const conversation = conversations.find((c) => c.id === conversationId)
  const workspacePath = conversation?.worktreePath || conversation?.workspaceRoot || workingDirectory || workspaceGit?.currentPath || ''
  const branch = conversation?.gitBranch || workspaceGit?.branch || '无分支'
  const hasWorkspacePath = Boolean(workspacePath.trim())
  const displayWorkspace = workspaceDisplayName(workspacePath, '本机')
  const displayBranch = branchDisplayName(branch) || '无分支'
  const contextPercent = contextUsage && contextUsage.limit > 0 ? Math.round((contextUsage.used / contextUsage.limit) * 100) : null
  const contextLedger = contextUsage?.ledger
  const visibleLedgerEntries = contextLedger?.entries.filter((entry) =>
    entry.estimated_tokens > 0 || entry.item_count > 0 || entry.source_count > 0
  ) ?? []
  const changedCount = gitStatus ? gitStatus.modified.length + gitStatus.staged.length + gitStatus.untracked.length : null
  const hasSessionRows = Boolean(conversationId)
  const hasWorkspaceRows = hasWorkspacePath
  const hasRuntimeRows =
    contextPercent != null ||
    Boolean(contextUsage?.compactedAt) ||
    terminalSessions.length > 0 ||
    Boolean(activeEditorPath)

  const refreshGitStatus = () => {
    setGitLoading(true)
    setGitError(null)
    fetchWorkspaceGitStatus(workspacePath)
      .then((result) => setGitStatus(result))
      .catch((error) => {
        setGitStatus(null)
        setGitError(error instanceof Error ? error.message : String(error))
      })
      .finally(() => setGitLoading(false))
  }

  useEffect(() => {
    if (!hasWorkspacePath) {
      setGitStatus(null)
      setGitError(null)
      setGitLoading(false)
      return
    }
    refreshGitStatus()
  }, [workspacePath, hasWorkspacePath])

  if (!hasSessionRows && !hasWorkspaceRows && !hasRuntimeRows) return null

  return (
    <div style={{ display: 'grid', gap: 10 }}>
      {hasSessionRows && (
        <>
          <SectionLabel label="会话" />
          <InfoCard>
            <InfoRow label="任务" value={conversation?.title || conversationId || '未命名'} />
            {hasWorkspacePath && (
              <InfoRow
                label="工作区模式"
                value={conversation?.gitIsolated || workspaceGit?.isWorktree ? '隔离工作区' : '共享工作区'}
                tone={conversation?.gitIsolated || workspaceGit?.isWorktree ? 'accent' : 'muted'}
              />
            )}
            {displayBranch !== '无分支' && <InfoRow label="分支" value={displayBranch} mono />}
          </InfoCard>
        </>
      )}
      {hasWorkspaceRows && (
        <>
          <SectionLabel label="工作区" />
          <InfoCard>
            <InfoRow label="位置" value={displayWorkspace} mono />
            <InfoRow
              label="更改"
              value={gitLoading ? '检查中…' : gitError ? '暂不可用' : changedCount == null ? '未知' : changedCount === 0 ? '无更改' : `${changedCount} 项`}
              tone={gitError || (changedCount && changedCount > 0) ? 'warning' : 'muted'}
            />
            {gitError && (
              <div style={{ color: 'var(--state-warning)', background: 'color-mix(in oklch, var(--state-warning) 9%, transparent)', border: '1px solid color-mix(in oklch, var(--state-warning) 32%, transparent)', borderRadius: 'var(--radius-sm, 4px)', padding: '6px 8px', fontSize: 'var(--text-xs)', lineHeight: 1.4 }}>
                无法读取 Git 状态：{gitError}
              </div>
            )}
            <div style={{ display: 'flex', gap: 6, marginTop: 8, flexWrap: 'wrap' }}>
              <SmallButton icon={<FolderOpen size={14} />} label="打开位置" disabled={!isDesktop()} onClick={() => void revealPath(workspacePath)} />
              <SmallButton icon={<Copy size={14} />} label="复制路径" onClick={() => void navigator.clipboard?.writeText(workspacePath)} />
              <SmallButton icon={<GitBranch size={14} />} label="刷新" onClick={refreshGitStatus} />
            </div>
          </InfoCard>
        </>
      )}
      {hasRuntimeRows && (
        <>
          <SectionLabel label="运行环境" />
          <InfoCard>
            {contextPercent != null && (
              <InfoRow label="上下文" value={`${contextPercent}% (${contextUsage?.used}/${contextUsage?.limit})`} tone={contextPercent >= 85 ? 'warning' : 'muted'} />
            )}
            {contextUsage?.compactedAt && (
              <InfoRow
                label="最近压缩"
                value={`${new Date(contextUsage.compactedAt).toLocaleTimeString()}：${contextUsage.compactSummary || '已完成'}`}
                tone="accent"
              />
            )}
            {terminalSessions.length > 0 && <InfoRow label="终端" value={String(terminalSessions.length)} />}
            {activeEditorPath && <InfoRow label="编辑器" value={activeEditorPath} mono />}
            {terminalSessions.length > 0 && (
              <SmallButton icon={<TerminalSquare size={14} />} label="打开终端" onClick={() => setRightStackTab('terminal')} />
            )}
          </InfoCard>
        </>
      )}
      {contextLedger && (
        <>
          <SectionLabel label="上下文用量" />
          <InfoCard>
            <InfoRow label="估算" value={`${formatNumber(contextLedger.estimated_tokens)} 令牌`} mono />
            <InfoRow label="实际" value={`${formatNumber(contextLedger.actual_tokens)} 令牌`} mono />
            <InfoRow label="压缩次数" value={String(contextLedger.compaction_count)} mono />
            <InfoRow
              label="原生附件"
              value={`${formatNumber(contextLedger.native_attachment_tokens)} 令牌 · ${contextLedger.native_attachment_count} 项`}
              mono
              tone={contextLedger.native_attachment_count > 0 ? 'accent' : 'muted'}
            />
            {visibleLedgerEntries.map((entry) => (
              <InfoRow
                key={entry.category}
                label={localizedLedgerLabel(entry.label)}
                value={`${formatNumber(entry.estimated_tokens)} 令牌 · ${entry.item_count} 项 · ${entry.source_count} 个来源`}
                mono
                title={entry.sources.length > 0 ? entry.sources.join(', ') : undefined}
              />
            ))}
          </InfoCard>
        </>
      )}
    </div>
  )
}

// ── Details Tab ────────────────────────────────────────────────

const RuntimeMetricsSection = () => {
  const messages = useAppStore((s) => s.messages)
  const agentProgress = useAppStore((s) => s.agentProgress)
  const inspectorEntries = useAppStore((s) => s.inspectorEntries)
  const cacheEntries = useMemo(() => inspectorEntries.filter(isCacheMetricEntry), [inspectorEntries])
  const summary = useMemo(
    () => buildRuntimeMetricSummary(messages, agentProgress, cacheEntries),
    [messages, agentProgress, cacheEntries],
  )
  if (!summary.hasSignals) return null
  return (
    <div style={{ display: 'grid', gap: 8 }}>
      <SectionLabel label="性能指标" />
      <InfoCard>
        <InfoRow label="首字时间" value={formatMaybeMs(summary.ttftMs)} mono tone={metricTone(summary.ttftMs, 2500)} />
        <InfoRow label="工具启动" value={formatMaybeMs(summary.avgToolStartLatencyMs)} mono tone={metricTone(summary.avgToolStartLatencyMs, 500)} />
        <InfoRow label="工具执行" value={formatMaybeMs(summary.avgToolExecMs)} mono />
        <InfoRow label="缓存收益" value={`${formatNumber(summary.cacheSavedMs)} ms${summary.cacheHitRate == null ? '' : ` · 命中 ${summary.cacheHitRate}%`}`} mono tone={summary.cacheSavedMs > 0 ? 'accent' : 'muted'} />
        <InfoRow label="批处理" value={summary.batchedGroups > 0 ? `${summary.batchedTools} 个工具 / ${summary.batchedGroups} 组` : '未分组调度'} tone={summary.batchedGroups > 0 ? 'accent' : 'muted'} />
        <InfoRow label="协作" value={coordinationMetricLabel(summary)} tone={summary.subagentSignals > 0 ? 'accent' : 'muted'} />
        <InfoRow label="预热" value={summary.prefetchSignals > 0 ? `${summary.prefetchSignals} 个缓存信号` : '无预热信号'} tone={summary.prefetchSignals > 0 ? 'accent' : 'muted'} />
        <InfoRow label="停顿" value={summary.stallSignals > 0 ? `${summary.stallSignals} 个信号` : '无'} tone={summary.stallSignals > 0 ? 'warning' : 'muted'} />
        <InfoRow label="恢复" value={summary.recoveryTotal > 0 ? `${summary.recoverySucceeded}/${summary.recoveryTotal} 成功` : '未触发恢复'} tone={summary.recoveryTotal > summary.recoverySucceeded ? 'warning' : summary.recoveryTotal > 0 ? 'accent' : 'muted'} />
        <InfoRow label="审批等待" value={summary.approvalSignals > 0 ? `${summary.approvalSignals} 次` : '无'} tone={summary.approvalSignals > 0 ? 'warning' : 'muted'} />
        <InfoRow label="追踪完整度" value={summary.traceCompleteness == null ? '—' : `${summary.traceCompleteness}% 有计时`} tone={summary.traceCompleteness != null && summary.traceCompleteness < 95 ? 'warning' : 'muted'} />
      </InfoCard>
    </div>
  )
}

const DetailsTab = ({ traceExportEnabled }: { traceExportEnabled: boolean }) => {
  const inspectorEntries = useAppStore((s) => s.inspectorEntries)
  const inspectorFocus = useAppStore((s) => s.inspectorFocus)
  const addInspectorEntry = useAppStore((s) => s.addInspectorEntry)
  const [filter, setFilter] = useState<'all' | 'provider' | 'tool' | 'cache' | 'usage'>('all')
  const importRef = useRef<HTMLInputElement>(null)
  const focusedEntry = useMemo(
    () => inspectorFocus ? inspectorEntries.find((e) => e.targetId === inspectorFocus.id) : null,
    [inspectorEntries, inspectorFocus],
  )
  const providerEntries = useMemo(() => inspectorEntries.filter(isProviderTraceEntry), [inspectorEntries])
  const cacheEntries = useMemo(() => inspectorEntries.filter(isCacheMetricEntry), [inspectorEntries])
  const providerRaws = useMemo(() => providerEntries.map((entry) => providerRawFromPayload(entry.payload)), [providerEntries])
  const visibleEntries = useMemo(() => inspectorEntries.filter((entry) => {
    if (filter === 'provider') return isProviderTraceEntry(entry)
    if (filter === 'tool') return entry.targetKind === 'tool_call'
    if (filter === 'cache') return entry.targetKind === 'cache'
    if (filter === 'usage') return isProviderTraceEntry(entry) || entry.targetKind === 'budget' || entry.targetKind === 'cache'
    return true
  }), [filter, inspectorEntries])
  const usageTotals = useMemo(() => providerEntries.reduce(
    (acc, entry) => {
      const usage = providerUsageSummary(providerRawFromPayload(entry.payload))
      acc.input += usage.input
      acc.ordinaryInput += promptCacheOrdinaryInputTokens(usage)
      acc.output += usage.output
      acc.cacheRead += usage.cacheRead
      acc.cacheWrite += usage.cacheWrite
      acc.cacheDeleted += usage.cacheDeleted ?? 0
      acc.promptCacheTotal += promptCacheEffectivePromptTokens(usage)
      acc.reasoning += usage.reasoning
      return acc
    },
    { input: 0, ordinaryInput: 0, output: 0, cacheRead: 0, cacheWrite: 0, cacheDeleted: 0, promptCacheTotal: 0, reasoning: 0 },
  ), [providerEntries])
  const sessionCacheHit = useMemo(() => providerCacheHitRate(usageTotals), [usageTotals])

  const importProviderTraceJsonl = async (file: File | undefined) => {
    if (!file) return
    const text = await file.text()
    let imported = 0
    for (const [index, line] of text.split(/\r?\n/).entries()) {
      const trimmed = line.trim()
      if (!trimmed) continue
      try {
        const payload = providerTracePayloadFromExport(safeJsonParse<unknown>(trimmed, null))
        if (!payload) continue
        addInspectorEntry({
          targetKind: 'provider',
          targetId: `imported-provider-${Date.now()}-${index}`,
          payload,
          timestamp: Date.now(),
        })
        imported += 1
      } catch {
        // Ignore malformed lines so partial JSONL exports can still be inspected.
      }
    }
    if (imported > 0) setFilter('provider')
  }

  return (
    <div style={{ display: 'grid', gap: 10 }}>
      <input
        ref={importRef}
        type="file"
        accept=".jsonl,.json,application/json,application/x-ndjson"
        style={{ display: 'none' }}
        onChange={(event) => {
          void importProviderTraceJsonl(event.target.files?.[0])
          event.target.value = ''
        }}
      />
      <SectionLabel label="原始诊断" />
      {!focusedEntry && inspectorEntries.length === 0 && (
        <InfoCard>
          <InfoRow label="记录" value="暂无诊断记录" />
          <SmallButton icon={<Upload size={14} />} label="导入 JSONL" onClick={() => importRef.current?.click()} />
        </InfoCard>
      )}
      {(focusedEntry || inspectorEntries.length > 0) && (
        <>
          {providerEntries.length > 0 && (
            <InfoCard>
              <InfoRow label="模型调用" value={`${providerEntries.length} 次`} />
              <InfoRow label="令牌" value={`${formatNumber(usageTotals.input)} 输入 / ${formatNumber(usageTotals.output)} 输出`} mono />
              <InfoRow label="提示词" value={`${formatNumber(usageTotals.ordinaryInput)} 普通 · ${formatNumber(usageTotals.cacheRead)} 缓存读取 · ${formatNumber(usageTotals.cacheWrite)} 缓存写入 · ${formatNumber(usageTotals.promptCacheTotal)} 总量`} mono />
              <InfoRow label="缓存" value={`${sessionCacheHit == null ? 'n/a' : `${sessionCacheHit}%`} 命中 · 读取 ${formatNumber(usageTotals.cacheRead)} · 写入 ${formatNumber(usageTotals.cacheWrite)}`} tone={sessionCacheHit ? 'accent' : 'muted'} />
              {usageTotals.cacheDeleted > 0 && <InfoRow label="已删除缓存" value={formatNumber(usageTotals.cacheDeleted)} mono />}
              <InfoRow label="推理令牌" value={formatNumber(usageTotals.reasoning)} mono />
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 7 }}>
                {traceExportEnabled && (
                  <>
                    <SmallButton icon={<Copy size={14} />} label="复制 JSONL" onClick={() => void navigator.clipboard?.writeText(providerTraceExportJsonl(providerRaws))} />
                    <SmallButton icon={<Download size={14} />} label="下载 JSONL" onClick={() => downloadText('minicode-provider-traces.jsonl', providerTraceExportJsonl(providerRaws), 'application/x-ndjson')} />
                  </>
                )}
                <SmallButton icon={<Upload size={14} />} label="导入 JSONL" onClick={() => importRef.current?.click()} />
              </div>
            </InfoCard>
          )}
          {providerEntries.length === 0 && (
            <InfoCard>
              <InfoRow label="模型调用" value="暂无追踪记录" />
              <SmallButton icon={<Upload size={14} />} label="导入 JSONL" onClick={() => importRef.current?.click()} />
            </InfoCard>
          )}
          {cacheEntries.length > 0 && <CacheDiagnosisCard entries={cacheEntries} />}
          <div style={filterBarStyle}>
            {(['all', 'provider', 'tool', 'cache', 'usage'] as const).map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => setFilter(item)}
                style={filterButtonStyle(filter === item)}
              >
                {({ all: '全部', provider: '模型', tool: '工具', cache: '缓存', usage: '用量' } as const)[item]}
              </button>
            ))}
          </div>
          {focusedEntry && (
            isProviderTraceEntry(focusedEntry)
              ? <ProviderTraceDetails entry={focusedEntry} previous={previousProviderEntry(providerEntries, focusedEntry)} traceExportEnabled={traceExportEnabled} />
              : focusedEntry.targetKind === 'cache'
                ? <CacheMetricDetails entry={focusedEntry} />
              : isPermissionDecisionEntry(focusedEntry)
                ? <PermissionDecisionDetails entry={focusedEntry} />
              : (
                <pre style={jsonBlockStyle}>
                  {JSON.stringify(focusedEntry.payload, null, 2)}
                </pre>
              )
          )}
          {visibleEntries.slice(-10).reverse().map((entry, i) => (
            <button
              key={`${entry.targetId}-${i}`}
              onClick={() => focusInspectorEntry(entry)}
              style={eventButtonStyle(entry.targetId === inspectorFocus?.id)}
            >
              <span style={{ color: 'var(--text-muted)' }}>{entry.targetKind}</span>
              <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--accent-primary)' }}>{entry.targetId.slice(0, 12)}</span>
              {entry.targetKind === 'provider' && (
                <span style={{ color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {providerEntryRowLabel(entry)}
                </span>
              )}
              {entry.targetKind === 'cache' && (
                <span style={{ color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {cacheMetricRowLabel(entry)}
                </span>
              )}
            </button>
          ))}
        </>
      )}
    </div>
  )
}

const isProviderTraceEntry = (entry: InspectorEntry): boolean =>
  entry.targetKind === 'provider' && String(entry.payload.kind || '') === 'provider_trace'

const isCacheMetricEntry = (entry: InspectorEntry): boolean =>
  entry.targetKind === 'cache' && String(entry.payload.type || '') === 'cache.lookup'

const isPermissionDecisionEntry = (entry: InspectorEntry): boolean =>
  entry.targetKind === 'tool_call' && String(entry.payload.event || '') === 'permission.decision'

const PermissionDecisionDetails = ({ entry }: { entry: InspectorEntry }) => {
  const capability = entry.payload.capability as Record<string, unknown> | undefined
  const matchedRule = entry.payload.matched_rule as Record<string, unknown> | undefined
  const scope = entry.payload.scope as Record<string, unknown> | undefined
  const scopeValue = [scope?.workspace_scope, scope?.boundary, scope?.target].filter(Boolean).join(' · ') || 'n/a'
  return (
    <InfoCard>
      <InfoRow label="Permission" value={String(entry.payload.decision || 'unknown')} tone={entry.payload.decision === 'deny' ? 'warning' : entry.payload.decision === 'allow' ? 'accent' : 'muted'} />
      <InfoRow label="Boundary" value={capability?.allowed === false ? `blocked · ${String(capability.reason || '')}` : String(capability?.reason || 'allowed')} />
      <InfoRow label="Policy" value={String(entry.payload.approval_policy || entry.payload.permission_level || 'unknown')} mono />
      <InfoRow label="Rule" value={[matchedRule?.source, matchedRule?.rule].filter(Boolean).join(' · ') || 'n/a'} mono />
      <InfoRow label="Risk" value={String(entry.payload.risk || 'unknown')} tone={entry.payload.risk === 'critical' || entry.payload.risk === 'high' ? 'warning' : 'muted'} />
      <InfoRow label="Scope" value={scopeValue} mono />
      <InfoRow label="Expiry" value={String(entry.payload.expiry || 'policy')} />
    </InfoCard>
  )
}

const CacheDiagnosisCard = ({ entries }: { entries: InspectorEntry[] }) => {
  const summary = summarizeCacheMetrics(entries)
  return (
    <InfoCard>
      <InfoRow label="Cache" value={`${summary.hits}/${summary.total} hits (${summary.hitRate}%)`} tone={summary.hits > 0 ? 'accent' : 'muted'} />
      <InfoRow label="Saved" value={`${formatNumber(summary.savedMs)} ms estimated`} mono />
      <InfoRow label="Layers" value={summary.layers.join(', ') || 'none'} mono />
      <SmallButton icon={<Database size={14} />} label="Show Cache" onClick={() => useAppStore.getState().setInspectorFocus({ kind: 'cache', id: entries[entries.length - 1]?.targetId || '' })} />
    </InfoCard>
  )
}

const CacheMetricDetails = ({ entry }: { entry: InspectorEntry }) => (
  <InfoCard>
    <InfoRow label="Layer" value={String(entry.payload.cache_layer || 'unknown')} mono />
    <InfoRow label="Tool" value={String(entry.payload.tool_name || 'provider')} mono />
    <InfoRow label="Result" value={entry.payload.hit ? 'hit' : entry.payload.stale ? 'stale miss' : 'miss'} tone={entry.payload.hit ? 'accent' : entry.payload.stale ? 'warning' : 'muted'} />
    <InfoRow label="Saved" value={`${formatNumber(Number(entry.payload.estimated_saved_ms || 0))} ms`} mono />
    <InfoRow label="Payload" value={`${formatNumber(Number(entry.payload.payload_size_bytes || 0))} bytes`} mono />
    <pre style={jsonBlockStyle}>{JSON.stringify(entry.payload, null, 2)}</pre>
  </InfoCard>
)

function summarizeCacheMetrics(entries: InspectorEntry[]): { total: number; hits: number; hitRate: number; savedMs: number; layers: string[] } {
  const total = entries.length
  const hits = entries.filter((entry) => Boolean(entry.payload.hit)).length
  const savedMs = entries.reduce((sum, entry) => sum + Number(entry.payload.estimated_saved_ms || 0), 0)
  const layers = Array.from(new Set(entries.map((entry) => String(entry.payload.cache_layer || '')).filter(Boolean))).slice(0, 4)
  return {
    total,
    hits,
    hitRate: total > 0 ? Math.round((hits / total) * 100) : 0,
    savedMs,
    layers,
  }
}

type RuntimeMetricSummary = {
  hasSignals: boolean
  ttftMs: number | null
  avgToolStartLatencyMs: number | null
  avgToolExecMs: number | null
  cacheSavedMs: number
  cacheHitRate: number | null
  batchedGroups: number
  batchedTools: number
  prefetchSignals: number
  stallSignals: number
  recoveryTotal: number
  recoverySucceeded: number
  approvalSignals: number
  subagentSignals: number
  cacheSpanSignals: number
  traceCompleteness: number | null
}

function buildRuntimeMetricSummary(
  messages: ChatMessage[],
  progress: AgentProgressEntry[],
  cacheEntries: InspectorEntry[],
): RuntimeMetricSummary {
  const toolRecords = messages.flatMap(getToolCallsFromMessage)
  const timestamps = [
    ...messages.map((message) => finiteTimestamp(message.timestamp)),
    ...progress.map((entry) => finiteTimestamp(entry.timestamp)),
  ].filter((value): value is number => value != null)
  const runStartedAt = timestamps.length > 0 ? Math.min(...timestamps) : null
  const firstToken = progress
    .filter((entry) => runtimeText(entry).match(/(?:provider[._\s-]*)?first[._\s-]*token|first[._\s-]*render|ttft/i))
    .sort((a, b) => a.timestamp - b.timestamp)[0]
  const ttftMs = runStartedAt != null && firstToken ? Math.max(0, firstToken.timestamp - runStartedAt) : null

  const toolStartLatencies = toolRecords
    .map((record) => {
      const startedAt = finiteTimestamp(record.startedAt)
      if (startedAt == null) return null
      const preparing = progress
        .filter((entry) => entry.toolCallId === record.id && finiteTimestamp(entry.timestamp) != null && entry.timestamp <= startedAt)
        .filter((entry) => entry.stage === 'tool' || runtimeText(entry).match(/prepar|dispatch|intent|guard|permission|approval|repair|waiting/i))
        .sort((a, b) => a.timestamp - b.timestamp)[0]
      return preparing ? Math.max(0, startedAt - preparing.timestamp) : null
    })
    .filter((value): value is number => value != null)

  const toolExecDurations = toolRecords
    .map((record) => {
      const startedAt = finiteTimestamp(record.startedAt)
      const finishedAt = finiteTimestamp(record.finishedAt)
      if (startedAt != null && finishedAt != null && finishedAt >= startedAt) return finishedAt - startedAt
      const duration = Number(record.durationMs || 0)
      return Number.isFinite(duration) && duration > 0 ? duration : null
    })
    .filter((value): value is number => value != null)

  const cacheSummary = summarizeCacheMetrics(cacheEntries)
  const grouped = new Map<string, number>()
  for (const record of toolRecords) {
    const groupId = String(record.groupId || '').trim()
    if (!groupId) continue
    grouped.set(groupId, (grouped.get(groupId) || 0) + 1)
  }
  const groupedCounts = Array.from(grouped.values()).filter((count) => count > 1)
  const timedTools = toolRecords.filter((record) => finiteTimestamp(record.startedAt) != null && (finiteTimestamp(record.finishedAt) != null || record.status === 'running')).length
  const traceCompleteness = toolRecords.length > 0 ? Math.round((timedTools / toolRecords.length) * 100) : progress.length > 0 ? 100 : null
  const prefetchSignals = cacheEntries.filter((entry) => {
    const saved = Number(entry.payload.estimated_saved_ms || 0)
    const layer = String(entry.payload.cache_layer || entry.payload.cacheLayer || '')
    const tool = String(entry.payload.tool_name || entry.payload.toolName || '')
    return Boolean(entry.payload.hit) && saved > 0 && /read|list|grep|glob|search|prefetch/i.test(`${layer} ${tool}`)
  }).length
  const progressTexts = progress.map(runtimeText)
  const stallSignals = progressTexts.filter((text) => /stall|slow|timeout|耗时|稍久|等待过久/i.test(text)).length
  const recoveryEntries = progress.filter((entry) => entry.phase === 'recover' || /recovery|recover|恢复/i.test(runtimeText(entry)))
  const recoverySucceeded = recoveryEntries.filter((entry) => entry.status === 'completed' || /succeeded|recovered|已恢复/i.test(runtimeText(entry))).length
  const approvalSignals = progress.filter((entry) => entry.phase === 'approval' || entry.stage === 'approval' || /approval|permission|审批|批准/i.test(runtimeText(entry))).length
  const subagentSignals = progress.filter((entry) => entry.phase === 'subagent').length
  const cacheSpanSignals = progress.filter((entry) => entry.phase === 'cache').length

  return {
    hasSignals: toolRecords.length > 0 || progress.length > 0 || cacheEntries.length > 0,
    ttftMs,
    avgToolStartLatencyMs: average(toolStartLatencies),
    avgToolExecMs: average(toolExecDurations),
    cacheSavedMs: cacheSummary.savedMs,
    cacheHitRate: cacheEntries.length > 0 ? cacheSummary.hitRate : null,
    batchedGroups: groupedCounts.length,
    batchedTools: groupedCounts.reduce((sum, count) => sum + count, 0),
    prefetchSignals,
    stallSignals,
    recoveryTotal: recoveryEntries.length,
    recoverySucceeded,
    approvalSignals,
    subagentSignals,
    cacheSpanSignals,
    traceCompleteness,
  }
}

const runtimeText = (entry: AgentProgressEntry): string =>
  `${entry.id} ${entry.stage} ${entry.phase || ''} ${entry.label || ''} ${entry.message || ''} ${entry.summary || ''} ${entry.detail || ''}`

const finiteTimestamp = (value: unknown): number | null => {
  const timestamp = Number(value)
  return Number.isFinite(timestamp) && timestamp > 0 ? timestamp : null
}

const average = (values: number[]): number | null =>
  values.length > 0 ? Math.round(values.reduce((sum, value) => sum + value, 0) / values.length) : null

const formatMaybeMs = (value: number | null): string =>
  value == null ? 'n/a' : `${formatNumber(value)} ms`

const metricTone = (value: number | null, warningAt: number): 'warning' | 'muted' =>
  value != null && value >= warningAt ? 'warning' : 'muted'

const coordinationMetricLabel = (summary: RuntimeMetricSummary): string => {
  const parts = [
    summary.subagentSignals > 0 ? `${summary.subagentSignals} 个子智能体阶段` : '',
    summary.cacheSpanSignals > 0 ? `${summary.cacheSpanSignals} 个缓存阶段` : '',
  ].filter(Boolean)
  return parts.length > 0 ? parts.join(' · ') : '无协作阶段'
}

const replayOutcomeLabel = (summary: RunReplaySummary): string => {
  if (summary.outcome === 'empty') return '暂无回放事件'
  if (summary.outcome === 'running') return `${summary.running} 项仍在运行`
  if (summary.outcome === 'needs_attention') return `${summary.failedOrBlocked} 项需要处理`
  return '回放已完成'
}

const cacheMetricRowLabel = (entry: InspectorEntry): string => {
  const layer = String(entry.payload.cache_layer || 'cache')
  const result = entry.payload.hit ? 'hit' : entry.payload.stale ? 'stale' : 'miss'
  const saved = Number(entry.payload.estimated_saved_ms || 0)
  return `${layer} · ${result}${saved > 0 ? ` · saved ${saved}ms` : ''}`
}

const providerRawFromPayload = (payload: Record<string, unknown>): ProviderRawMetadata => ({
  provider: typeof payload.provider === 'string' ? payload.provider : undefined,
  model: typeof payload.model === 'string' ? payload.model : undefined,
  finish_reason: typeof payload.finish_reason === 'string' ? payload.finish_reason : undefined,
  event_type: typeof payload.event_type === 'string' ? payload.event_type : undefined,
  usage: typeof payload.usage === 'object' && payload.usage !== null ? payload.usage as Record<string, unknown> : undefined,
  raw_usage: typeof payload.raw_usage === 'object' && payload.raw_usage !== null ? payload.raw_usage as Record<string, unknown> : undefined,
  citations: Array.isArray(payload.citations) ? payload.citations as ProviderRawMetadata['citations'] : undefined,
  search_sources: Array.isArray(payload.search_sources) ? payload.search_sources as ProviderRawMetadata['search_sources'] : undefined,
  container: typeof payload.container === 'object' && payload.container !== null ? payload.container as ProviderRawMetadata['container'] : undefined,
  refusal: typeof payload.refusal === 'object' && payload.refusal !== null ? payload.refusal as ProviderRawMetadata['refusal'] : undefined,
  output_items: Array.isArray(payload.output_items) ? payload.output_items as ProviderRawMetadata['output_items'] : undefined,
  provider_timeline: Array.isArray(payload.provider_timeline) ? payload.provider_timeline as ProviderRawMetadata['provider_timeline'] : undefined,
  request_summary: typeof payload.request_summary === 'object' && payload.request_summary !== null ? payload.request_summary as ProviderRawMetadata['request_summary'] : undefined,
  loop_metrics: typeof payload.loop_metrics === 'object' && payload.loop_metrics !== null ? payload.loop_metrics as ProviderRawMetadata['loop_metrics'] : undefined,
  safety: typeof payload.safety === 'object' && payload.safety !== null ? payload.safety as ProviderRawMetadata['safety'] : undefined,
  prompt_cache_diagnostic: typeof payload.prompt_cache_diagnostic === 'object' && payload.prompt_cache_diagnostic !== null ? payload.prompt_cache_diagnostic as ProviderRawMetadata['prompt_cache_diagnostic'] : undefined,
})

const previousProviderEntry = (entries: InspectorEntry[], focused: InspectorEntry): InspectorEntry | undefined => {
  const index = entries.findIndex((entry) => entry.targetId === focused.targetId)
  return index > 0 ? entries[index - 1] : undefined
}

const providerRowLabel = (entry: InspectorEntry): string => {
  const raw = providerRawFromPayload(entry.payload)
  const usage = providerUsageSummary(raw)
  return `${raw.provider || 'provider'} · ${raw.model || raw.request_summary?.model || 'model'} · ${formatNumber(usage.input)} in`
}

const providerEntryRowLabel = (entry: InspectorEntry): string => {
  if (isProviderTraceEntry(entry)) return providerRowLabel(entry)
  const data = entry.payload.data && typeof entry.payload.data === 'object'
    ? entry.payload.data as Record<string, unknown>
    : {}
  const event = String(entry.payload.span_event || entry.payload.event || entry.payload.type || 'provider event').trim()
  const status = String(entry.payload.status || '').trim()
  const finishReason = String(data.finish_reason || entry.payload.finish_reason || '').trim()
  const input = Number(data.input_tokens)
  const output = Number(data.output_tokens)
  const usage = Number.isFinite(input) || Number.isFinite(output)
    ? `${formatNumber(Number.isFinite(input) ? input : 0)} in / ${formatNumber(Number.isFinite(output) ? output : 0)} out`
    : ''
  return [event, status, finishReason, usage].filter(Boolean).join(' · ')
}

const ProviderTraceDetails = ({ entry, previous, traceExportEnabled }: { entry: InspectorEntry; previous?: InspectorEntry; traceExportEnabled: boolean }) => {
  const raw = providerRawFromPayload(entry.payload)
  const usage = providerUsageSummary(raw)
  const nativeUsage = providerNativeUsageDetails(raw)
  const hit = providerCacheHitRate(usage)
  const summary = raw.request_summary ?? {}
  const previousSummary = providerRawFromPayload(previous?.payload ?? {}).request_summary
  const diff = providerRequestDiff(previousSummary, summary)
  const diffSummary = providerRequestDiffSummary(previousSummary, summary)
  const diagnostics = providerTraceDiagnostics(raw)
  const timelineRows = providerTimelineRows(raw.provider_timeline)
  const promptSectionSummary = providerPromptSectionSummary(summary.prompt_section_summary)
  const promptLargestSections = providerPromptLargestSections(summary.prompt_section_summary)
  const largestTools = providerLargestToolsSummary(summary)
  const largestInputItems = providerLargestInputItemsSummary(summary)
  const duplicateInput = providerDuplicateInputSummary(summary)
  const promptSectionDelta = providerPromptSectionDeltaSummary(raw.prompt_cache_diagnostic?.prompt_section_delta)
  const promptCacheDiagnostic = providerPromptCacheDiagnosticSummary(raw.prompt_cache_diagnostic)
  const ordinaryInput = promptCacheOrdinaryInputTokens(usage)
  const promptCacheTotal = promptCacheEffectivePromptTokens(usage)
  const citationCount = raw.citations?.length ?? 0
  const documentCitationCount = raw.citations?.filter((citation) => !citation.url).length ?? 0
  const hasNativeCacheBreakdown = nativeUsage.cache5m > 0 || nativeUsage.cache1h > 0
  const hasHostedToolCounts = nativeUsage.webSearchRequests > 0 || nativeUsage.webFetchRequests > 0
  const exportJson = () => providerTraceExportJson(raw, previousSummary)
  const safeRequestJson = () => providerSafeRequestJson(raw)
  const curlSkeleton = () => providerCurlSkeleton(raw)
  return (
    <div style={{ display: 'grid', gap: 8 }}>
      <InfoCard>
        <InfoRow label="Provider" value={String(raw.provider || 'unknown')} />
        <InfoRow label="Model" value={String(raw.model || summary.model || 'unknown')} mono />
        <InfoRow label="Finish" value={String(raw.finish_reason || 'unknown')} tone={raw.finish_reason ? 'muted' : 'warning'} />
        <InfoRow label="API" value={providerRequestModeSummary(raw)} mono />
        <InfoRow label="Usage" value={`${formatNumber(usage.input)} in / ${formatNumber(usage.output)} out / ${formatNumber(usage.reasoning)} reasoning`} mono />
        <InfoRow label="Cache" value={`${hit == null ? 'n/a' : `${hit}%`} hit · ${formatNumber(ordinaryInput)} ordinary · ${formatNumber(usage.cacheRead)} read · ${formatNumber(usage.cacheWrite)} write · ${formatNumber(promptCacheTotal)} total${usage.cacheDeleted ? ` · ${formatNumber(usage.cacheDeleted)} deleted` : ''}`} tone={hit ? 'accent' : 'muted'} />
        {hasNativeCacheBreakdown && <InfoRow label="缓存 TTL" value={`5m ${formatNumber(nativeUsage.cache5m)} · 1h ${formatNumber(nativeUsage.cache1h)}`} mono />}
        {hasHostedToolCounts && <InfoRow label="托管工具" value={`search ${formatNumber(nativeUsage.webSearchRequests)} · fetch ${formatNumber(nativeUsage.webFetchRequests)}`} mono />}
        {(nativeUsage.serviceTier || nativeUsage.inferenceGeo) && <InfoRow label="服务层" value={[nativeUsage.serviceTier, nativeUsage.inferenceGeo].filter(Boolean).join(' · ')} mono />}
        {(raw.search_sources?.length ?? 0) > 0 && <InfoRow label="Provider 来源" value={providerSearchSourcesSummary(raw)} />}
        {citationCount > 0 && <InfoRow label="Provider 引用" value={`${citationCount}${documentCitationCount > 0 ? ` · ${documentCitationCount} document location${documentCitationCount === 1 ? '' : 's'}` : ''}`} />}
        {hasProviderContainerMetadata(raw.container) && <InfoRow label="Container" value={providerContainerSummary(raw)} mono />}
        {hasProviderRefusalMetadata(raw.refusal) && <InfoRow label="拒绝" value={providerRefusalSummary(raw)} tone="warning" />}
        <InfoRow label="Loop" value={providerLoopMetricsSummary(raw)} tone={(raw.loop_metrics?.provider_call_count ?? 0) >= 6 || (raw.loop_metrics?.tool_batch_count ?? 0) >= 5 ? 'warning' : 'muted'} mono />
        <InfoRow label="Cache Key" value={providerCacheDiagnosis(raw)} mono />
        <InfoRow label="Prompt" value={`${summary.instructions_hash || 'no hash'} · ${formatNumber(summary.instructions_len || 0)} chars`} mono />
        <InfoRow label="Prompt Wire" value={providerInstructionsTransportSummary(raw)} mono />
        <InfoRow label="Tools" value={`${summary.tools_hash || 'no hash'} · ${summary.tools_len ?? 0} tools · ${formatNumber(summary.tools_chars || 0)} chars`} mono />
        <InfoRow label="Tool Size" value={largestTools} mono />
        <InfoRow label="Input" value={`${formatNumber(summary.input_items_len || 0)} items · ${formatNumber(summary.input_chars || 0)} chars`} mono />
        <InfoRow label="Input Size" value={largestInputItems} mono />
        <InfoRow label="Input Dup" value={duplicateInput} tone={summary.duplicate_input_content?.length ? 'warning' : 'muted'} mono />
        <InfoRow label="Sections" value={promptSectionSummary} mono />
        <InfoRow label="Largest" value={promptLargestSections} mono />
        {raw.prompt_cache_diagnostic?.reason && <InfoRow label="Cache Break" value={promptCacheDiagnostic} tone="warning" mono />}
        {traceExportEnabled && (
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 7 }}>
            <SmallButton icon={<Copy size={14} />} label="Copy Trace" onClick={() => void navigator.clipboard?.writeText(exportJson())} />
            <SmallButton icon={<Copy size={14} />} label="Request JSON" onClick={() => void navigator.clipboard?.writeText(safeRequestJson())} />
            <SmallButton icon={<Copy size={14} />} label="cURL Skeleton" onClick={() => void navigator.clipboard?.writeText(curlSkeleton())} />
            <SmallButton icon={<Download size={14} />} label="Download" onClick={() => downloadText(`minicode-provider-trace-${safeFilePart(entry.targetId)}.json`, exportJson(), 'application/json')} />
          </div>
        )}
      </InfoCard>
      <div style={sequenceStyle}>{providerOutputSequence(raw.output_items)}</div>
      <div style={phaseCountsStyle}>{providerOutputPhaseCounts(raw.output_items)}</div>
      <div style={eventCountsStyle}>{providerTimelineEventCounts(raw.provider_timeline)}</div>
      <div style={lifecycleStyle}>{providerResponseLifecycle(raw.provider_timeline)}</div>
      {timelineRows.length > 0 && (
        <div style={timelineRowsStyle}>
          {timelineRows.map((row, index) => (
            <div key={`${row.event}-${index}`} style={timelineRowStyle(row.tone)}>
              <span style={timelineEventNameStyle}>{row.event}</span>
              <span style={timelineEventDetailStyle}>{row.detail}</span>
            </div>
          ))}
        </div>
      )}
      <div style={timelineStyle}>{providerTimelineSequence(raw.provider_timeline)}</div>
      <div style={diagnosticsStyle}>{diagnostics.join(' · ')}</div>
      <div style={diffSummaryStyle}>{diffSummary.join(' · ')}</div>
      {raw.prompt_cache_diagnostic?.prompt_section_delta?.status === 'changed' && (
        <>
          <div style={promptSectionDeltaStyle}>{promptSectionDelta.overview}</div>
          <div style={promptSectionDeltaStyle}>{promptSectionDelta.layerSummary}</div>
          <div style={promptSectionDeltaStyle}>{promptSectionDelta.changedSections}</div>
        </>
      )}
      <InfoCard>
        {diff.map((item) => (
          <InfoRow
            key={item.label}
            label={shortDiffLabel(item.label)}
            value={item.changed ? `${formatDiffValue(item.before)} -> ${formatDiffValue(item.after)}` : `${formatDiffValue(item.after)} unchanged`}
            tone={item.changed ? 'warning' : 'muted'}
            mono
          />
        ))}
      </InfoCard>
      <pre style={jsonBlockStyle}>{JSON.stringify(sanitizeProviderTraceExportValue(entry.payload), null, 2)}</pre>
    </div>
  )
}

const shortDiffLabel = (label: string): string =>
  ({
    instructions_hash: 'Prompt',
    instructions_full_hash: 'PromptFull',
    instructions_len: 'Chars',
    tools_hash: 'Tools',
    tools_len: 'Count',
    tools_chars: 'ToolChars',
    tool_schema_hashes: 'Schema',
    prompt_cache_key_present: 'CacheKey',
    prompt_cache_key_hash: 'CacheHash',
    turn_aborted_marker_present: 'Abort',
    input_items_len: 'Input',
    input_chars: 'InputChars',
    largest_input_items: 'InputMax',
    duplicate_input_content: 'InputDup',
    input_item_counts: 'Roles',
    metadata_keys: 'Meta',
    prompt_section_summary: 'Sections',
  } as Record<string, string>)[label] ?? label

const formatDiffValue = (value: unknown): string => {
  if (Array.isArray(value)) return value.join(',') || 'none'
  if (value && typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => `${key}:${String(item)}`)
      .join(',') || 'none'
  }
  if (value == null || value === '') return 'none'
  return String(value)
}

const formatNumber = (value: number): string => Math.max(0, Number(value || 0)).toLocaleString()

const localizedLedgerLabel = (label: string): string => ({
  history: '对话历史',
  'tool results': '工具结果',
  tools: '工具定义',
  system: '系统提示',
  skills: '技能',
  retrieved: '检索内容',
  attachments: '附件',
}[label.trim().toLowerCase()] ?? label)

const downloadText = (filename: string, content: string, type: string): void => {
  const blob = new Blob([content], { type })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

const safeFilePart = (value: string): string =>
  (value || 'trace').replace(/[^a-z0-9._-]+/gi, '-').slice(0, 80) || 'trace'

// ── Styles ─────────────────────────────────────────────────────

const advancedToggleStyle: React.CSSProperties = {
  width: '100%',
  minHeight: 52,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  gap: 12,
  padding: '9px 11px',
  border: '1px solid var(--border-subtle)',
  borderRadius: 'var(--radius-md, 9px)',
  background: 'var(--surface-soft)',
  color: 'var(--text-secondary)',
  cursor: 'pointer',
  textAlign: 'left',
  font: 'inherit',
}


const eventButtonStyle = (active: boolean): React.CSSProperties => ({
  display: 'grid',
  gridTemplateColumns: 'auto auto minmax(0, 1fr)',
  gap: 8,
  padding: '4px 0',
  background: active ? 'color-mix(in oklch, var(--accent-orange) 6%, transparent)' : 'transparent',
  border: 0,
  borderTop: '1px solid var(--border-subtle)',
  borderRadius: 0,
  fontSize: 'var(--text-xs)',
  cursor: 'pointer',
  textAlign: 'left',
})

const filterBarStyle: React.CSSProperties = {
  display: 'flex',
  gap: 5,
  flexWrap: 'wrap',
}

const sectionHeaderWithActionsStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  gap: 8,
  flexWrap: 'wrap',
}

const filterButtonStyle = (active: boolean): React.CSSProperties => ({
  padding: '3px 7px',
  border: '1px solid var(--border-subtle)',
  borderRadius: 'var(--radius-sm, 4px)',
  background: active ? 'var(--surface-elevated)' : 'transparent',
  color: active ? 'var(--text-primary)' : 'var(--text-muted)',
  fontSize: 'var(--text-xs)',
  cursor: 'pointer',
})

const timelineShellStyle: React.CSSProperties = {
  border: '1px solid var(--border-subtle)',
  borderRadius: 'var(--radius-sm, 4px)',
  background: 'var(--surface-page)',
  overflow: 'hidden',
}

const sessionReplayStatusStyle: React.CSSProperties = {
  color: 'var(--text-muted)',
  fontSize: 'var(--text-xs)',
  lineHeight: 1.4,
}

const replayListStyle: React.CSSProperties = {
  display: 'grid',
  gap: 1,
  border: '1px solid var(--border-subtle)',
  borderRadius: 'var(--radius-sm, 4px)',
  overflow: 'hidden',
  background: 'var(--surface-page)',
}

const replayRowStyle = (status: RunReplayEvent['status']): React.CSSProperties => ({
  display: 'grid',
  gridTemplateColumns: '44px minmax(86px, 0.8fr) minmax(0, 1.4fr) auto',
  gap: 8,
  alignItems: 'center',
  padding: '5px 7px',
  borderTop: '1px solid var(--border-subtle)',
  background:
    status === 'failed' || status === 'blocked' || status === 'partial'
      ? 'color-mix(in oklch, var(--state-warning) 7%, transparent)'
      : status === 'running'
        ? 'color-mix(in oklch, var(--accent-primary) 5%, transparent)'
        : 'transparent',
  color: status === 'failed' || status === 'blocked' || status === 'partial' ? 'var(--state-warning)' : 'var(--text-secondary)',
  fontSize: 'var(--text-xs)',
})

const replaySeqStyle: React.CSSProperties = {
  fontFamily: 'var(--font-mono)',
  color: 'var(--text-muted)',
}

const replayEventStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  fontFamily: 'var(--font-mono)',
}

const replayLabelStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  color: 'var(--text-primary)',
}

const replayDurationStyle: React.CSSProperties = {
  color: 'var(--text-muted)',
  fontFamily: 'var(--font-mono)',
}

const replayMoreStyle: React.CSSProperties = {
  padding: '5px 7px',
  color: 'var(--text-muted)',
  fontSize: 'var(--text-xs)',
  borderTop: '1px solid var(--border-subtle)',
}

const sequenceStyle: React.CSSProperties = {
  padding: '6px 7px',
  border: '1px solid var(--border-subtle)',
  borderRadius: 'var(--radius-sm, 4px)',
  background: 'var(--surface-page)',
  color: 'var(--text-secondary)',
  fontFamily: 'var(--font-mono)',
  fontSize: 'var(--text-xs)',
  lineHeight: 1.45,
  overflowWrap: 'anywhere',
}

const timelineStyle: React.CSSProperties = {
  ...sequenceStyle,
  background: 'color-mix(in oklch, var(--accent-primary) 5%, var(--surface-page))',
}

const eventCountsStyle: React.CSSProperties = {
  ...sequenceStyle,
  background: 'color-mix(in oklch, var(--state-info) 5%, var(--surface-page))',
}

const phaseCountsStyle: React.CSSProperties = {
  ...sequenceStyle,
  background: 'color-mix(in oklch, var(--accent-primary) 4%, var(--surface-page))',
}

const lifecycleStyle: React.CSSProperties = {
  ...sequenceStyle,
  background: 'color-mix(in oklch, var(--state-success) 5%, var(--surface-page))',
}

const timelineRowsStyle: React.CSSProperties = {
  display: 'grid',
  gap: 1,
  border: '1px solid var(--border-subtle)',
  borderRadius: 'var(--radius-sm, 4px)',
  overflow: 'hidden',
  background: 'var(--surface-page)',
}

const timelineRowStyle = (tone: 'muted' | 'accent' | 'warning'): React.CSSProperties => ({
  display: 'grid',
  gridTemplateColumns: 'minmax(145px, 0.9fr) minmax(0, 1fr)',
  gap: 8,
  padding: '5px 7px',
  background:
    tone === 'warning'
      ? 'color-mix(in oklch, var(--state-warning) 7%, transparent)'
      : tone === 'accent'
        ? 'color-mix(in oklch, var(--accent-primary) 5%, transparent)'
        : 'transparent',
  color: tone === 'warning' ? 'var(--state-warning)' : 'var(--text-secondary)',
  fontSize: 'var(--text-xs)',
  borderTop: '1px solid var(--border-subtle)',
})

const timelineEventNameStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  fontFamily: 'var(--font-mono)',
}

const timelineEventDetailStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  color: 'var(--text-muted)',
}

const diagnosticsStyle: React.CSSProperties = {
  ...sequenceStyle,
  background: 'color-mix(in oklch, var(--state-warning) 5%, var(--surface-page))',
}

const diffSummaryStyle: React.CSSProperties = {
  ...sequenceStyle,
  background: 'color-mix(in oklch, var(--accent-orange) 5%, var(--surface-page))',
}

const promptSectionDeltaStyle: React.CSSProperties = {
  ...sequenceStyle,
  background: 'color-mix(in oklch, var(--accent-primary) 4%, var(--surface-page))',
}

const jsonBlockStyle: React.CSSProperties = {
  fontSize: 'var(--text-xs)',
  fontFamily: 'var(--font-mono)',
  color: 'var(--text-secondary)',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  maxHeight: 'min(48vh, 420px)',
  overflow: 'auto',
  margin: 0,
  padding: 7,
  background: 'var(--surface-page)',
  border: '1px solid var(--border-subtle)',
  borderRadius: 'var(--radius-sm, 4px)',
}
