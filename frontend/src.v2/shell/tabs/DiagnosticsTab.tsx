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
import { selectActiveConversationPreview } from '../../lib/preview-projection'
import { branchDisplayName, workspaceDisplayName } from '../../lib/workspace-display'
import { workspaceRootsEqual } from '../../lib/workspace-path'
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
import { openSettings } from '../../lib/settings-navigation'
import type { SettingsTab } from '../../stores/types'

type InfoTone = 'default' | 'muted' | 'accent' | 'warning'

export const DiagnosticsTab = () => {
  const [doctor, setDoctor] = useState<DoctorPayload | null>(null)
  const [loading, setLoading] = useState(false)
  const local = useLocalDiagnostics()
  const mcpSnapshot = useAppStore((s) => mcpDiagnosticsSnapshot(s.mcpServers))
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities)
  const mcpServers = useAppStore((s) => s.mcpServers)
  const lastMcpSnapshot = useRef<string | null>(null)
  const refreshRequestRef = useRef(0)
  const doctorMatchesWorkspace = !doctor?.workspace?.root || workspaceRootsEqual(doctor.workspace.root, local.workspace)
  const scopedDoctor = doctorMatchesWorkspace ? doctor : doctor && { ...doctor, workspace: undefined, mcp: [] }
  const effectiveCapabilities = useMemo(
    () => mergeCapabilities(runtimeCapabilities ?? undefined, doctorMatchesWorkspace ? doctor?.capabilities : undefined),
    [runtimeCapabilities, doctor?.capabilities, doctorMatchesWorkspace],
  )
  const capabilities = effectiveCapabilities?.summary
  const capabilitySource: CapabilitySource | undefined = capabilityHasDetails(runtimeCapabilities ?? undefined)
    ? 'runtime'
    : doctor?.capabilitySource
  const issues = diagnosisIssues(scopedDoctor, mcpServers, effectiveCapabilities?.permission?.sandbox_status)
  const sandboxStatus = effectiveCapabilities?.permission?.sandbox_status
  const remoteMcp = (scopedDoctor?.mcp || []) as { status?: string; phase?: string }[]
  const mcpErrorCount = issues.filter((issue) => issue.tab === 'connectors').length
  const unknown = !doctor?.backend?.status || !doctor.llm || !capabilities || !sandboxStatus?.probe_status
    || ['unknown', 'pending'].includes(sandboxStatus.probe_status)
    || mcpServers.some((server) => !['connected', 'disabled', 'error', 'failed', 'auth_required', 'expired'].includes(server.phase || server.status))
    || remoteMcp.some((server) => !['connected', 'disabled', 'error', 'failed', 'auth_required', 'expired'].includes(server.phase || server.status || ''))
  const stateLabel = loading ? '检查中' : issues.length ? `${issues.length} 项需要处理` : unknown ? '状态未知' : '正常'

  const refresh = useCallback(async () => {
    const requestId = ++refreshRequestRef.current
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
      if (requestId === refreshRequestRef.current) setDoctor(payload)
    } catch (error) {
      if (requestId === refreshRequestRef.current) setDoctor({ error: error instanceof Error ? error.message : String(error || '未知错误') })
    } finally {
      if (requestId === refreshRequestRef.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
    return () => { refreshRequestRef.current += 1 }
  }, [refresh])

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
      <PanelHeader title="运行诊断" meta={stateLabel} action={<SmallButton icon={<RefreshCw size={14} />} label="刷新" onClick={() => void refresh()} />} />

      {issues.length > 0 ? <section aria-label="需要处理的组件" style={{ display: 'grid', gap: 8 }}>
        {issues.map((issue) => <div key={issue.title} style={errorStyle}>
          <strong>{issue.title}</strong><p style={{ margin: '5px 0', color: 'var(--text-secondary)' }}>{issue.description}</p>
          <SmallButton icon={<Wrench size={14} />} label={issue.tab === 'provider' ? '打开模型设置' : issue.tab === 'connectors' ? '打开 MCP 设置' : '打开环境设置'} onClick={() => openSettings(issue.tab)} />
          {issue.detail && <details><summary>详细信息</summary><code style={{ overflowWrap: 'anywhere' }}>{issue.detail}</code></details>}
        </div>)}
      </section> : <p style={{ margin: 0, color: 'var(--text-muted)', fontSize: 'var(--mc-font-secondary)' }}>{stateLabel === '正常' ? '当前已检测组件运行正常。' : loading ? '正在检查连接和运行能力。' : '部分组件尚未返回检测结果，刷新后可查看。'}</p>}

      <InfoCard>
        <InfoRow label="后端" value={doctor?.backend?.status === 'ok' ? '正常' : String(doctor?.backend?.status ?? '未知')} tone={doctor?.backend?.status === 'ok' ? 'accent' : 'warning'} />
        <InfoRow label="会话" value={String(doctor?.backend?.active_sessions ?? local.activeSessions)} />
        <InfoRow label="服务商" value={String(doctor?.llm?.provider ?? '未知')} />
        <InfoRow label="模型" value={String(doctor?.llm?.active_model ?? doctor?.llm?.current_model ?? local.model)} mono />
      </InfoCard>

      <InfoCard>
        <InfoRow label="工作区" value={workspaceDisplayName(local.workspace || '', '本机')} mono />
        <InfoRow label="分支" value={branchDisplayName(local.branch || '') || '--'} />
        <InfoRow label="预览" value={String(local.preview || '--')} mono />
        <InfoRow label="终端" value={`${local.terminals} 个会话`} />
      </InfoCard>

      <InfoCard>
        <InfoRow label="MCP" value={`${local.mcpServers} 个服务`} tone={mcpErrorCount ? 'warning' : 'muted'} />
        <InfoRow label="MCP 错误" value={String(mcpErrorCount)} tone={mcpErrorCount ? 'warning' : 'muted'} />
        <InfoRow label="运行环境" value={isDesktop() ? '桌面端' : '网页兼容模式'} />
      </InfoCard>

      <details><summary style={{ color: 'var(--text-muted)', fontSize: 'var(--mc-font-secondary)', cursor: 'pointer' }}>展开运行能力和原始诊断</summary>
      <div style={{ display: 'grid', gap: 10, paddingTop: 10 }}>
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
      </div></details>
    </div>
  )
}

