import { useEffect } from 'react'
import { useAppStore } from '../stores'
import { useWorkspaceProblems } from './workspaceProblems'
import { ProblemsPanel } from './ProblemsPanel'

export const ProblemsWorkspace = ({ visible, onErrorCount }: { visible: boolean; onErrorCount: (count: number | null) => void }) => {
  const workspaceRoot = useAppStore((state) => state.workingDirectory)
  const problems = useWorkspaceProblems(workspaceRoot, visible)
  useEffect(() => {
    onErrorCount(['ready', 'partial'].includes(problems.state.phase)
      ? problems.state.items.filter((problem) => problem.severity === 'error').length : null)
  }, [onErrorCount, problems.state])
  return <ProblemsPanel key={workspaceRoot} state={problems.state} onRefresh={problems.refresh} onNavigate={problems.navigate} onQuickFixes={problems.quickFixes} onApplyFix={problems.applyFix} />
}
