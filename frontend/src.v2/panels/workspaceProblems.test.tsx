/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import * as monaco from 'monaco-editor/editor/editor.api.js'
import { typescript as ts } from 'monaco-editor/languages/features/typescript/lib/typescriptServices.js'
import { WorkspaceTypeScriptService } from './workspaceTypeScriptService'
import { WORKSPACE_TYPESCRIPT_METADATA_URI } from './workspaceTypeScriptContract'
import { useAppStore } from '../stores'
import { clearEditorWorkspaceBufferCacheForTests } from '../stores/shared-helpers'
import { useWorkspaceProblems, collectWorkspaceProblems } from './workspaceProblems'
import { ProblemsPanel } from './ProblemsPanel'
import { editorModelUri } from './monacoLanguageServices'
import type { WorkspaceProjectIndex } from '../protocol/workspace'

const runtime = vi.hoisted(() => {
  Object.defineProperty(globalThis, 'matchMedia', { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }) })
  return { service: null as unknown as WorkspaceTypeScriptService, source: 'export const message = "hello";\nexport const result = mesage;', root: '/project' }
})
vi.mock('monaco-editor/languages/definitions/typescript/register.js', () => ({}))
vi.mock('monaco-editor/languages/definitions/javascript/register.js', () => ({}))
vi.mock('../protocol/workspace', () => ({
  readWorkspaceProjectIndex: vi.fn(async (root: string): Promise<WorkspaceProjectIndex> => ({ workspace_root: root, complete: true, issues: [], files: [
    { path: 'tsconfig.json', content: '{"compilerOptions":{"strict":true},"include":["src/**/*.ts"]}', content_hash: 'config-hash', kind: 'config' },
    { path: 'src/unopened.ts', content: runtime.source, content_hash: 'source-hash', kind: 'source' },
  ] })),
  readWorkspaceFile: vi.fn(async (path: string) => ({ path, content: runtime.source, content_hash: 'source-hash', size_bytes: runtime.source.length })),
}))
vi.mock('./monacoLanguageServices', async (original) => ({
  ...await original<typeof import('./monacoLanguageServices')>(),
  configureMiniCodeMonacoWorkers: vi.fn(), loadMiniCodeLanguageServices: vi.fn(async () => {}),
  setWorkspaceTypeScriptFiles: (metadata: unknown, files: Array<{ filePath: string; content: string }>) => {
    const extras = Object.fromEntries(files.map((file) => [file.filePath, { content: file.content, version: 1 }]))
    extras[WORKSPACE_TYPESCRIPT_METADATA_URI] = { content: JSON.stringify(metadata), version: 1 }
    // Exercise the real native TS service against actual Monaco mirror models.
    runtime.service = new WorkspaceTypeScriptService({ getMirrorModels: () => monaco.editor.getModels().map((model) => ({ uri: model.uri, version: model.getVersionId(), getValue: () => model.getValue() })) }, {
      compilerOptions: { target: ts.ScriptTarget.ESNext, module: ts.ModuleKind.ESNext, moduleResolution: ts.ModuleResolutionKind.NodeJs }, extraLibs: extras,
    })
  },
  getWorkspaceTypeScriptWorker: vi.fn(async () => runtime.service),
  syncWorkspaceTypeScriptModels: vi.fn(async () => ({ configurationRequests: async () => [], configurationDiagnostics: async () => [], addConfigurationFiles: async () => {} })),
}))
beforeAll(() => { ['typescript', 'javascript', 'json'].forEach((id) => monaco.languages.register({ id })) })
beforeEach(() => {
  clearEditorWorkspaceBufferCacheForTests(); localStorage.clear()
  runtime.source = 'export const message = "hello";\nexport const result = mesage;'
  useAppStore.setState({ workingDirectory: '/project', editorTabs: [], editorOpenRequests: [], activeEditorOpenRequestId: null, activeTabPath: null, activeEditorPath: null,
    fileChanges: [], panelSlots: [{ id: 'chat', kind: 'chat', focused: true }, { id: 'editor', kind: 'editor' }] })
})
afterEach(() => { cleanup(); monaco.editor.getModels().forEach((model) => model.dispose()); clearEditorWorkspaceBufferCacheForTests(); localStorage.clear() })
const Fixture = () => {
  const root = useAppStore((state) => state.workingDirectory)
  const problems = useWorkspaceProblems(root, true)
  return <ProblemsPanel state={problems.state} onRefresh={problems.refresh} onNavigate={problems.navigate} onQuickFixes={problems.quickFixes} onApplyFix={problems.applyFix} />
}

describe('workspace Problems native diagnostics and edits', () => {
  it('starts from no open code tab, diagnoses an unopened source, applies a real worker fix and preserves native Undo', async () => {
    render(<Fixture />)
    const message = await screen.findByText(/Cannot find name 'mesage'/)
    expect(useAppStore.getState().editorTabs).toHaveLength(0)
    expect(screen.getByText('/project/src/unopened.ts')).toBeTruthy()
    fireEvent.click(message)
    expect(useAppStore.getState().workingDirectory).toBe('/project')
    expect(useAppStore.getState().editorOpenRequests.at(-1)).toMatchObject({ path: 'src/unopened.ts', exact: true, line: 2, column: 23, endLine: 2, endColumn: 29 })
    fireEvent.click(screen.getByRole('button', { name: /查看修复 Cannot find name 'mesage'/ }))
    fireEvent.click(await screen.findByRole('button', { name: /Change spelling to 'message'/ }))
    await screen.findByText(/已修改/)
    const model = monaco.editor.getModel(monaco.Uri.parse(editorModelUri('src/unopened.ts', '/project')))!
    expect(model.getValue()).toContain('result = message')
    const tab = useAppStore.getState().editorTabs.find((entry) => entry.path.endsWith('unopened.ts'))!
    expect(tab.content).toContain('result = message')
    expect(tab.original).toContain('result = mesage')
    expect(tab.contentHash).toBe('source-hash')
    expect(tab.content).not.toBe(tab.original)
    await act(async () => { await model.undo() })
    expect(model.getValue()).toContain('result = mesage')
    await waitFor(() => expect(useAppStore.getState().editorTabs.find((entry) => entry.path.endsWith('unopened.ts'))!.content).toContain('result = mesage'))
  })
  it('merges opened web markers and excludes another workspace and synthetic diff resources', async () => {
    render(<Fixture />)
    await screen.findByText(/Cannot find name 'mesage'/)
    const json = monaco.editor.createModel('{}', 'json', monaco.Uri.parse(editorModelUri('config.json', '/project')))
    const foreign = monaco.editor.createModel('{}', 'json', monaco.Uri.parse(editorModelUri('config.json', '/other')))
    const data = { severity: monaco.MarkerSeverity.Error, message: 'Required property', source: 'JSON', startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 }
    monaco.editor.setModelMarkers(json, 'json', [data])
    monaco.editor.setModelMarkers(foreign, 'json', [{ ...data, message: 'Foreign property' }])
    const problems = await collectWorkspaceProblems(monaco, '/project')
    expect(problems.some((problem) => problem.message === 'Required property' && problem.source === 'JSON')).toBe(true)
    expect(problems.some((problem) => problem.message === 'Foreign property')).toBe(false)
  })
})