type DiagnosticIssue = { title: string; description: string; tab: SettingsTab; detail?: string }
const diagnosisIssues = (doctor: DoctorPayload | null, servers: { name: string; status: string; phase?: string; lastError?: string }[], sandbox: NonNullable<NonNullable<DoctorPayload['capabilities']>['permission']>['sandbox_status']): DiagnosticIssue[] => {
  const issues: DiagnosticIssue[] = []
  if (doctor?.error) issues.push({ title: '运行诊断未完成', description: '暂时无法确认后端状态。检查连接后刷新。', tab: 'advanced', detail: doctor.error })
  else if (doctor?.backend?.status && doctor.backend.status !== 'ok') issues.push({ title: '后端连接需要处理', description: '后端未报告正常状态，任务执行可能受到影响。', tab: 'advanced', detail: String(doctor.backend.status) })
  if (doctor?.llm && !doctor.llm.active_model && !doctor.llm.current_model) issues.push({ title: '尚未选择模型', description: '选择并配置模型后才能运行新任务。', tab: 'provider' })
  if (doctor?.workspace?.exists === false || doctor?.workspace?.writable === false) issues.push({ title: '工作区需要处理', description: doctor.workspace.exists === false ? '当前项目目录不存在，请重新打开项目。' : '当前项目不可写，代码修改无法保存。', tab: 'advanced' })
  const remote = (doctor?.mcp || []) as { name?: string; status?: string; phase?: string; error?: string; message?: string }[]
  const byName = new Map(servers.map((server) => [server.name, server]))
  for (const server of remote) byName.set(server.name || '未命名服务', { name: server.name || '未命名服务', status: server.status || '', phase: server.phase, lastError: server.error || server.message })
  for (const server of byName.values()) {
    const phase = server.phase || server.status
    if (!['error', 'failed', 'auth_required', 'expired'].includes(phase)) continue
    issues.push({ title: `MCP · ${server.name}`, description: ['auth_required', 'expired'].includes(phase) ? '登录需要完成或已过期。此服务的工具暂不可用。' : '服务连接失败。检查配置后重新连接。', tab: 'connectors', detail: server.lastError })
  }
  if (sandbox?.backend_available === false || sandbox?.probe_status === 'failed') issues.push({ title: '执行隔离需要处理', description: sandbox.unavailable_action === 'run_unsandboxed' ? '当前执行环境无法提供系统隔离，命令将按现有权限策略运行。' : '当前隔离后端不可用，部分命令可能无法执行。', tab: 'advanced', detail: sandbox.reason })
  return issues
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
        <InfoRow label="通过脚本调用" value={formatCapabilityPreview(exposure.codeMode)} tone={exposure.codeMode.length ? 'muted' : 'default'} mono />
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
  const livePreviewUrl = useAppStore((s) => selectActiveConversationPreview(s).livePreviewUrl)
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
    mcpErrors: mcpServers.filter((s) => ['error', 'failed', 'auth_required', 'expired'].includes(s.phase || s.status)).length,
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
