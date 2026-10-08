import type * as Monaco from 'monaco-editor/editor/editor.api.js'
import type * as TS from 'typescript'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useAppStore } from '../stores'
import { readWorkspaceFile } from '../protocol/workspace'
import { normalizeWorkspacePath, workspaceFilePathsEqual, workspacePathWithin, workspaceRootsEqual } from '../lib/workspace-path'
import { configureMiniCodeMonacoWorkers, editorModelUri, getWorkspaceTypeScriptWorker, loadMiniCodeLanguageServices } from './monacoLanguageServices'
import { useWorkspaceModelIndex } from './useWorkspaceModelIndex'
import { isDependencyIndexPath } from './workspaceModelIndex'
import { applyWorkspaceBufferEdits, offsetRange, type WorkspaceBufferEdit } from './applyWorkspaceBufferEdits'

export interface WorkspaceProblem {
  id: string
  path: string
  uri: string
  severity: 'error' | 'warning' | 'info' | 'hint'
  source: string
  message: string
  code?: number | string
  range: Monaco.IRange
  modelVersion: number
  workerDiagnostic: boolean
  start: number
  length: number
  sourceText?: string
}
export interface ProblemQuickFix { id: string; description: string; changes: WorkspaceBufferEdit[] }
export interface WorkspaceProblemsState {
  phase: 'idle' | 'loading' | 'ready' | 'partial' | 'error'
  items: WorkspaceProblem[]
  issues: Array<{ path: string; message: string }>
}

const flattenMessage = (message: string | TS.DiagnosticMessageChain): string => typeof message === 'string'
  ? message : [message.messageText, ...(message.next ?? []).map(flattenMessage)].join('\n')

export function problemFromTypeScriptDiagnostic(model: Monaco.editor.ITextModel, diagnostic: TS.Diagnostic): WorkspaceProblem {
  return problemFromSourceDiagnostic(model.uri, model.getValue(undefined, true), model.getVersionId(), diagnostic)
}

function problemFromSourceDiagnostic(uri: Monaco.Uri, content: string, version: number, diagnostic: TS.Diagnostic): WorkspaceProblem {
  const start = diagnostic.start ?? 0
  const length = diagnostic.length ?? 0
  const resource = uri.toString()
  return {
    id: `typescript:${resource}:${diagnostic.code}:${start}:${length}`, path: normalizeWorkspacePath(uri.fsPath), uri: resource,
    severity: diagnostic.category === 1 ? 'error' : diagnostic.category === 0 ? 'warning' : diagnostic.category === 2 ? 'hint' : 'info',
    source: 'TypeScript', code: diagnostic.code, message: flattenMessage(diagnostic.messageText),
    range: offsetRange(content, { offset: start, length, text: '' }),
    modelVersion: version, workerDiagnostic: true, start, length, sourceText: content,
  }
}

const isTypeScriptModel = (model: Monaco.editor.ITextModel) => ['typescript', 'javascript'].includes(model.getLanguageId())
const webMarkerLanguages = new Set(['html', 'handlebars', 'razor', 'css', 'scss', 'less', 'json', 'python', 'yaml'])
export async function collectWorkspaceProblems(monaco: typeof Monaco, workspaceRoot: string, indexedSources: Array<{ uri: Monaco.Uri; content: string }> = []): Promise<WorkspaceProblem[]> {
  const models = monaco.editor.getModels().filter((model) => model.uri.scheme === 'file'
    && workspacePathWithin(model.uri.fsPath, workspaceRoot) && !isDependencyIndexPath(model.uri.fsPath))
  const sources = new Map(indexedSources.map((source) => [source.uri.toString(), source]))
  for (const model of models.filter(isTypeScriptModel)) sources.set(model.uri.toString(), { uri: model.uri, content: model.getValue(undefined, true) })
  const problems = await Promise.all([...sources.values()].map(async (source) => {
    const worker = await getWorkspaceTypeScriptWorker(source.uri)
    const resource = source.uri.toString()
    const content = (await worker.getScriptText(resource))!
    const model = monaco.editor.getModel(source.uri)
    const diagnostics = (await Promise.all([worker.getSyntacticDiagnostics(resource), worker.getSemanticDiagnostics(resource), worker.getSuggestionDiagnostics(resource)])).flat()
    return diagnostics.map((diagnostic) => problemFromSourceDiagnostic(source.uri, content, model?.getVersionId() ?? 0, diagnostic as TS.Diagnostic))
  }))
  const webModels = new Map(models.filter((model) => webMarkerLanguages.has(model.getLanguageId())).map((model) => [model.uri.toString(), model]))
  const markers = monaco.editor.getModelMarkers({}).flatMap((marker) => {
    const model = webModels.get(marker.resource.toString())
    if (!model) return []
    const range = { startLineNumber: marker.startLineNumber, startColumn: marker.startColumn, endLineNumber: marker.endLineNumber, endColumn: marker.endColumn }
    const start = model.getOffsetAt({ lineNumber: marker.startLineNumber, column: marker.startColumn })
    const code = typeof marker.code === 'object' ? marker.code.value : marker.code
    return [{ id: `${marker.owner}:${marker.resource.toString()}:${code ?? ''}:${start}:${marker.message}`, path: normalizeWorkspacePath(marker.resource.fsPath),
      uri: marker.resource.toString(), source: marker.source || marker.owner, code, message: marker.message,
      severity: marker.severity === monaco.MarkerSeverity.Error ? 'error' : marker.severity === monaco.MarkerSeverity.Warning ? 'warning' : marker.severity === monaco.MarkerSeverity.Hint ? 'hint' : 'info',
      range, modelVersion: model.getVersionId(), workerDiagnostic: false, start,
      length: model.getOffsetAt({ lineNumber: marker.endLineNumber, column: marker.endColumn }) - start,
    } satisfies WorkspaceProblem]
  })
  const items = new Map([...problems.flat(), ...markers].map((problem) => [problem.id, problem]))
  return [...items.values()].sort((left, right) => left.path.localeCompare(right.path) || left.range.startLineNumber - right.range.startLineNumber || left.range.startColumn - right.range.startColumn)
}

