/**
 * Shared components and styles for SidebarRight tabs.
 */
import { ChevronDown, ChevronRight } from 'lucide-react'
import { Children, useState } from 'react'
import type { ActivityBrowserItem } from './activitySidebarState'
import { StatusIcon, type StatusIconStatus } from '../components/icons'

// ── Status Mark ──────────────────────────────────────────────────

export const statusColor = (status: string): string => {
  if (status === 'running' || status === 'in_progress') return 'var(--state-info)'
  if (status === 'done' || status === 'success' || status === 'completed') return 'var(--state-success)'
  if (status === 'error' || status === 'failed') return 'var(--state-danger)'
  if (status === 'blocked') return 'var(--state-warning)'
  return 'var(--text-muted)'
}

export const statusMarkLabel = (status: string): string => {
  if (status === 'running' || status === 'in_progress') return '运行中'
  if (status === 'done' || status === 'success' || status === 'completed') return '已完成'
  if (status === 'error' || status === 'failed') return '失败'
  if (status === 'blocked') return '需要处理'
  return '等待中'
}

export const StatusMark = ({ status, animated = true }: { status: string; animated?: boolean }) => {
  const label = statusMarkLabel(status)
  const iconStatus: StatusIconStatus = status === 'running' || status === 'in_progress'
    ? 'running'
    : status === 'completed' || status === 'done' || status === 'success'
      ? 'success'
      : status === 'failed' || status === 'error'
        ? 'failed'
        : status === 'blocked'
          ? 'blocked'
          : 'pending'
  return (
    <span role="img" aria-label={label} style={{ display: 'inline-flex', flexShrink: 0 }}>
      <StatusIcon status={iconStatus} size={14} spinningClassName={animated ? undefined : 'shrink-0'} />
    </span>
  )
}

// ── Panel Header ─────────────────────────────────────────────────

export const PanelHeader = ({ title, meta, action }: { title: string; meta?: string; action?: React.ReactNode }) => (
  <div style={panelHeaderStyle}>
    <span style={{ fontWeight: "var(--fw-bold)", color: 'var(--text-primary)', flex: 1 }}>{title}</span>
    {meta && <span style={{ fontSize: 'var(--text-xs)', color: 'var(--text-muted)', fontFamily: 'var(--font-ui)' }}>{meta}</span>}
    {action}
  </div>
)

// ── Section Label ────────────────────────────────────────────────

export const SectionLabel = ({ label }: { label: string }) => (
  <div style={sectionLabelStyle}>
    {label}
  </div>
)

// ── Info Card ────────────────────────────────────────────────────

export const InfoCard = ({ children }: { children: React.ReactNode }) => (
  <div style={infoCardStyle}>
    {children}
  </div>
)

export type InfoTone = 'default' | 'muted' | 'accent' | 'warning'

export const InfoRow = ({ label, value, mono, tone = 'default', title }: { label: string; value: string; mono?: boolean; tone?: InfoTone; title?: string }) => {
  const color = tone === 'accent' ? 'var(--accent-primary)' : tone === 'warning' ? 'var(--state-warning)' : tone === 'muted' ? 'var(--text-muted)' : 'var(--text-secondary)'
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '72px minmax(0, 1fr)', gap: 8, alignItems: 'center', minHeight: 18, fontSize: 'var(--text-xs)' }}>
      <span style={{ color: 'var(--text-muted)' }}>{label}</span>
      <span title={title ?? value} style={{ color, fontFamily: mono ? 'var(--font-mono)' : undefined, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{value}</span>
    </div>
  )
}

// ── Small Button ─────────────────────────────────────────────────

