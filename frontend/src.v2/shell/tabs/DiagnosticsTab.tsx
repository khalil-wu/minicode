/**
 * Diagnostics tab — backend health, LLM info, MCP status, agent capabilities.
 */
import { RefreshCw, Wrench } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  apiBase,
  authHeaders,
  errorMessageFromResponseText,
  fetchWithTimeout,
} from '../../protocol/api'
import { isDesktop } from '../../desktop/runtime'
import { sendClientCommand } from '../../protocol/ws-outbox'
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

  const refresh = useCallback(async () => {
    setLoading(true)
    sendClientCommand(
      { type: 'runtime.capabilities.inspect', source: 'diagnostics' },
      { silent: true },
    )
    try {
      const res = await fetchWithTimeout(
        `${apiBase()}/api/doctor`,
        { cache: 'no-store', headers: authHeaders() },
        { timeoutMessage: '运行诊断超时，请重试。' },
      )
      if (!res.ok) {
        const text = await res.text().catch(() => '')
        throw new Error(errorMessageFromResponseText(text, res.statusText || `HTTP ${res.status}`))
      }
      const payload = await withCapabilityFallback(await res.json() as DoctorPayload)
      setDoctor(payload)
    } catch (error) {
      setDoctor({ error: error instanceof Error ? error.message : String(error || '未知错误') })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  useEffect(() => {
    if (lastMcpSnapshot.current === null) {
      lastMcpSnapshot.current = mcpSnapshot
      return
    }
    if (lastMcpSnapshot.current === mcpSnapshot) return
    lastMcpSnapshot.current = mcpSnapshot
    void refresh()
  }, [mcpSnapshot, refresh])

  return (
    <div style={{ display: 'grid', gap: 10 }}>
      <PanelHeader title="运行诊断" meta={loading ? '检查中' : doctor?.error ? '失败' : doctor ? '正常' : '等待'} action={<SmallButton icon={<RefreshCw size={14} />} label="刷新" onClick={() => void refresh()} />} />

      {doctor?.error && <div style={errorStyle}>{doctor.error}</div>}

      <InfoCard>
        <InfoRow label="后端" value={doctor?.backend?.status === 'ok' ? '正常' : String(doctor?.backend?.status ?? '未知')} tone={doctor?.backend?.status === 'ok' ? 'accent' : 'warning'} />
        <InfoRow label="会话" value={String(doctor?.backend?.active_sessions ?? local.activeSessions)} />
        <InfoRow label="服务商" value={String(doctor?.llm?.provider ?? '未知')} />
        <InfoRow label="模型" value={String(doctor?.llm?.active_model ?? doctor?.llm?.current_model ?? local.model)} mono />
      </InfoCard>

      <InfoCard>
        <InfoRow label="工作区" value={workspaceDisplayName(String((doctor?.workspace?.root ?? local.workspace) || ''), '本机')} mono />
        <InfoRow label="分支" value={branchDisplayName(String(doctor?.git?.branch ?? local.branch ?? '')) || '--'} />
        <InfoRow label="预览" value={String(doctor?.preview?.url ?? local.preview ?? '--')} mono />
        <InfoRow label="终端" value={`${local.terminals} 个会话`} />
      </InfoCard>

      <InfoCard>
        <InfoRow label="MCP" value={`${Array.isArray(doctor?.mcp) ? doctor?.mcp.length : local.mcpServers} 个服务`} tone={local.mcpErrors ? 'warning' : 'muted'} />
        <InfoRow label="MCP 错误" value={String(local.mcpErrors)} tone={local.mcpErrors ? 'warning' : 'muted'} />
        <InfoRow label="运行环境" value={isDesktop() ? '桌面端' : '网页兼容模式'} />
      </InfoCard>

      <SectionLabel label="沙箱能力" />
      <SandboxCapabilityCard status={effectiveCapabilities?.permission?.sandbox_status} />

      <SectionLabel label="智能体能力" />
      <InfoCard>
        <InfoRow label="工具" value={formatAgentToolCounts(capabilities)} tone={capabilities ? 'accent' : 'muted'} />
        <InfoRow label="MCP resources" value={capabilityFlagLabel(capabilities?.mcp_resource_bridge)} tone={capabilityFlagTone(capabilities?.mcp_resource_bridge)} />
        <InfoRow label="Deferred" value={formatDeferredCapability(capabilities)} tone={capabilityFlagTone(capabilities?.deferred_bridge)} />
        <InfoRow label="技能" value={formatSkillCapability(capabilities)} tone={capabilityFlagTone(capabilities?.skill_catalog)} />
        <InfoRow label="MCP proxies" value={formatMcpProxyCount(capabilities)} tone={capabilities ? 'muted' : 'warning'} />
      </InfoCard>

      <SectionLabel label="能力清单" />
      <InfoCard>
        <InfoRow label="来源" value={formatCapabilitySource(capabilitySource)} tone={capabilitySourceTone(capabilitySource)} />
        <InfoRow label="Exposure" value={formatExposureBreakdown(capabilities)} tone={capabilities ? 'muted' : 'warning'} />
        <InfoRow label="命令" value={formatInventoryCount(effectiveCapabilities?.commands, capabilities?.commands, 'command', 'commands')} />
        <InfoRow label="Tool sample" value={formatCapabilityPreview(capabilityToolNames(effectiveCapabilities?.tools))} mono />
        <InfoRow label="Command" value={formatCapabilityPreview(capabilityItemNames(effectiveCapabilities?.commands))} mono />
        <InfoRow label="Skill sample" value={formatCapabilityPreview(capabilityItemNames(effectiveCapabilities?.skills))} mono />
      </InfoCard>

      <ToolExposureCard toolViews={effectiveCapabilities?.tool_views} />
    </div>
  )
}

const SandboxCapabilityCard = ({ status }: { status?: NonNullable<NonNullable<DoctorPayload['capabilities']>['permission']>['sandbox_status'] }) => {
  const probe = String(status?.probe_status ?? 'unknown')
  const backend = String(status?.backend ?? 'unknown')
  const available = status?.backend_available
  const isolated = (value: boolean | null | undefined) => value === true ? '已隔离' : value === false ? '未隔离' : '未知'
  const tone: InfoTone = status?.fail_closed === true || available === false ? 'warning' : available === true ? 'accent' : 'muted'
  return (
    <InfoCard>
      <InfoRow label="探测" value={probe === 'ready' ? '已完成' : probe === 'pending' ? '等待探测' : probe} tone={tone} />
      <InfoRow label="执行后端" value={backend} mono />
      <InfoRow label="文件系统" value={isolated(status?.filesystem_isolated)} tone={status?.filesystem_isolated === true ? 'accent' : 'warning'} />
      <InfoRow label="子进程网络" value={isolated(status?.network_isolated)} tone={status?.network_isolated === true ? 'accent' : 'warning'} />
      <InfoRow label="拒绝读取" value={isolated(status?.deny_read_isolated)} tone={status?.deny_read_isolated === true ? 'accent' : 'warning'} />
      <InfoRow label="后端缺失时" value={sandboxUnavailableActionLabel(status?.unavailable_action)} tone={status?.unavailable_action === 'run_unsandboxed' ? 'warning' : 'muted'} />
      <InfoRow label="失败策略" value={status?.fail_closed ? '失败关闭' : '不适用'} tone={status?.fail_closed ? 'warning' : 'muted'} />
      {status?.reason && <InfoRow label="原因" value={status.reason} />}
    </InfoCard>
  )
}

const sandboxUnavailableActionLabel = (value: string | undefined): string => {
  switch (value) {
    case 'run_unsandboxed': return '按权限策略直接运行（无 OS 隔离）'
    case 'reject_command': return '拒绝命令'
    case 'reject_turn': return '在回合开始前失败'
    case 'enforce_policy': return '强制执行沙箱策略'
    case 'external_backend': return '由外部沙箱负责'
    case 'await_probe': return '等待探测'
    case 'none': return '不适用'
    default: return value ? value : '未知'
  }
}

// ── Helpers ────────────────────────────────────────────────────

const withCapabilityFallback = async (payload: DoctorPayload): Promise<DoctorPayload> => {
  const fallbackSource = capabilityHasDetails(payload.capabilities) ? 'doctor' : 'unknown'
  if (capabilityHasInventory(payload.capabilities)) {
    return { ...payload, capabilities: withDerivedCapabilitySummary(payload.capabilities), capabilitySource: 'doctor' }
  }
  try {
    const res = await fetchWithTimeout(
      `${apiBase()}/api/status`,
      { cache: 'no-store', headers: authHeaders() },
      { timeoutMessage: '能力清单加载超时。' },
    )
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
      <SectionLabel label="工具范围" />
      <InfoCard>
        <InfoRow label="直接可用" value={formatCapabilityPreview(exposure.direct)} tone={exposure.direct.length ? 'accent' : 'muted'} mono />
        <InfoRow label="按需加载" value={formatCapabilityPreview(exposure.deferred)} tone={exposure.deferred.length ? 'muted' : 'default'} mono />
        <InfoRow label="未开放" value={formatCapabilityPreview(exposure.hidden)} tone={exposure.hidden.length ? 'warning' : 'muted'} mono />
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
