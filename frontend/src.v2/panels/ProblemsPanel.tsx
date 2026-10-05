import { ChevronDown, ChevronRight, CircleAlert, CircleX, Info, Lightbulb, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import type { ProblemQuickFix, WorkspaceProblem, WorkspaceProblemsState } from './workspaceProblems'
import './ProblemsPanel.css'

interface ProblemsPanelProps {
  state: WorkspaceProblemsState
  onRefresh: () => void
  onNavigate: (problem: WorkspaceProblem) => void
  onQuickFixes: (problem: WorkspaceProblem) => Promise<ProblemQuickFix[]>
  onApplyFix: (fix: ProblemQuickFix) => Promise<void>
}
const severityLabel = { error: '错误', warning: '警告', info: '信息', hint: '提示' }
const SeverityIcon = ({ severity }: { severity: WorkspaceProblem['severity'] }) => {
  const Icon = severity === 'error' ? CircleX : severity === 'warning' ? CircleAlert : severity === 'hint' ? Lightbulb : Info
  return <Icon size={14} className={`mc-problem-severity mc-problem-severity-${severity}`} />
}

export const ProblemsPanel = ({ state, onRefresh, onNavigate, onQuickFixes, onApplyFix }: ProblemsPanelProps) => {
  const [file, setFile] = useState('')
  const [severity, setSeverity] = useState('all')
  const [source, setSource] = useState('all')
  const [collapsed, setCollapsed] = useState(new Set<string>())
  const [fixes, setFixes] = useState<{ problemId: string; items: ProblemQuickFix[] } | null>(null)
  const [busyProblem, setBusyProblem] = useState('')
  const [result, setResult] = useState('')
  const [error, setError] = useState('')
  const filtered = state.items.filter((problem) => problem.path.toLocaleLowerCase().includes(file.trim().toLocaleLowerCase())
    && (severity === 'all' || problem.severity === severity) && (source === 'all' || problem.source === source))
  const groups = new Map<string, WorkspaceProblem[]>()
  filtered.forEach((problem) => groups.set(problem.path, [...(groups.get(problem.path) ?? []), problem]))
  const errors = state.items.filter((problem) => problem.severity === 'error').length
  const warnings = state.items.filter((problem) => problem.severity === 'warning').length
  const readFixes = async (problem: WorkspaceProblem) => {
    setBusyProblem(problem.id); setError(''); setResult(''); setFixes(null)
    try {
      const items = await onQuickFixes(problem)
      setFixes({ problemId: problem.id, items })
    } catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)) }
    finally { setBusyProblem('') }
  }
  const apply = async (fix: ProblemQuickFix) => {
    setBusyProblem(fixes!.problemId); setError(''); setResult('')
    try {
      await onApplyFix(fix)
      setResult(`已修改 ${fix.changes.map((change) => change.path).join('、')}；请检查编辑器中的更改并保存。`)
      setFixes(null)
    } catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)) }
    finally { setBusyProblem('') }
  }
  return <section className="mc-problems-panel" aria-label="代码问题">
    <div className="mc-problems-toolbar">
      <input aria-label="按文件筛选问题" placeholder="筛选文件…" value={file} onChange={(event) => setFile(event.target.value)} />
      <select aria-label="问题级别" value={severity} onChange={(event) => setSeverity(event.target.value)}><option value="all">全部级别</option>{Object.entries(severityLabel).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select>
      <select aria-label="问题来源" value={source} onChange={(event) => setSource(event.target.value)}><option value="all">全部来源</option>{[...new Set(state.items.map((problem) => problem.source))].map((value) => <option value={value} key={value}>{value}</option>)}</select>
      <span className="mc-problems-count">{state.phase === 'ready' || state.phase === 'partial' ? `${errors} 错误 · ${warnings} 警告 · ${filtered.length} 项匹配` : '诊断尚未完成'}</span>
      <button type="button" className="btn-ghost mc-icon-button" aria-label="刷新代码问题" onClick={onRefresh}><RefreshCw size={14} /></button>
    </div>
    {state.phase === 'loading' && <p role="status" className="mc-problems-message">正在建立项目索引并读取诊断…</p>}
    {state.phase === 'idle' && <p className="mc-problems-message">打开工作区后可检查项目代码。</p>}
    {state.issues.map((issue) => <p role="alert" className="mc-problems-message" key={`${issue.path}:${issue.message}`}>{issue.path} · {issue.message}</p>)}
    {error && <p role="alert" className="mc-problems-error">{error}</p>}
    {result && <p role="status" className="mc-problems-result">{result}</p>}
    <div className="mc-problems-list">
      {[...groups].map(([path, problems]) => <div key={path} className="mc-problems-file">
        <button type="button" className="mc-problems-file-heading" aria-expanded={!collapsed.has(path)} onClick={() => setCollapsed((current) => { const next = new Set(current); next.has(path) ? next.delete(path) : next.add(path); return next })}>
          {collapsed.has(path) ? <ChevronRight size={14} /> : <ChevronDown size={14} />}<span>{path}</span><small>{problems.length}</small>
        </button>
        {!collapsed.has(path) && problems.map((problem) => <div key={problem.id}>
          <div className="mc-problem-row">
            <button type="button" className="mc-problem-location" onClick={() => onNavigate(problem)} title={`${problem.path}:${problem.range.startLineNumber}:${problem.range.startColumn}`}>
              <SeverityIcon severity={problem.severity} /><span className="mc-problem-text">{problem.message}</span><span className="mc-problem-meta">{problem.source}{problem.code !== undefined ? `(${problem.code})` : ''} · {problem.range.startLineNumber}:{problem.range.startColumn}</span>
            </button>
            {problem.workerDiagnostic && <button type="button" className="btn-ghost mc-icon-button" aria-label={`查看修复 ${problem.message}`} title="查看可用修复" disabled={Boolean(busyProblem)} onClick={() => void readFixes(problem)}><Lightbulb size={14} /></button>}
          </div>
          {fixes?.problemId === problem.id && <div className="mc-problem-fixes">
            {fixes.items.length === 0 ? <span>当前没有可直接应用的修复。</span> : fixes.items.map((fix) => <button type="button" key={fix.id} className="btn-ghost" disabled={Boolean(busyProblem)} onClick={() => void apply(fix)}>{fix.description}</button>)}
          </div>}
        </div>)}
      </div>)}
      {(state.phase === 'ready' || state.phase === 'partial') && filtered.length === 0 && <p className="mc-problems-message">{state.items.length ? '没有符合筛选条件的问题。' : '当前支持的项目代码没有报告问题。'}</p>}
    </div>
  </section>
}