export function useWorkspaceProblems(workspaceRoot: string, visible: boolean) {
  const projectIndex = useWorkspaceModelIndex(workspaceRoot)
  const monacoRef = useRef<typeof Monaco | null>(null)
  const [monacoEpoch, setMonacoEpoch] = useState(0)
  const [scanEpoch, setScanEpoch] = useState(0)
  const [snapshot, setSnapshot] = useState<{ workspaceRoot: string; state: WorkspaceProblemsState }>({ workspaceRoot, state: { phase: 'idle', items: [], issues: [] } })
  const state = workspaceRootsEqual(snapshot.workspaceRoot, workspaceRoot) ? snapshot.state : { phase: visible && workspaceRoot ? 'loading' as const : 'idle' as const, items: [], issues: [] }
  const setState = (next: WorkspaceProblemsState) => setSnapshot({ workspaceRoot, state: next })
  useEffect(() => {
    if (!visible || !workspaceRoot) return
    let cancelled = false
    setState({ phase: 'loading', items: [], issues: [] })
    const initialize = async () => {
      configureMiniCodeMonacoWorkers()
      const [monaco] = await Promise.all([import('monaco-editor/editor/editor.api.js'), loadMiniCodeLanguageServices(),
        import('monaco-editor/languages/definitions/typescript/register.js'), import('monaco-editor/languages/definitions/javascript/register.js')])
      if (cancelled) return
      monacoRef.current = monaco
      projectIndex.initialize(monaco)
      setMonacoEpoch((value) => value + 1)
    }
    void initialize().catch((error: unknown) => { if (!cancelled) setState({ phase: 'error', items: [], issues: [{ path: workspaceRoot, message: error instanceof Error ? error.message : String(error) }] }) })
    return () => { cancelled = true }
  }, [workspaceRoot, visible, projectIndex.initialize])

  useEffect(() => {
    const monaco = monacoRef.current
    if (!visible) { setState({ phase: 'idle', items: [], issues: [] }); return }
    if (!monaco || projectIndex.status.phase === 'idle' || projectIndex.status.phase === 'loading') return
    if (projectIndex.status.phase === 'error') { setState({ phase: 'error', items: [], issues: projectIndex.status.issues }); return }
    let disposed = false
    let revision = 0
    let timer: number | undefined
    const listeners = new Map<Monaco.editor.ITextModel, Monaco.IDisposable>()
    const schedule = () => {
      const request = ++revision
      window.clearTimeout(timer)
      timer = window.setTimeout(() => {
        void collectWorkspaceProblems(monaco, workspaceRoot, projectIndex.sourceFiles()).then((items) => {
          if (!disposed && request === revision && workspaceRootsEqual(useAppStore.getState().workingDirectory, workspaceRoot)) setState({ phase: projectIndex.status.phase === 'partial' ? 'partial' : 'ready', items, issues: projectIndex.status.issues })
        }).catch((error: unknown) => {
          if (!disposed && request === revision) setState({ phase: 'error', items: [], issues: [{ path: workspaceRoot, message: error instanceof Error ? error.message : String(error) }] })
        })
      }, 180)
    }
    const attach = (model: Monaco.editor.ITextModel) => {
      if (model.uri.scheme === 'file' && workspacePathWithin(model.uri.fsPath, workspaceRoot)) listeners.set(model, model.onDidChangeContent(schedule))
      schedule()
    }
    monaco.editor.getModels().forEach(attach)
    const creation = monaco.editor.onDidCreateModel(attach)
    const markers = monaco.editor.onDidChangeMarkers(schedule)
    schedule()
    return () => { disposed = true; window.clearTimeout(timer); creation.dispose(); markers.dispose(); listeners.forEach((listener) => listener.dispose()) }
  }, [workspaceRoot, visible, monacoEpoch, scanEpoch, projectIndex.status])

  const navigate = useCallback((problem: WorkspaceProblem) => {
    useAppStore.getState().openEditorFile(problem.path, problem.path.split(/[/\\]/).pop(), {
      exact: true, line: problem.range.startLineNumber, column: problem.range.startColumn,
      endLine: problem.range.endLineNumber, endColumn: problem.range.endColumn,
    })
  }, [])
  const quickFixes = useCallback(async (problem: WorkspaceProblem): Promise<ProblemQuickFix[]> => {
    if (!problem.workerDiagnostic || typeof problem.code !== 'number') return []
    const monaco = monacoRef.current!
    const resource = monaco.Uri.parse(problem.uri)
    const model = monaco.editor.getModel(resource)
    const worker = await getWorkspaceTypeScriptWorker(resource)
    if ((model && problem.modelVersion !== 0 && model.getVersionId() !== problem.modelVersion)
      || (await worker.getScriptText(problem.uri)) !== problem.sourceText) throw new Error('文件内容已变化，请刷新问题后再选择修复。')
    const versions = new Map(monaco.editor.getModels().map((entry) => [entry.uri.toString(), entry.getVersionId()]))
    const actions = await worker.getCodeFixesAtPosition(problem.uri, problem.start, problem.start + problem.length, [problem.code], {}) as readonly TS.CodeFixAction[]
    const fixes: ProblemQuickFix[] = []
    for (const action of actions) {
      if (!action.changes.length || action.commands?.length || action.changes.some((change) => change.isNewFile || !workspacePathWithin(monaco.Uri.parse(change.fileName).fsPath, workspaceRoot))) continue
      const changes: WorkspaceBufferEdit[] = []
      for (const change of action.changes) {
        const uri = monaco.Uri.parse(change.fileName)
        const path = normalizeWorkspacePath(uri.fsPath)
        const source = monaco.editor.getModel(uri)
        if (source && source.getVersionId() !== versions.get(uri.toString())) throw new Error('修复涉及的文件已变化，请重新读取可用修复。')
        const tab = useAppStore.getState().editorTabs.find((entry) => workspaceFilePathsEqual(entry.path, path, workspaceRoot))
        if (tab?.readOnly) { changes.length = 0; break }
        const disk = tab ? null : await readWorkspaceFile(path, workspaceRoot)
        const before = source ? source.getValue(undefined, true) : (await worker.getScriptText(change.fileName))!
        if (disk && before !== disk.content) throw new Error('修复涉及的文件已在磁盘上变化，请刷新项目索引后重试。')
        changes.push({ path, before, original: tab?.original ?? disk!.content,
          contentHash: tab?.contentHash ?? disk?.content_hash, sizeBytes: tab?.sizeBytes ?? disk?.size_bytes,
          expectedVersion: source?.getVersionId(), edits: change.textChanges.map((edit) => ({ offset: edit.span.start, length: edit.span.length, text: edit.newText })) })
      }
      if (changes.length) fixes.push({ id: `${problem.id}:${action.fixName}:${action.description}`, description: action.description, changes })
    }
    return fixes
  }, [workspaceRoot])
  const applyFix = useCallback(async (fix: ProblemQuickFix) => {
    await applyWorkspaceBufferEdits(workspaceRoot, fix.changes, fix.description)
    const first = fix.changes.find((change) => change.edits.length)
    if (first) {
      const edit = [...first.edits].sort((left, right) => left.offset - right.offset)[0]
      const model = monacoRef.current!.editor.getModel(monacoRef.current!.Uri.parse(editorModelUri(first.path, workspaceRoot)))!
      const start = model.getPositionAt(edit.offset)
      const end = model.getPositionAt(edit.offset + edit.text.length)
      useAppStore.getState().openEditorFile(first.path, undefined, { exact: true, line: start.lineNumber, column: start.column, endLine: end.lineNumber, endColumn: end.column })
    }
    setScanEpoch((value) => value + 1)
  }, [workspaceRoot])
  const refresh = useCallback(() => { projectIndex.refresh(); setScanEpoch((value) => value + 1) }, [projectIndex.refresh])
  return { state, navigate, quickFixes, applyFix, refresh }
}
