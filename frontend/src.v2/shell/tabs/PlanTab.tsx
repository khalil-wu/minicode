/**
 * Plan tab — displays the active plan and its steps.
 */
import { useAppStore } from '../../stores'
import { EmptyLine, PanelHeader, RowCard, StatusMark } from '../SidebarShared'
import { hasVisiblePlanSteps, isVisiblePlanStepActive, visiblePlanStepStatus } from '../../lib/planVisibility'

export const PlanTab = () => {
  const plan = useAppStore((s) => s.plan)

  if (!hasVisiblePlanSteps(plan)) {
    return (
      <div style={{ display: 'grid', gap: 10 }}>
        <PanelHeader title="计划" />
        <EmptyLine>No proposed plan in this session.</EmptyLine>
      </div>
    )
  }

  const doneCount = plan.steps.filter((s) => s.status === 'done').length

  return (
    <div style={{ display: 'grid', gap: 10 }}>
      <PanelHeader title="计划" meta={`${doneCount}/${plan.steps.length} ${plan.status === 'executing' ? '进行中' : plan.status === 'completed' ? '已完成' : '待开始'}`} />
      <div style={{ display: 'grid', gap: 2 }}>
        {plan.steps.map((s, i) => {
          const displayStatus = visiblePlanStepStatus(plan, s)
          const active = isVisiblePlanStepActive(plan, s, i)
          return (
          <RowCard key={s.id} active={active}>
            <StatusMark status={displayStatus} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{
                color: s.status === 'done' ? 'var(--text-muted)' : 'var(--text-primary)',
                textDecoration: s.status === 'done' ? 'line-through' : 'none',
                fontSize: 'var(--text-xs)',
                lineHeight: 1.45,
                fontWeight: active ? 600 : 400,
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
          )
        })}
      </div>
    </div>
  )
}
