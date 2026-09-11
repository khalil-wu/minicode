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

  it('uses one project list and refreshes its saved folders after connecting', () => {
    render(<ConversationsTab conversationId="conv-represented" onSetConfirmDialog={vi.fn()} />)
    expect(screen.getByText('项目')).toBeTruthy()
    expect(screen.getByText('Existing workspace task')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '选择会话' })).toBeNull()
    expect(screen.queryByRole('region', { name: '最近工作区' })).toBeNull()
    expect(screen.queryByRole('button', { name: '清空最近工作区' })).toBeNull()
    expect(screen.queryByRole('checkbox')).toBeNull()
    act(() => useAppStore.setState({ isConnected: true }))
    expect(sendClientCommandMock).toHaveBeenCalledWith({ type: 'workspace.recent' })
    expect(useAppStore.getState().recentWorkspaces).toHaveLength(2)
  })

  it('keeps saved folders when no conversations exist and can start a task in an empty folder', () => {
    useAppStore.setState({ conversations: [] })
    const original = useAppStore.getState().createConversation
    const create = vi.fn(async () => true)
    useAppStore.setState({ createConversation: create })
    try {
      render(<ConversationsTab conversationId="" onSetConfirmDialog={vi.fn()} />)
      expect(screen.queryByText('开始你的第一个任务')).toBeNull()
      expect(screen.getByRole('region', { name: '工作区 Represented' })).toBeTruthy()
      expect(screen.getByRole('region', { name: '工作区 Tools' })).toBeTruthy()
      fireEvent.click(screen.getByRole('button', { name: '在 Tools 中新建任务' }))
      expect(create).toHaveBeenCalledWith({ bindWorkspace: true, workspaceRoot: 'D:\\External\\Tools', appMode: 'cowork' })
    } finally { useAppStore.setState({ createConversation: original }) }
  })

  it('keeps the same folder row after its last conversation is archived or removed', () => {
    const { unmount } = render(<ConversationsTab conversationId="conv-represented" onSetConfirmDialog={vi.fn()} />)
    const folder = screen.getByRole('region', { name: '工作区 Represented' })
    act(() => useAppStore.setState({ conversations: useAppStore.getState().conversations.map(item => ({ ...item, archived: true })) }))
    expect(screen.getByRole('region', { name: '工作区 Represented' })).toBe(folder)
    expect(screen.queryByText('Existing workspace task')).toBeNull()
    act(() => useAppStore.setState({ conversations: [] }))
    expect(screen.getByRole('region', { name: '工作区 Represented' })).toBe(folder)
    unmount()
    render(<ConversationsTab conversationId="" onSetConfirmDialog={vi.fn()} />)
    expect(screen.getByRole('region', { name: '工作区 Represented' })).toBeTruthy()
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
