/**
 * Inspector tab — combines ContextTab (session/workspace/runtime info)
 * and DetailsTab (inspector entries / recent tool calls).
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Copy, Database, Download, FolderOpen, GitBranch, TerminalSquare, Upload } from 'lucide-react'
import { isDesktop, revealPath } from '../../desktop/runtime'
import { fetchWorkspaceGitStatus, type WorkspaceGitStatusResponse } from '../../protocol/workspace'
import { useAppStore } from '../../stores'
import type { AgentProgressEntry, ChatMessage, InspectorEntry, ProviderRawMetadata } from '../../stores/types'
import { branchDisplayName, workspaceDisplayName } from '../../lib/workspace-display'
import { providerCacheDiagnosis, providerCacheHitRate, providerContinuationDetail, providerContinuationLabel, providerCurlSkeleton, providerDuplicateInputSummary, providerInstructionsTransportSummary, providerLargestInputItemsSummary, providerLargestToolsSummary, providerLoopMetricsSummary, providerOutputPhaseCounts, providerOutputSequence, providerPromptCacheDiagnosticSummary, providerPromptLargestSections, providerPromptSectionDeltaSummary, providerPromptSectionSummary, providerRequestDiff, providerRequestDiffSummary, providerRequestModeSummary, providerResponseLifecycle, providerSafeRequestJson, providerTimelineEventCounts, providerTimelineRows, providerTimelineSequence, providerTraceDiagnostics, providerTraceExportJson, providerTraceExportJsonl, providerTracePayloadFromExport, providerUsageSummary } from '../../chat/providerTrace'
import { promptCacheEffectivePromptTokens } from '../../chat/cacheUsage'
import { capabilityFeatureEnabled } from '../../protocol/capabilities'
import { fetchReplayExport } from '../../protocol/replay'
import { getWebSocket } from '../../hooks/useWebSocket'
import { InfoCard, InfoRow, SectionLabel, SmallButton } from '../SidebarShared'
import { ToolCallTimeline, buildRunReplayEvents, buildRunReplaySummary, runTimelineExportJsonl, runTimelineReplayJsonl, type RunReplayEvent, type RunReplaySummary } from '../../chat/tool-calls/ToolCallTimeline'
import { getToolCallsFromMessage } from '../../lib/content-blocks'
import { projectMessagesToActivityItems } from '../../agent-loop/projection/project-activity-items'

export const InspectorTab = () => (
  <InspectorTabContent />
)

const InspectorTabContent = () => {
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities)
  const traceExportEnabled = capabilityFeatureEnabled(runtimeCapabilities, 'agent_trace_export_v1', false)
  return (
    <div style={{ display: 'grid', gap: 12 }}>
      <ContextTab />
      <RunTimelineSection traceExportEnabled={traceExportEnabled} />
      <RuntimeMetricsSection />
      <DetailsTab traceExportEnabled={traceExportEnabled} />
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
  const activityItems = useMemo(() => projectMessagesToActivityItems(messages), [messages])
  const activityAttention = activityItems.filter((item) => item.hasFailure || item.hasPendingUserAction).length
  const exportJsonl = () => runTimelineExportJsonl(messages, agentProgress)
  const replayJsonl = () => runTimelineReplayJsonl(messages, agentProgress)
  const copySessionReplay = async () => {
    const sessionId = getWebSocket()?.sessionId
    if (!sessionId) {
      setSessionReplayStatus('No active session')
      return
    }
    setSessionReplayStatus('Loading session replay...')
    try {
      const payload = await fetchReplayExport({ sessionId, conversationId, limit: 500 })
      await navigator.clipboard?.writeText(JSON.stringify(payload, null, 2))
      setSessionReplayStatus(`Copied ${payload.event_count} session events`)
    } catch (error) {
      setSessionReplayStatus(error instanceof Error ? error.message : String(error))
    }
  }
  return (
    <div style={{ display: 'grid', gap: 8 }}>
      <div style={sectionHeaderWithActionsStyle}>
        <SectionLabel label="Run Timeline" />
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          <SmallButton
            icon={<TerminalSquare size={12} />}
            label={expanded ? 'Hide events' : 'Show recent events'}
            onClick={() => setExpanded((value) => !value)}
          />
        {traceExportEnabled && (
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            <SmallButton icon={<Copy size={12} />} label="Copy JSONL" onClick={() => void navigator.clipboard?.writeText(exportJsonl())} />
            <SmallButton icon={<Download size={12} />} label="Download JSONL" onClick={() => downloadText('minicode-run-timeline.jsonl', exportJsonl(), 'application/x-ndjson')} />
            <SmallButton icon={<Copy size={12} />} label="Copy Replay" onClick={() => void navigator.clipboard?.writeText(replayJsonl())} />
            <SmallButton icon={<Download size={12} />} label="Download Replay" onClick={() => downloadText('minicode-run-replay.jsonl', replayJsonl(), 'application/x-ndjson')} />
            <SmallButton icon={<Copy size={12} />} label="Copy Session Replay" onClick={() => void copySessionReplay()} />
          </div>
        )}
        </div>
      </div>
      {traceExportEnabled && sessionReplayStatus && <div style={sessionReplayStatusStyle}>{sessionReplayStatus}</div>}
      <InfoCard>
        <InfoRow label="Events" value={replayEvents.length === 0 ? 'No runtime events' : `${replayEvents.length} events`} mono />
        <InfoRow label="Activity items" value={activityItems.length === 0 ? 'No canonical items' : `${activityItems.length} canonical`} tone={activityAttention > 0 ? 'warning' : 'muted'} mono />
        <InfoRow label="Status" value={replaySummary.outcome === 'needs_attention' ? `${replaySummary.failedOrBlocked} need attention` : replaySummary.outcome === 'running' ? `${replaySummary.running} running` : replaySummary.outcome === 'completed' ? 'Completed' : 'Idle'} tone={replaySummary.outcome === 'needs_attention' ? 'warning' : replaySummary.outcome === 'running' ? 'accent' : 'muted'} />
        <InfoRow label="Span" value={replaySummary.spanMs == null ? 'n/a' : `${(replaySummary.spanMs / 1000).toFixed(1)}s`} mono />
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
      <SectionLabel label="Replay View" />
      <InfoCard>
        <InfoRow label="Events" value={`${summary.events} replayable`} mono />
        <InfoRow label="Coverage" value={`${summary.coveragePercent}% timed · ${summary.phases.join(', ') || 'none'}`} tone={summary.coveragePercent >= 95 ? 'accent' : 'warning'} mono />
        <InfoRow label="Outcome" value={replayOutcomeLabel(summary)} tone={summary.outcome === 'needs_attention' ? 'warning' : summary.outcome === 'completed' ? 'accent' : 'muted'} />
        <InfoRow label="Span" value={summary.spanMs == null ? 'n/a' : `${(summary.spanMs / 1000).toFixed(2)}s`} mono />
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
          <div style={replayMoreStyle}>+{events.length - visibleEvents.length} more replay events</div>
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
  const branch = conversation?.gitBranch || workspaceGit?.branch || 'No branch'
  const hasWorkspacePath = Boolean(workspacePath.trim())
  const displayWorkspace = workspaceDisplayName(workspacePath, 'Computer')
  const displayBranch = branchDisplayName(branch) || 'No branch'
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
    fetchWorkspaceGitStatus()
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
          <SectionLabel label="Session" />
          <InfoCard>
            <InfoRow label="Conversation" value={conversation?.title || conversationId || 'Untitled'} />
            {hasWorkspacePath && (
              <InfoRow
                label="Isolation"
                value={conversation?.gitIsolated || workspaceGit?.isWorktree ? 'Protected workspace' : 'Shared workspace'}
                tone={conversation?.gitIsolated || workspaceGit?.isWorktree ? 'accent' : 'muted'}
              />
            )}
            {displayBranch !== 'No branch' && <InfoRow label="Branch" value={displayBranch} mono />}
          </InfoCard>
        </>
      )}
      {hasWorkspaceRows && (
        <>
          <SectionLabel label="Workspace" />
          <InfoCard>
            <InfoRow label="Path" value={displayWorkspace} mono />
            <InfoRow
              label="Changes"
              value={gitLoading ? 'Checking...' : gitError ? 'Unavailable' : changedCount == null ? 'Unknown' : changedCount === 0 ? 'Clean' : `${changedCount} changed`}
              tone={gitError || (changedCount && changedCount > 0) ? 'warning' : 'muted'}
            />
            {gitError && (
              <div style={{ color: 'var(--state-warning)', background: 'color-mix(in oklch, var(--state-warning) 9%, transparent)', border: '1px solid color-mix(in oklch, var(--state-warning) 32%, transparent)', borderRadius: 'var(--radius-sm, 4px)', padding: '6px 8px', fontSize: 'var(--text-xs)', lineHeight: 1.4 }}>
                Git status failed: {gitError}
              </div>
            )}
            <div style={{ display: 'flex', gap: 6, marginTop: 8, flexWrap: 'wrap' }}>
              <SmallButton icon={<FolderOpen size={12} />} label="Reveal" disabled={!isDesktop()} onClick={() => void revealPath(workspacePath)} />
              <SmallButton icon={<Copy size={12} />} label="Copy" onClick={() => void navigator.clipboard?.writeText(workspacePath)} />
              <SmallButton icon={<GitBranch size={12} />} label="Refresh" onClick={refreshGitStatus} />
            </div>
          </InfoCard>
        </>
      )}
      {hasRuntimeRows && (
        <>
          <SectionLabel label="Runtime" />
          <InfoCard>
            {contextPercent != null && (
              <InfoRow label="Context" value={`${contextPercent}% (${contextUsage?.used}/${contextUsage?.limit})`} tone={contextPercent >= 85 ? 'warning' : 'muted'} />
            )}
            {contextUsage?.compactedAt && (
              <InfoRow
                label="Compact"
                value={`${new Date(contextUsage.compactedAt).toLocaleTimeString()}: ${contextUsage.compactSummary || 'Done'}`}
                tone="accent"
              />
            )}
            {terminalSessions.length > 0 && <InfoRow label="Terminals" value={String(terminalSessions.length)} />}
            {activeEditorPath && <InfoRow label="Editor" value={activeEditorPath} mono />}
            {terminalSessions.length > 0 && (
              <SmallButton icon={<TerminalSquare size={12} />} label="Open Terminal" onClick={() => setRightStackTab('terminal')} />
            )}
          </InfoCard>
        </>
      )}
      {contextLedger && (
        <>
          <SectionLabel label="Context Ledger" />
          <InfoCard>
            <InfoRow label="Estimated" value={`${formatNumber(contextLedger.estimated_tokens)} tok`} mono />
            <InfoRow label="Actual" value={`${formatNumber(contextLedger.actual_tokens)} tok`} mono />
            <InfoRow label="Compactions" value={String(contextLedger.compaction_count)} mono />
            <InfoRow
              label="Native attachments"
              value={`${formatNumber(contextLedger.native_attachment_tokens)} tok · ${contextLedger.native_attachment_count} items`}
              mono
              tone={contextLedger.native_attachment_count > 0 ? 'accent' : 'muted'}
            />
            {visibleLedgerEntries.map((entry) => (
              <InfoRow
                key={entry.category}
                label={entry.label}
                value={`${formatNumber(entry.estimated_tokens)} tok · ${entry.item_count} items · ${entry.source_count} sources`}
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
      <SectionLabel label="Runtime Metrics" />
      <InfoCard>
        <InfoRow label="TTFT" value={formatMaybeMs(summary.ttftMs)} mono tone={metricTone(summary.ttftMs, 2500)} />
        <InfoRow label="Tool Start" value={formatMaybeMs(summary.avgToolStartLatencyMs)} mono tone={metricTone(summary.avgToolStartLatencyMs, 500)} />
        <InfoRow label="Tool Exec" value={formatMaybeMs(summary.avgToolExecMs)} mono />
        <InfoRow label="Cache Saved" value={`${formatNumber(summary.cacheSavedMs)} ms${summary.cacheHitRate == null ? '' : ` · ${summary.cacheHitRate}% hit`}`} mono tone={summary.cacheSavedMs > 0 ? 'accent' : 'muted'} />
        <InfoRow label="Batching" value={summary.batchedGroups > 0 ? `${summary.batchedTools} tools in ${summary.batchedGroups} groups` : 'No grouped dispatch'} tone={summary.batchedGroups > 0 ? 'accent' : 'muted'} />
        <InfoRow label="Coordination" value={coordinationMetricLabel(summary)} tone={summary.workflowSignals > 0 || summary.subagentSignals > 0 ? 'accent' : 'muted'} />
        <InfoRow label="Prefetch" value={summary.prefetchSignals > 0 ? `${summary.prefetchSignals} warm cache signals` : 'No warm cache signal'} tone={summary.prefetchSignals > 0 ? 'accent' : 'muted'} />
        <InfoRow label="Stalls" value={summary.stallSignals > 0 ? `${summary.stallSignals} signals` : 'None'} tone={summary.stallSignals > 0 ? 'warning' : 'muted'} />
        <InfoRow label="Recovery" value={summary.recoveryTotal > 0 ? `${summary.recoverySucceeded}/${summary.recoveryTotal} succeeded` : 'No recovery'} tone={summary.recoveryTotal > summary.recoverySucceeded ? 'warning' : summary.recoveryTotal > 0 ? 'accent' : 'muted'} />
        <InfoRow label="Approvals" value={summary.approvalSignals > 0 ? `${summary.approvalSignals} waits` : 'No approval wait'} tone={summary.approvalSignals > 0 ? 'warning' : 'muted'} />
        <InfoRow label="Trace" value={summary.traceCompleteness == null ? 'n/a' : `${summary.traceCompleteness}% timed`} tone={summary.traceCompleteness != null && summary.traceCompleteness < 95 ? 'warning' : 'muted'} />
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
    if (filter === 'provider') return entry.targetKind === 'provider'
    if (filter === 'tool') return entry.targetKind === 'tool_call'
    if (filter === 'cache') return entry.targetKind === 'cache'
    if (filter === 'usage') return entry.targetKind === 'provider' || entry.targetKind === 'budget' || entry.targetKind === 'cache'
    return true
  }), [filter, inspectorEntries])
  const usageTotals = useMemo(() => providerEntries.reduce(
    (acc, entry) => {
      const usage = providerUsageSummary(providerRawFromPayload(entry.payload))
      acc.input += usage.input
      acc.output += usage.output
      acc.cacheRead += usage.cacheRead
      acc.cacheWrite += usage.cacheWrite
      acc.promptCacheTotal += promptCacheEffectivePromptTokens(usage)
      acc.reasoning += usage.reasoning
      return acc
    },
    { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, promptCacheTotal: 0, reasoning: 0 },
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
        const payload = providerTracePayloadFromExport(JSON.parse(trimmed))
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
      <SectionLabel label="Inspector" />
      {!focusedEntry && inspectorEntries.length === 0 && (
        <InfoCard>
          <InfoRow label="Entries" value="No inspector entries" />
          <SmallButton icon={<Upload size={12} />} label="Import JSONL" onClick={() => importRef.current?.click()} />
        </InfoCard>
      )}
      {(focusedEntry || inspectorEntries.length > 0) && (
        <>
          {providerEntries.length > 0 && (
            <InfoCard>
              <InfoRow label="Provider" value={`${providerEntries.length} calls`} />
              <InfoRow label="Tokens" value={`${formatNumber(usageTotals.input)} in / ${formatNumber(usageTotals.output)} out`} mono />
              <InfoRow label="Cache" value={`${sessionCacheHit == null ? 'n/a' : `${sessionCacheHit}%`} hit · ${formatNumber(usageTotals.cacheRead)} read`} tone={sessionCacheHit ? 'accent' : 'muted'} />
              <InfoRow label="Reasoning" value={formatNumber(usageTotals.reasoning)} mono />
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 7 }}>
                {traceExportEnabled && (
                  <>
                    <SmallButton icon={<Copy size={12} />} label="Copy JSONL" onClick={() => void navigator.clipboard?.writeText(providerTraceExportJsonl(providerRaws))} />
                    <SmallButton icon={<Download size={12} />} label="Download JSONL" onClick={() => downloadText('minicode-provider-traces.jsonl', providerTraceExportJsonl(providerRaws), 'application/x-ndjson')} />
                  </>
                )}
                <SmallButton icon={<Upload size={12} />} label="Import JSONL" onClick={() => importRef.current?.click()} />
              </div>
            </InfoCard>
          )}
          {providerEntries.length === 0 && (
            <InfoCard>
              <InfoRow label="Provider" value="No provider traces yet" />
              <SmallButton icon={<Upload size={12} />} label="Import JSONL" onClick={() => importRef.current?.click()} />
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
                {item}
              </button>
            ))}
          </div>
          {focusedEntry && (
            focusedEntry.targetKind === 'provider'
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
              onClick={() => useAppStore.getState().setInspectorFocus({ kind: entry.targetKind, id: entry.targetId })}
              style={eventButtonStyle(entry.targetId === inspectorFocus?.id)}
            >
              <span style={{ color: 'var(--text-muted)' }}>{entry.targetKind}</span>
              <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--accent-primary)' }}>{entry.targetId.slice(0, 12)}</span>
              {entry.targetKind === 'provider' && (
                <span style={{ color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {providerRowLabel(entry)}
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
      <SmallButton icon={<Database size={12} />} label="Show Cache" onClick={() => useAppStore.getState().setInspectorFocus({ kind: 'cache', id: entries[entries.length - 1]?.targetId || '' })} />
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
  workflowSignals: number
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
  const workflowSignals = progress.filter((entry) => entry.phase === 'workflow').length
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
    workflowSignals,
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
    summary.subagentSignals > 0 ? `${summary.subagentSignals} agent spans` : '',
    summary.workflowSignals > 0 ? `${summary.workflowSignals} workflow spans` : '',
    summary.cacheSpanSignals > 0 ? `${summary.cacheSpanSignals} cache spans` : '',
  ].filter(Boolean)
  return parts.length > 0 ? parts.join(' · ') : 'No coordination spans'
}

const replayOutcomeLabel = (summary: RunReplaySummary): string => {
  if (summary.outcome === 'empty') return 'No replay events'
  if (summary.outcome === 'running') return `${summary.running} still open`
  if (summary.outcome === 'needs_attention') return `${summary.failedOrBlocked} need attention`
  return 'Replay is complete'
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
  output_items: Array.isArray(payload.output_items) ? payload.output_items as ProviderRawMetadata['output_items'] : undefined,
  provider_timeline: Array.isArray(payload.provider_timeline) ? payload.provider_timeline as ProviderRawMetadata['provider_timeline'] : undefined,
  request_summary: typeof payload.request_summary === 'object' && payload.request_summary !== null ? payload.request_summary as ProviderRawMetadata['request_summary'] : undefined,
  stateful_continuation: typeof payload.stateful_continuation === 'object' && payload.stateful_continuation !== null ? payload.stateful_continuation as ProviderRawMetadata['stateful_continuation'] : undefined,
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

const ProviderTraceDetails = ({ entry, previous, traceExportEnabled }: { entry: InspectorEntry; previous?: InspectorEntry; traceExportEnabled: boolean }) => {
  const raw = providerRawFromPayload(entry.payload)
  const usage = providerUsageSummary(raw)
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
        <InfoRow label="Mode" value={providerContinuationLabel(raw)} mono />
        {summary.previous_response_id_present && <InfoRow label="Continuation" value={providerContinuationDetail(raw)} mono />}
        <InfoRow label="Usage" value={`${formatNumber(usage.input)} in / ${formatNumber(usage.output)} out / ${formatNumber(usage.reasoning)} reasoning`} mono />
        <InfoRow label="Cache" value={`${hit == null ? 'n/a' : `${hit}%`} hit · ${formatNumber(usage.cacheRead)} read · ${formatNumber(usage.cacheWrite)} write`} tone={hit ? 'accent' : 'muted'} />
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
            <SmallButton icon={<Copy size={12} />} label="Copy Trace" onClick={() => void navigator.clipboard?.writeText(exportJson())} />
            <SmallButton icon={<Copy size={12} />} label="Request JSON" onClick={() => void navigator.clipboard?.writeText(safeRequestJson())} />
            <SmallButton icon={<Copy size={12} />} label="cURL Skeleton" onClick={() => void navigator.clipboard?.writeText(curlSkeleton())} />
            <SmallButton icon={<Download size={12} />} label="Download" onClick={() => downloadText(`minicode-provider-trace-${safeFilePart(entry.targetId)}.json`, exportJson(), 'application/json')} />
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
      <pre style={jsonBlockStyle}>{JSON.stringify(entry.payload, null, 2)}</pre>
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
    previous_response_id_present: 'PrevID',
    previous_response_id_hash: 'PrevHash',
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