export const SmallButton = ({ icon, label, onClick, disabled }: { icon: React.ReactNode; label: string; onClick: () => void; disabled?: boolean }) => (
  <button type="button" className="mc-row-hover" disabled={disabled} onClick={onClick} style={{ display: 'inline-flex', alignItems: 'center', gap: 5, padding: '4px 7px', background: 'transparent', color: disabled ? 'var(--text-muted)' : 'var(--text-secondary)', border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-sm, 4px)', cursor: disabled ? 'not-allowed' : 'pointer', fontSize: 'var(--text-xs)', whiteSpace: 'nowrap' }}>
    {icon}
    {label}
  </button>
)

// ── Empty Line ───────────────────────────────────────────────────

export const EmptyLine = ({ children }: { children: React.ReactNode }) => (
  <span style={{ color: 'var(--text-muted)', fontSize: 'var(--text-sm)', lineHeight: 1.5 }}>{children}</span>
)

// ── Scrollable Panel ─────────────────────────────────────────────

export const ScrollablePanel = ({ children }: { children: React.ReactNode }) => (
  <div
    style={{
      flex: '1 1 auto',
      minHeight: 0,
      minWidth: 0,
      overflowX: 'hidden',
      overflowY: 'auto',
      overscrollBehavior: 'contain',
      padding: '7px 12px 12px',
      scrollbarGutter: 'stable',
      background: 'var(--surface-base)',
    }}
  >
    {children}
  </div>
)

// ── Activity Section ─────────────────────────────────────────────

export const ActivitySection = ({
  title,
  children,
  initialExpanded = false,
  previewCount = 3,
}: {
  title: string
  children: React.ReactNode
  initialExpanded?: boolean
  previewCount?: number
}) => {
  const childArray = Children.toArray(children)
  const childCount = childArray.length
  const canExpand = childCount > previewCount
  const [expanded, setExpanded] = useState(initialExpanded || !canExpand)
  if (childCount === 0) return null
  const visibleChildren = expanded || !canExpand
    ? childArray
    : childArray.slice(0, previewCount)

  return (
    <section className="activity-sidebar-section" style={activitySectionStyle} aria-label={title}>
      <button
        type="button"
        className="activity-sidebar-section-header"
        style={activitySectionHeaderStyle(canExpand)}
        aria-expanded={canExpand ? expanded : undefined}
        disabled={!canExpand}
        onClick={() => { if (canExpand) setExpanded((value) => !value) }}
      >
        <span className="activity-sidebar-section-caret" style={activitySectionCaretStyle}>
          {canExpand ? (expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />) : <span />}
        </span>
        <span style={activitySectionTitleStyle}>{title}</span>
        <span style={activitySectionCountStyle}>{childCount}</span>
      </button>
      <div style={activitySectionBodyStyle}>{visibleChildren}</div>
      {!expanded && canExpand && (
        <button
          type="button"
          className="activity-sidebar-section-more"
          style={activitySectionMoreStyle}
          onClick={() => setExpanded(true)}
        >
          {`再显示 ${childCount - previewCount} 项`}
        </button>
      )}
    </section>
  )
}

// ── Activity Button Row ──────────────────────────────────────────

export const ActivityButtonRow = ({ children, onClick, title }: { children: React.ReactNode; onClick?: () => void; title?: string }) => {
  if (!onClick) {
    return (
      <div style={activityButtonRowStyle(false)} title={title}>
        {children}
      </div>
    )
  }
  return (
    <button
      type="button"
      className="mc-row-hover"
      onClick={onClick}
      style={activityButtonRowStyle(true)}
      title={title}
    >
      {children}
    </button>
  )
}

// ── Activity Icon ────────────────────────────────────────────────

export const ActivityIcon = ({ children }: { children: React.ReactNode }) => (
  <span style={activityIconStyle}>{children}</span>
)

// ── Row Card ─────────────────────────────────────────────────────

export const RowCard = ({ active, children }: { active: boolean; children: React.ReactNode }) => (
  <div style={rowCardStyle(active)}>
    {children}
  </div>
)

// ── Styles ───────────────────────────────────────────────────────

const panelHeaderStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  marginBottom: 4,
  minHeight: 24,
  paddingBottom: 5,
  borderBottom: '1px solid var(--border-subtle)',
}

