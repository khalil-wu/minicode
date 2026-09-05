/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { sendClientCommandMock } = vi.hoisted(() => ({
  sendClientCommandMock: vi.fn(() => true),
}))

vi.mock('../desktop/runtime', () => ({ isDesktop: () => false, revealPath: vi.fn() }))
vi.mock('../overlays/ToastContainer', () => ({ pushToast: vi.fn() }))
vi.mock('../protocol/ws-outbox', () => ({
  sendClientCommand: sendClientCommandMock,
  sendClientCommandAwaitResult: vi.fn(async (command: { type: string }) => ({
    type: 'command_result', command: command.type, level: 'success', message: '', data: {},
  })),
  sendConversationDeleteCommand: vi.fn(async () => true),
  commandResultSucceeded: (event: { level?: string }) => !['error', 'failed'].includes(String(event.level || '')),
}))

import { useAppStore } from '../stores'
import { ConversationsTab } from './ConversationsTab'

describe('ConversationsTab project navigation', () => {
  beforeEach(() => {
    localStorage.removeItem('minicode.sidebar.conversations.state')
    sendClientCommandMock.mockClear()
    useAppStore.setState({
      appMode: 'cowork',
      conversationId: 'conv-represented',
      conversations: [{
        id: 'conv-represented', title: 'Existing workspace task',
        updatedAt: '2026-08-15T00:00:00.000Z', workspaceRoot: 'C:\\Represented',
      }],
      conversationMessages: {}, conversationStreaming: {}, conversationHydration: {},
      recentWorkspaces: [
        { path: 'C:\\Represented', name: 'Represented workspace', projectType: 'node', lastOpened: 1_787_000_000 },
        { path: 'D:\\External\\Tools', name: 'External Tools', projectType: 'python', lastOpened: 1_787_100_000 },
      ],
      isConnected: false, isStreaming: false,
      pendingApproval: null, approvalQueue: [], pendingDiffReview: null, diffReviewQueue: [],
      pendingAskUser: null, askUserQueue: [], runtimeSession: null,
      workingDirectory: 'C:\\Represented', workspaceGit: null,
    })
  })

  afterEach(() => { cleanup(); vi.useRealTimers() })

  it('removes selection and recent-workspace controls without deleting saved history', () => {
    render(<ConversationsTab conversationId="conv-represented" onSetConfirmDialog={vi.fn()} />)
    expect(screen.getByText('项目')).toBeTruthy()
    expect(screen.getByText('Existing workspace task')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '选择会话' })).toBeNull()
    expect(screen.queryByRole('region', { name: '最近工作区' })).toBeNull()
    expect(screen.queryByRole('button', { name: '清空最近工作区' })).toBeNull()
    expect(screen.queryByRole('checkbox')).toBeNull()
    act(() => useAppStore.setState({ isConnected: true }))
    expect(sendClientCommandMock).not.toHaveBeenCalledWith({ type: 'workspace.recent' })
    expect(useAppStore.getState().recentWorkspaces).toHaveLength(2)
  })

  it('shows the task empty state even when saved recent workspaces exist', () => {
    useAppStore.setState({ conversations: [] })
    render(<ConversationsTab conversationId="" onSetConfirmDialog={vi.fn()} />)
    expect(screen.getByText('开始你的第一个任务')).toBeTruthy()
    expect(screen.getByRole('button', { name: '新建任务' })).toBeTruthy()
    expect(screen.queryByText('最近工作区')).toBeNull()
  })

  it('excludes archived tasks while keeping active tasks visible', () => {
    useAppStore.setState({ conversations: [
      ...useAppStore.getState().conversations,
      { id: 'archived', title: 'Archived task', updatedAt: '2026-08-15', archived: true },
    ] })
    render(<ConversationsTab conversationId="conv-represented" onSetConfirmDialog={vi.fn()} />)
    expect(screen.queryByText('Archived task')).toBeNull()
    expect(screen.getByText('Existing workspace task')).toBeTruthy()
  })

  it('does not expose session deletion from the conversation menu', () => {
    const onSetConfirmDialog = vi.fn()
    render(<ConversationsTab conversationId="conv-represented" onSetConfirmDialog={onSetConfirmDialog} />)
    fireEvent.click(screen.getByRole('button', { name: '会话操作' }))
    expect(screen.queryByRole('menuitem', { name: '删除' })).toBeNull()
    expect(onSetConfirmDialog).not.toHaveBeenCalled()
  })

  it('debounces sidebar scroll persistence off the interaction path', () => {
    vi.useFakeTimers()
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem')
    render(<ConversationsTab conversationId="conv-represented" onSetConfirmDialog={vi.fn()} />)
    const list = screen.getByTestId('conversation-list')
    Object.defineProperty(list, 'scrollTop', { configurable: true, value: 120, writable: true })
    fireEvent.scroll(list)
    Object.defineProperty(list, 'scrollTop', { configurable: true, value: 260, writable: true })
    fireEvent.scroll(list)
    expect(setItemSpy).not.toHaveBeenCalledWith('minicode.sidebar.conversations.state', expect.any(String))
    act(() => vi.advanceTimersByTime(139))
    expect(setItemSpy).not.toHaveBeenCalledWith('minicode.sidebar.conversations.state', expect.any(String))
    act(() => vi.advanceTimersByTime(1))
    const persisted = setItemSpy.mock.calls.find(([key]) => key === 'minicode.sidebar.conversations.state')
    expect(JSON.parse(String(persisted?.[1]))).toMatchObject({ scrollTop: 260 })
    setItemSpy.mockRestore()
  })
})
