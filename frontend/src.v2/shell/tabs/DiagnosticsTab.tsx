/**
 * Diagnostics tab — backend health, LLM info, MCP status, agent capabilities.
 */
import { RefreshCw, Wrench } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { apiBase, authHeaders } from '../../protocol/api'
import { isDesktop } from '../../desktop/runtime'
import { getWebSocket } from '../../hooks/useWebSocket'
import { useAppStore } from '../../stores'
import { branchDisplayName, workspaceDisplayName } from '../../lib/workspace-display'
import {
  capabilityFlagLabel,
  capabilityHasDetails,
  capabilityHasInventory,
  capabilityItemNames,
  capabilityToolNames,
  formatAgentToolCounts,
  formatCapabilityPreview,
  formatCapabilitySource,
  formatDeferredCapability,
  formatExposureBreakdown,
  formatInventoryCount,
  formatMcpProxyCount,
  formatSkillCapability,
  mergeCapabilities,
  summarizeToolViews,
  withDerivedCapabilitySummary,
  type AgentCapabilityToolView,
  type CapabilitySource,
  type DoctorPayload,
} from '../../protocol/capabilities'
import { InfoCard, InfoRow, PanelHeader, SectionLabel, SmallButton } from '../SidebarShared'

type InfoTone = 'default' | 'muted' | 'accent' | 'warning'

export const DiagnosticsTab = () => {
  const [doctor, setDoctor] = useState<DoctorPayload | null>(null)
  const [loading, setLoading] = useState(false)
  const local = useLocalDiagnostics()
  const mcpSnapshot = useAppStore((s) => mcpDiagnosticsSnapshot(s.mcpServers))
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities)
  const lastMcpSnapshot = useRef<string | null>(null)
  const effectiveCapabilities = useMemo(
    () => mergeCapabilities(runtimeCapabilities ?? undefined, doctor?.capabilities),
    [runtimeCapabilities, doctor?.capabilities],
  )
  const capabilities = effectiveCapabilities?.summary
  const capabilitySource: CapabilitySource | undefined = capabilityHasDetails(runtimeCapabilities ?? undefined)
    ? 'runtime'
    : doctor?.capabilitySource

  const refresh = useCallback(() => {
    setLoading(true)
    getWebSocket()?.send({ type: 'runtime.capabilities.inspect', source: 'diagnostics' })
    fetch(`${apiBase()}/api/doctor`, { cache: 'no-store', headers: authHeaders() })
      .then((res) => res.ok ? res.json() : Promise.reject(new Error(res.statusText)))
      .then((payload) => withCapabilityFallback(payload as DoctorPayload))
      .then((payload) => setDoctor(payload))
      .catch((error) => setDoctor({ error: String(error) }))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { refresh() }, [refresh])

  useEffect(() => {
    if (lastMcpSnapshot.current === null) {
      lastMcpSnapshot.current = mcpSnapshot
      return
    }
    if (lastMcpSnapshot.current === mcpSnapshot) return
    lastMcpSnapshot.current = mcpSnapshot
    refresh()
  }, [mcpSnapshot, refresh])

  return (
    <div style={{ display: 'grid', gap: 10 }}>
      <PanelHeader title="Diagnostics" meta={loading ? 'checking' : 'ready'} action={<SmallButton icon={<RefreshCw size={12} />} label="Refresh" onClick={refresh} />} />

      {doctor?.error && <div style={errorStyle}>{doctor.error}</div>}

      <InfoCard>
        <InfoRow label="Backend" value={String(doctor?.backend?.status ?? 'unknown')} tone={doctor?.backend?.status === 'ok' ? 'accent' : 'warning'} />
        <InfoRow label="Sessions" value={String(doctor?.backend?.active_sessions ?? local.activeSessions)} />
        <InfoRow label="Provider" value={String(doctor?.llm?.provider ?? 'unknown')} />
        <InfoRow label="Model" value={String(doctor?.llm?.active_model ?? doctor?.llm?.current_model ?? local.model)} mono />
      </InfoCard>

      <InfoCard>
        <InfoRow label="Workspace" value={workspaceDisplayName(String((doctor?.workspace?.root ?? local.workspace) || ''), 'Computer')} mono />
        <InfoRow label="Branch" value={branchDisplayName(String(doctor?.git?.branch ?? local.branch ?? '')) || '--'} />
        <InfoRow label="Preview Pane" value={String(doctor?.preview?.url ?? local.preview ?? '--')} mono />
        <InfoRow label="Terminal" value={`${local.terminals} sessions`} />
      </InfoCard>

      <InfoCard>
        <InfoRow label="MCP" value={`${Array.isArray(doctor?.mcp) ? doctor?.mcp.length : local.mcpServers} servers`} tone={local.mcpErrors ? 'warning' : 'muted'} />
        <InfoRow label="MCP errors" value={String(local.mcpErrors)} tone={local.mcpErrors ? 'warning' : 'muted'} />
        <InfoRow label="Runtime" value={isDesktop() ? 'Electron desktop' : 'Web fallback'} />
      </InfoCard>

      <SectionLabel label="Agent" />
      <InfoCard>
        <InfoRow label="Tools" value={formatAgentToolCounts(capabilities)} tone={capabilities ? 'accent' : 'muted'} />
        <InfoRow label="MCP resources" value={capabilityFlagLabel(capabilities?.mcp_resource_bridge)} tone={capabilityFlagTone(capabilities?.mcp_resource_bridge)} />
        <InfoRow label="Deferred" value={formatDeferredCapability(capabilities)} tone={capabilityFlagTone(capabilities?.deferred_bridge)} />
        <InfoRow label="Skills" value={formatSkillCapability(capabilities)} tone={capabilityFlagTone(capabilities?.skill_bridge)} />
        <InfoRow label="MCP proxies" value={formatMcpProxyCount(capabilities)} tone={capabilities ? 'muted' : 'warning'} />
      </InfoCard>

      <SectionLabel label="Inventory" />
      <InfoCard>
        <InfoRow label="Source" value={formatCapabilitySource(capabilitySource)} tone={capabilitySourceTone(capabilitySource)} />
        <InfoRow label="Exposure" value={formatExposureBreakdown(capabilities)} tone={capabilities ? 'muted' : 'warning'} />
        <InfoRow label="Commands" value={formatInventoryCount(effectiveCapabilities?.commands, capabilities?.commands, 'command', 'commands')} />
        <InfoRow label="Tool sample" value={formatCapabilityPreview(capabilityToolNames(effectiveCapabilities?.tools))} mono />
        <InfoRow label="Command" value={formatCapabilityPreview(capabilityItemNames(effectiveCapabilities?.commands))} mono />
        <InfoRow label="Skill sample" value={formatCapabilityPreview(capabilityItemNames(effectiveCapabilities?.skills))} mono />
      </InfoCard>

      <ToolExposureCard toolViews={effectiveCapabilities?.tool_views} />
    </div>
  )
}

