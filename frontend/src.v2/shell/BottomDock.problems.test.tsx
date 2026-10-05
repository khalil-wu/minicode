/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { BottomDock } from './BottomDock'
import { useAppStore } from '../stores'

const diagnostics = vi.hoisted(() => {
  Object.defineProperty(globalThis, 'matchMedia', { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) })
  return { hook: vi.fn() }
})
vi.mock('../panels/workspaceProblems', () => ({ useWorkspaceProblems: diagnostics.hook }))
vi.mock('../panels/GitPanel', () => ({ GitPanel: () => <div>Git</div> }))
afterEach(cleanup)

it('opens Problems without an editor tab and counts errors separately from hints', async () => {
  diagnostics.hook.mockReturnValue({ state: { phase: 'ready', issues: [], items: [
    { id: 'error', path: '/project/a.ts', severity: 'error', source: 'TypeScript', message: 'Compiler error', range: { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 }, workerDiagnostic: true },
    { id: 'hint', path: '/project/a.ts', severity: 'hint', source: 'TypeScript', message: 'Unused declaration', range: { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 }, workerDiagnostic: true },
  ] }, navigate: vi.fn(), quickFixes: vi.fn(), applyFix: vi.fn(), refresh: vi.fn() })
  useAppStore.setState({ workingDirectory: '/project', editorTabs: [], dockCollapsed: false, activeBottomTab: 'git', settingsOpen: false, skillsMarketplaceOpen: false, rightPanelExpanded: false })
  render(<BottomDock />)
  fireEvent.click(screen.getByRole('tab', { name: '问题' }))
  expect(useAppStore.getState().activeBottomTab).toBe('problems')
  expect(await screen.findByText('1 错误 · 0 警告 · 2 项匹配')).toBeTruthy()
  expect(diagnostics.hook).toHaveBeenLastCalledWith('/project', true)
  expect(screen.getByRole('tab', { name: '问题 1 个错误' })).toBeTruthy()
})
