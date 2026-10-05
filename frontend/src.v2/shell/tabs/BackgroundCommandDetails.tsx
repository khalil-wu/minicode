import { ArrowLeft, RefreshCw, Square } from 'lucide-react'
import { useEffect, useState } from 'react'
import { fetchBackgroundCommand, stopBackgroundCommand, type BackgroundCommandDetail } from '../../protocol/conversation-resources'
import { useAppStore } from '../../stores'
import { InfoCard, InfoRow, PanelHeader } from '../SidebarShared'

export const BackgroundCommandDetails = ({ conversationId, commandId, onBack }: { conversationId: string; commandId: string; onBack: () => void }) => {
  const isConnected = useAppStore((state) => state.isConnected)
  const [detail, setDetail] = useState<BackgroundCommandDetail | null>(null)
  const [error, setError] = useState('')
  const [stopping, setStopping] = useState(false)
  const [refreshVersion, setRefreshVersion] = useState(0)
  useEffect(() => {
    if (!isConnected) return
    const controller = new AbortController()
    let cursor = 0
    let timer: number | undefined
    const read = async () => {
      try {
        const next = await fetchBackgroundCommand(conversationId, commandId, cursor, controller.signal)
        cursor = next.next_cursor
        setDetail((current) => ({ ...next, output: `${current?.output ?? ''}${next.output}` }))
        setError('')
        if (next.has_more || next.managed && (next.status === 'running' || next.status === 'stalled' || next.cleanup_pending)) timer = window.setTimeout(() => void read(), next.has_more ? 0 : 1500)
      } catch (failure) {
        if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : String(failure))
      }
    }
    setDetail(null); setError('')
    void read()
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [conversationId, commandId, isConnected, refreshVersion])
  const stop = async () => {
    setStopping(true); setError('')
    try {
      const stopped = await stopBackgroundCommand(conversationId, commandId)
      setDetail((current) => ({ ...stopped, output: current?.output ?? stopped.output }))
      setRefreshVersion((value) => value + 1)
    } catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)) }
    finally { setStopping(false) }
  }
  const status = detail?.cleanup_pending ? '清理未完成' : detail ? ({ running: '运行中', stalled: '等待输入', completed: '已完成', failed: '失败', cancelled: '已停止', unknown: '已退出 · 结果未知' })[detail.status] : '正在读取'
  const seconds = detail ? Math.max(0, Math.round(((detail.completed_at ?? Date.now() / 1000) - detail.started_at))) : 0
  const canStop = detail?.managed && (detail.status === 'running' || detail.status === 'stalled' || detail.cleanup_pending)
  return <section style={{ display: 'grid', gap: 12 }} aria-label="后台进程详情">
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
      <button type="button" className="btn-ghost" onClick={onBack} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}><ArrowLeft size={14} />返回上下文</button>
      <button type="button" className="btn-ghost mc-icon-button" aria-label="刷新后台进程" disabled={!isConnected} onClick={() => setRefreshVersion((value) => value + 1)}><RefreshCw size={14} /></button>
    </div>
    <PanelHeader title="后台进程" meta={status} />
    {detail && <>
      <pre style={{ margin: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 'var(--mc-font-secondary)', color: 'var(--text-primary)' }}>{detail.command}</pre>
      {detail.description && <p style={{ margin: 0, fontSize: 'var(--mc-font-secondary)', color: 'var(--text-muted)' }}>{detail.description}</p>}
      {!detail.managed && <p style={{ margin: 0, fontSize: 'var(--mc-font-secondary)', color: 'var(--text-muted)' }}>这是上次运行保留的进程记录；原运行期已结束，无法确认退出码。</p>}
      <InfoCard><InfoRow label="目录" value={detail.cwd || '未指定'} mono /><InfoRow label="状态" value={status} /><InfoRow label="开始" value={new Date(detail.started_at * 1000).toLocaleString()} /><InfoRow label="运行时间" value={!detail.managed && detail.completed_at == null ? '无法确认' : `${seconds} 秒`} /><InfoRow label="退出码" value={detail.exit_code == null ? detail.status === 'running' || detail.status === 'stalled' ? '尚未退出' : '未知' : String(detail.exit_code)} /></InfoCard>
      {canStop && <button type="button" className="btn-ghost" disabled={stopping || !isConnected} onClick={() => void stop()} style={{ display: 'inline-flex', justifySelf: 'start', alignItems: 'center', gap: 6 }}><Square size={13} />{stopping ? '正在停止…' : detail.cleanup_pending ? '继续停止并清理' : '停止进程'}</button>}
      {detail.cleanup_pending && <p role="status" style={{ margin: 0, fontSize: 'var(--mc-font-secondary)', color: 'var(--state-warning)' }}>进程清理尚未完成。{detail.cleanup_reason}{Object.keys(detail.cleanup_error).length > 0 ? ` · ${JSON.stringify(detail.cleanup_error)}` : ''}</p>}
      <pre aria-label="后台进程日志" style={{ margin: 0, maxHeight: '50vh', overflow: 'auto', padding: 12, border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-lg)', background: 'var(--surface-soft)', color: 'var(--text-secondary)', fontSize: 'var(--mc-font-caption)', lineHeight: 1.6, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{detail.output || '暂无输出'}</pre>
    </>}
    {!isConnected && <p role="status">连接已中断，恢复后将读取进程实际状态。</p>}
    {error && <p role="alert" style={{ color: 'var(--state-danger)', fontSize: 'var(--mc-font-secondary)' }}>{error}</p>}
    {!detail && !error && isConnected && <p role="status">正在读取后台进程…</p>}
  </section>
}