// ── Helpers ────────────────────────────────────────────────────

const withCapabilityFallback = async (payload: DoctorPayload): Promise<DoctorPayload> => {
  const fallbackSource = capabilityHasDetails(payload.capabilities) ? 'doctor' : 'unknown'
  if (capabilityHasInventory(payload.capabilities)) {
    return { ...payload, capabilities: withDerivedCapabilitySummary(payload.capabilities), capabilitySource: 'doctor' }
  }
  try {
    const res = await fetch(`${apiBase()}/api/status`, { cache: 'no-store', headers: authHeaders() })
    if (!res.ok) return { ...payload, capabilitySource: fallbackSource }
    const statusPayload = await res.json() as DoctorPayload
    const statusHasDetails = capabilityHasDetails(statusPayload.capabilities)
    return {
      ...payload,
      capabilities: mergeCapabilities(payload.capabilities, statusPayload.capabilities),
      capabilitySource: statusHasDetails ? 'status' : fallbackSource,
    }
  } catch {
    return { ...payload, capabilitySource: fallbackSource }
  }
}

const mcpDiagnosticsSnapshot = (servers: { name: string; status: string; phase?: string; tools?: number; lastError?: string }[]): string =>
  servers
    .map((server) => [server.name, server.status, server.phase ?? '', server.tools ?? '', server.lastError ?? ''].join(':'))
    .sort()
    .join('|')

const capabilityFlagTone = (ready: boolean | undefined): InfoTone => {
  if (ready === true) return 'accent'
  if (ready === false) return 'warning'
  return 'muted'
}

const capabilitySourceTone = (source: CapabilitySource | undefined): InfoTone =>
  source === 'doctor' ? 'muted' : source === 'runtime' ? 'accent' : 'warning'

const ToolExposureCard = ({ toolViews }: { toolViews: AgentCapabilityToolView[] | undefined }) => {
  const exposure = summarizeToolViews(toolViews)
  if (exposure.total == null) return null
  return (
    <>
      <SectionLabel label="Tool Exposure" />
      <InfoCard>
        <InfoRow label="Direct tools" value={formatCapabilityPreview(exposure.direct)} tone={exposure.direct.length ? 'accent' : 'muted'} mono />
        <InfoRow label="Deferred tools" value={formatCapabilityPreview(exposure.deferred)} tone={exposure.deferred.length ? 'muted' : 'default'} mono />
        <InfoRow label="Hidden tools" value={formatCapabilityPreview(exposure.hidden)} tone={exposure.hidden.length ? 'warning' : 'muted'} mono />
      </InfoCard>
    </>
  )
}

const useLocalDiagnostics = () => {
  const conversations = useAppStore((s) => s.conversations)
  const currentModel = useAppStore((s) => s.currentModel)
  const workingDirectory = useAppStore((s) => s.workingDirectory)
  const workspaceGit = useAppStore((s) => s.workspaceGit)
  const livePreviewUrl = useAppStore((s) => s.livePreviewUrl)
  const terminalSessions = useAppStore((s) => s.terminalSessions)
  const mcpServers = useAppStore((s) => s.mcpServers)
  return useMemo(() => ({
    activeSessions: conversations.length,
    model: currentModel || 'Select model',
    workspace: workingDirectory,
    branch: workspaceGit?.branch,
    preview: livePreviewUrl,
    terminals: terminalSessions.length,
    mcpServers: mcpServers.length,
    mcpErrors: mcpServers.filter((s) => s.status === 'error').length,
  }), [conversations.length, currentModel, workingDirectory, workspaceGit?.branch, livePreviewUrl, terminalSessions.length, mcpServers])
}

// ── Styles ─────────────────────────────────────────────────────

const errorStyle: React.CSSProperties = {
  color: 'var(--state-danger)',
  background: 'var(--state-danger-soft)',
  border: '1px solid var(--state-danger)',
  borderRadius: 'var(--radius-sm, 4px)',
  padding: 8,
  fontSize: 'var(--text-xs)',
}
