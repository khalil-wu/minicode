/**
 * Plan tab — displays the active plan and its steps.
 */
import { useAppStore } from '../../stores'
import { EmptyLine, PanelHeader, RowCard, StatusMark } from '../SidebarShared'
import { hasVisiblePlanSteps } from '../../lib/planVisibility'

export const PlanTab = () => {
  const plan = useAppStore((s) => s.plan)

  if (!hasVisiblePlanSteps(plan)) {
    return (
      <div style={{ display: 'grid', gap: 10 }}>
        <PanelHeader title="Plan" />
        <EmptyLine>No proposed plan in this session.</EmptyLine>
      </div>
    )
  }

  const doneCount = plan.steps.filter((s) => s.status === 'done').length

  return (
    <div style={{ display: 'grid', gap: 10 }}>
      <PanelHeader title="Plan" meta={`${doneCount}/${plan.steps.length} ${plan.status}`} />
      <div style={{ display: 'grid', gap: 2 }}>
        {plan.steps.map((s, i) => (
          <RowCard key={s.id} active={i === plan.currentStep && s.status === 'running'}>
            <StatusMark status={s.status} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{
                color: s.status === 'done' ? 'var(--text-muted)' : 'var(--text-primary)',
                textDecoration: s.status === 'done' ? 'line-through' : 'none',
                fontSize: 'var(--text-xs)',
                lineHeight: 1.45,
                fontWeight: i === plan.currentStep ? 600 : 400,
              }}>
                {s.title}
              </div>
              {s.detail && (
                <div style={{ fontSize: 'var(--text-xs)', color: 'var(--text-muted)', marginTop: 2 }}>
                  {s.detail}
                </div>
              )}
            </div>
          </RowCard>
        ))}
      </div>
    </div>
  )
}
