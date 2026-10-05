/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ProblemsPanel } from './ProblemsPanel'
import type { WorkspaceProblem } from './workspaceProblems'

afterEach(cleanup)
const problem = (patch: Partial<WorkspaceProblem> = {}): WorkspaceProblem => ({ id: 'error', path: '/project/a.ts', uri: 'file:///project/a.ts', severity: 'error', source: 'TypeScript', message: 'Real compiler error', code: 2322, range: { startLineNumber: 2, startColumn: 8, endLineNumber: 2, endColumn: 13 }, modelVersion: 1, workerDiagnostic: true, start: 20, length: 5, ...patch })

describe('Problems interactions', () => {
  it('filters files, severities and sources while navigating the exact supplied diagnostic range', () => {
    const navigate = vi.fn()
    render(<ProblemsPanel state={{ phase: 'ready', issues: [], items: [problem(), problem({ id: 'web', path: '/project/b.json', source: 'JSON', severity: 'warning', message: 'Schema warning', workerDiagnostic: false })] }} onRefresh={() => {}} onNavigate={navigate} onQuickFixes={async () => []} onApplyFix={async () => {}} />)
    fireEvent.change(screen.getByRole('combobox', { name: '问题来源' }), { target: { value: 'JSON' } })
    expect(screen.queryByText('Real compiler error')).toBeNull()
    expect(screen.getByText('Schema warning')).toBeTruthy()
    fireEvent.change(screen.getByRole('combobox', { name: '问题来源' }), { target: { value: 'all' } })
    fireEvent.change(screen.getByRole('combobox', { name: '问题级别' }), { target: { value: 'error' } })
    expect(screen.queryByText('Schema warning')).toBeNull()
    fireEvent.click(screen.getByText('Real compiler error'))
    expect(navigate).toHaveBeenCalledWith(expect.objectContaining({ path: '/project/a.ts', range: { startLineNumber: 2, startColumn: 8, endLineNumber: 2, endColumn: 13 } }))
  })
  it('shows only returned code fixes and reports actual changed buffers after the command completes', async () => {
    const fix = { id: 'fix', description: 'Change spelling', changes: [{ path: '/project/a.ts', before: 'mesage', edits: [{ offset: 0, length: 6, text: 'message' }] }] }
    const apply = vi.fn(async () => {})
    render(<ProblemsPanel state={{ phase: 'ready', issues: [], items: [problem()] }} onRefresh={() => {}} onNavigate={() => {}} onQuickFixes={async () => [fix]} onApplyFix={apply} />)
    fireEvent.click(screen.getByRole('button', { name: '查看修复 Real compiler error' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Change spelling' }))
    await waitFor(() => expect(apply).toHaveBeenCalledWith(fix))
    expect(await screen.findByText(/已修改 \/project\/a.ts/)).toBeTruthy()
    expect(screen.getByText(/请检查编辑器中的更改并保存/)).toBeTruthy()
  })
  it('keeps incomplete indexing visible instead of reporting zero problems', () => {
    render(<ProblemsPanel state={{ phase: 'loading', issues: [], items: [] }} onRefresh={() => {}} onNavigate={() => {}} onQuickFixes={async () => []} onApplyFix={async () => {}} />)
    expect(screen.getByText('正在建立项目索引并读取诊断…')).toBeTruthy()
    expect(screen.queryByText(/当前支持的项目代码没有报告问题/)).toBeNull()
  })
})
