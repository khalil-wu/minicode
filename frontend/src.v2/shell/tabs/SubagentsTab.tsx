/**
 * Subagents tab — lists delegated subagents and their status.
 */
import { useAppStore } from '../../stores'
import { EmptyLine, PanelHeader, RowCard, StatusMark } from '../SidebarShared'

export const SubagentsTab = () => {
  const subagents = useAppStore((s) => s.subagents)

  if (subagents.length === 0) {
    return <EmptyLine>No delegated subagents are running.</EmptyLine>
  }

  const running = subagents.filter((sub) => sub.status === 'running').length

  return (
    <div>
      <PanelHeader
        title="Subagents"
        meta={running ? `${running}/${subagents.length} running` : `${subagents.length} recent`}
      />
      <div style={{ display: 'grid', gap: 4 }}>
        {subagents.map((sub) => (
          <RowCard key={sub.id} active={sub.status === 'running'}>
            <StatusMark status={sub.status} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ color: 'var(--text-primary)', fontSize: 'var(--text-xs)', lineHeight: 1.45 }}>
                {sub.role}
              </div>
              <div style={{ color: 'var(--text-muted)', fontSize: 'var(--text-xs)', fontFamily: 'var(--font-mono)', marginTop: 2 }}>
                {sub.id}
              </div>
              {sub.summary && (
                <div style={{ color: 'var(--text-secondary)', fontSize: 'var(--text-xs)', marginTop: 4 }}>
                  {sub.summary}
                </div>
              )}
            </div>
          </RowCard>
        ))}
      </div>
    </div>
  )
}