const sectionLabelStyle: React.CSSProperties = {
  fontSize: 'var(--text-xs)',
  color: 'var(--text-muted)',
  textTransform: 'uppercase',
  fontWeight: "var(--fw-bold)",
  paddingTop: 1,
  letterSpacing: 0,
}

const infoCardStyle: React.CSSProperties = {
  display: 'grid',
  gap: 3,
  padding: '7px 0 0',
  background: 'transparent',
  border: 0,
  borderTop: '1px solid var(--border-subtle)',
  borderRadius: 0,
}

const activitySectionStyle: React.CSSProperties = {
  display: 'grid',
  gap: 2,
}

const activitySectionBodyStyle: React.CSSProperties = {
  display: 'grid',
  gap: 1,
}

const activitySectionHeaderStyle = (canExpand: boolean): React.CSSProperties => ({
  display: 'grid',
  gridTemplateColumns: '14px minmax(0, 1fr) auto',
  alignItems: 'center',
  gap: 5,
  minHeight: 24,
  padding: '2px 0',
  border: 0,
  borderTop: '1px solid var(--border-subtle)',
  borderRadius: 0,
  background: 'transparent',
  color: 'var(--text-muted)',
  cursor: canExpand ? 'pointer' : 'default',
  textAlign: 'left',
})

const activitySectionCaretStyle: React.CSSProperties = {
  width: 14,
  height: 18,
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  color: 'var(--text-muted)',
}

const activitySectionTitleStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: 'hidden',
  color: 'var(--text-secondary)',
  fontSize: 'var(--text-xs)',
  fontWeight: "var(--fw-bold)",
  letterSpacing: 0,
  textOverflow: 'ellipsis',
  textTransform: 'none',
  whiteSpace: 'nowrap',
}

const activitySectionCountStyle: React.CSSProperties = {
  minWidth: 18,
  height: 17,
  padding: '0 5px',
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  border: '1px solid var(--border-subtle)',
  borderRadius: 'var(--radius-sm, 5px)',
  color: 'var(--text-muted)',
  fontFamily: 'var(--font-ui)',
  fontSize: "var(--text-3xs)",
}

const activitySectionMoreStyle: React.CSSProperties = {
  justifySelf: 'start',
  minHeight: 22,
  padding: '1px 0 2px 22px',
  border: 0,
  background: 'transparent',
  color: 'var(--text-muted)',
  cursor: 'pointer',
  fontFamily: 'var(--font-ui)',
  fontSize: "var(--text-3xs)",
}

const activityButtonRowStyle = (interactive: boolean): React.CSSProperties => ({
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  minWidth: 0,
  minHeight: 28,
  width: '100%',
  padding: '4px 0',
  border: 0,
  borderTop: '1px solid var(--border-subtle)',
  borderRadius: 0,
  background: 'transparent',
  color: 'var(--text-primary)',
  cursor: interactive ? 'pointer' : 'default',
  textAlign: 'left',
  textDecoration: 'none',
})

const activityIconStyle: React.CSSProperties = {
  width: 18,
  height: 18,
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  flexShrink: 0,
  border: '1px solid color-mix(in oklch, var(--border-subtle) 58%, transparent)',
  borderRadius: 'var(--radius-sm, 5px)',
  color: 'var(--text-muted)',
  background: 'transparent',
}

const rowCardStyle = (active: boolean): React.CSSProperties => ({
  display: 'flex',
  alignItems: 'flex-start',
  gap: 8,
  padding: '6px 0',
  borderRadius: 0,
  borderTop: '1px solid var(--border-subtle)',
  background: active ? 'color-mix(in oklch, var(--accent-orange) 7%, transparent)' : 'transparent',
  boxShadow: active ? 'inset 2px 0 0 var(--accent-orange)' : 'none',
})
