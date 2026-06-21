/**
 * Inspector tab — combines ContextTab (session/workspace/runtime info)
 * and DetailsTab (inspector entries / recent tool calls).
 */
import { useEffect, useState } from 'react'
import { Copy, FolderOpen, GitBranch, TerminalSquare } from 'lucide-react'
import { isDesktop, revealPath } from '../../desktop/runtime'
import { fetchWorkspaceGitStatus, type WorkspaceGitStatusResponse } from '../../protocol/workspace'
import { useAppStore } from '../../stores'
import { branchDisplayName, workspaceDisplayName } from '../../lib/workspace-display'
import { InfoCard, InfoRow, SectionLabel, SmallButton, statusColor } from '../SidebarShared'

export const InspectorTab = ({ toolCalls }: { toolCalls: { id: string; name: string; status: string }[] }) => (
  <div style={{ display: 'grid', gap: 12 }}>
    <ContextTab />
    <DetailsTab toolCalls={toolCalls} />
  </div>
)

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
    </div>
  )
}

// ── Details Tab ────────────────────────────────────────────────

const DetailsTab = ({ toolCalls }: { toolCalls: { id: string; name: string; status: string }[] }) => {
  const inspectorEntries = useAppStore((s) => s.inspectorEntries)
  const inspectorFocus = useAppStore((s) => s.inspectorFocus)
  const focusedEntry = inspectorFocus ? inspectorEntries.find((e) => e.targetId === inspectorFocus.id) : null

  if (!focusedEntry && inspectorEntries.length === 0 && toolCalls.length === 0) return null

  return (
    <div style={{ display: 'grid', gap: 10 }}>
      {(focusedEntry || inspectorEntries.length > 0) && (
        <>
          <SectionLabel label="Inspector" />
          {focusedEntry && (
            <pre style={jsonBlockStyle}>
              {JSON.stringify(focusedEntry.payload, null, 2)}
            </pre>
          )}
          {inspectorEntries.slice(-8).reverse().map((entry, i) => (
            <button
              key={`${entry.targetId}-${i}`}
              onClick={() => useAppStore.getState().setInspectorFocus({ kind: entry.targetKind, id: entry.targetId })}
              style={eventButtonStyle(entry.targetId === inspectorFocus?.id)}
            >
              <span style={{ color: 'var(--text-muted)' }}>{entry.targetKind}</span>
              <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--accent-primary)' }}>{entry.targetId.slice(0, 12)}</span>
            </button>
          ))}
        </>
      )}
      {toolCalls.length > 0 && (
        <>
          <SectionLabel label="Recent Tools" />
          {toolCalls.map((tc) => (
            <div key={tc.id} style={toolCallRowStyle}>
              <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--accent-primary)' }}>{tc.name}</span>
              <span style={{ marginLeft: 8, color: statusColor(tc.status) }}>{tc.status}</span>
            </div>
          ))}
        </>
      )}
    </div>
  )
}

// ── Styles ─────────────────────────────────────────────────────

const eventButtonStyle = (active: boolean): React.CSSProperties => ({
  display: 'flex',
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

const toolCallRowStyle: React.CSSProperties = {
  padding: '4px 0',
  background: 'transparent',
  borderTop: '1px solid var(--border-subtle)',
  borderRadius: 0,
  fontSize: 'var(--text-xs)',
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
