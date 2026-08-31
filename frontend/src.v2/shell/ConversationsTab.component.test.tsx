/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const {
  pushToastMock,
  sendClientCommandMock,
  sendClientCommandAwaitResultMock,
  sendConversationDeleteCommandMock,
} = vi.hoisted(() => ({
  pushToastMock: vi.fn(),
  sendClientCommandMock: vi.fn(() => true),
  sendClientCommandAwaitResultMock: vi.fn(async (command: { type: string }) => ({
    type: "command_result",
    command: command.type,
    level: "success",
    message: "",
    data: {},
  })),
  sendConversationDeleteCommandMock: vi.fn(async () => true),
}))

vi.mock('../desktop/runtime', () => ({
  isDesktop: () => false,
  revealPath: vi.fn(),
}))

vi.mock('../overlays/ToastContainer', () => ({
  pushToast: pushToastMock,
}))

vi.mock('../protocol/ws-outbox', () => ({
  sendClientCommand: sendClientCommandMock,
  sendClientCommandAwaitResult: sendClientCommandAwaitResultMock,
  sendConversationDeleteCommand: sendConversationDeleteCommandMock,
  commandResultSucceeded: (event: { level?: string }) => !['error', 'failed'].includes(String(event.level || '')),
}))

import { useAppStore } from '../stores'
import { ConversationsTab, recentWorkspaceMetadata } from './ConversationsTab'

describe('ConversationsTab recent workspace projection', () => {
  beforeEach(() => {
    localStorage.removeItem('minicode.sidebar.conversations.state')
    pushToastMock.mockClear()
    sendClientCommandMock.mockReset()
    sendClientCommandMock.mockReturnValue(true)
    sendClientCommandAwaitResultMock.mockClear()
    sendConversationDeleteCommandMock.mockClear()
    useAppStore.setState({
      appMode: 'cowork',
      conversationId: 'conv-represented',
      conversations: [{
        id: 'conv-represented',
        title: 'Existing workspace task',
        updatedAt: '2026-08-15T00:00:00.000Z',
        workspaceRoot: 'C:\\Represented',
      }],
      conversationMessages: {},
      conversationStreaming: {},
      conversationHydration: {},
      recentWorkspaces: [
        {
          path: 'C:\\Represented',
          name: 'Represented workspace',
          projectType: 'node',
          lastOpened: 1_787_000_000,
        },
        {
          path: 'D:\\External\\Tools',
          name: 'External Tools',
          projectType: 'python',
          lastOpened: 1_787_100_000,
        },
      ],
      isConnected: false,
      isStreaming: false,
      pendingApproval: null,
      approvalQueue: [],
      pendingDiffReview: null,
      diffReviewQueue: [],
      pendingAskUser: null,
      askUserQueue: [],
      runtimeSession: null,
      workingDirectory: 'C:\\Represented',
      workspaceGit: null,
    })
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('requests recents on connection and renders actionable name, path, type, and last-opened time', async () => {
    render(
      <ConversationsTab
        conversationId="conv-represented"
        onSetConfirmDialog={vi.fn()}
      />,
    )

    expect(sendClientCommandMock).not.toHaveBeenCalledWith({ type: 'workspace.recent' })
    act(() => useAppStore.setState({ isConnected: true }))
    await waitFor(() => expect(sendClientCommandMock).toHaveBeenCalledWith({ type: 'workspace.recent' }))

    const recentSection = screen.getByRole('region', { name: '最近工作区' })
    expect(within(recentSection).getByText('External Tools')).toBeTruthy()
    expect(within(recentSection).getByText('D:\\External\\Tools')).toBeTruthy()
    expect(within(recentSection).getByText(recentWorkspaceMetadata('python', 1_787_100_000))).toBeTruthy()
    expect(within(recentSection).queryByText('Represented workspace')).toBeNull()

    sendClientCommandMock.mockClear()
    fireEvent.click(within(recentSection).getByRole('button', { name: /^External Tools/ }))

    expect(sendClientCommandMock).toHaveBeenCalledWith({
      type: 'workspace.set',
      path: 'D:\\External\\Tools',
    })
    expect(useAppStore.getState().appMode).toBe('code')
    expect(pushToastMock).toHaveBeenCalledWith('正在打开工作区：D:\\External\\Tools', 'info', 2600)
  })

  it('uses explicit fallback metadata instead of displaying invalid or empty values', () => {
    expect(recentWorkspaceMetadata('unknown', Number.NaN)).toBe('类型未知 · 打开时间未知')
  })

  it('removes one recent-workspace record through the MRU command only', async () => {
    render(
      <ConversationsTab
        conversationId="conv-represented"
        onSetConfirmDialog={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '删除最近工作区记录 External Tools' }))

    await waitFor(() => expect(sendClientCommandAwaitResultMock).toHaveBeenCalledWith({
      type: 'workspace.recent.remove',
      path: 'D:\\External\\Tools',
    }, 'workspace.recent.remove'))
    expect(sendClientCommandMock).not.toHaveBeenCalledWith(expect.objectContaining({
      type: expect.stringMatching(/delete|remove/i),
      path: 'D:\\External\\Tools',
    }))
  })

  it('confirms that clearing recents preserves project files and sends only the clear command', async () => {
    const onSetConfirmDialog = vi.fn()
    render(
      <ConversationsTab
        conversationId="conv-represented"
        onSetConfirmDialog={onSetConfirmDialog}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '清空最近工作区' }))
    const dialog = onSetConfirmDialog.mock.calls[0]?.[0]
    expect(dialog).toMatchObject({
      title: '清空最近工作区',
      message: '只清除最近工作区列表，不会删除任何文件或项目目录。',
      confirmLabel: '清空列表',
      danger: true,
    })

    act(() => dialog.onConfirm())
    await waitFor(() => expect(sendClientCommandAwaitResultMock).toHaveBeenCalledWith(
      { type: 'workspace.recent.clear' },
      'workspace.recent.clear',
    ))
    expect(sendClientCommandMock).not.toHaveBeenCalledWith(expect.objectContaining({
      type: expect.stringMatching(/delete|remove/i),
    }))
  })

  it('does not expose session deletion from the conversation menu', async () => {
    const onSetConfirmDialog = vi.fn()
    render(
      <ConversationsTab
        conversationId="conv-represented"
        onSetConfirmDialog={onSetConfirmDialog}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '会话操作' }))
    expect(screen.queryByRole('menuitem', { name: '删除' })).toBeNull()
    expect(onSetConfirmDialog).not.toHaveBeenCalled()
  })

  it('does not expose batch session deletion in selection mode', async () => {
    useAppStore.setState({
      conversationId: 'conv-first',
      conversations: [
        { id: 'conv-first', title: 'First task', updatedAt: '2026-08-15T00:00:00.000Z' },
        { id: 'conv-second', title: 'Second task', updatedAt: '2026-08-15T00:00:01.000Z' },
      ],
      recentWorkspaces: [],
    })
    const onSetConfirmDialog = vi.fn()
    render(<ConversationsTab conversationId="conv-first" onSetConfirmDialog={onSetConfirmDialog} />)

    fireEvent.click(screen.getByRole('button', { name: '选择会话' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select First task' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Second task' }))
    expect(screen.getByText('已选择 2 个')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '删除所选会话' })).toBeNull()
    expect(sendConversationDeleteCommandMock).not.toHaveBeenCalled()
  })

  it('debounces sidebar scroll persistence off the interaction path', () => {
    vi.useFakeTimers()
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem')
    render(
      <ConversationsTab
        conversationId="conv-represented"
        onSetConfirmDialog={vi.fn()}
      />,
    )

    const list = screen.getByTestId('conversation-list')
    Object.defineProperty(list, 'scrollTop', { configurable: true, value: 120, writable: true })
    fireEvent.scroll(list)
    Object.defineProperty(list, 'scrollTop', { configurable: true, value: 260, writable: true })
    fireEvent.scroll(list)

    expect(setItemSpy).not.toHaveBeenCalledWith(
      'minicode.sidebar.conversations.state',
      expect.any(String),
    )
    act(() => vi.advanceTimersByTime(139))
    expect(setItemSpy).not.toHaveBeenCalledWith(
      'minicode.sidebar.conversations.state',
      expect.any(String),
    )
    act(() => vi.advanceTimersByTime(1))

    const persisted = setItemSpy.mock.calls.find(([key]) => key === 'minicode.sidebar.conversations.state')
    expect(persisted).toBeTruthy()
    expect(JSON.parse(String(persisted?.[1]))).toMatchObject({ scrollTop: 260 })
    setItemSpy.mockRestore()
  })
})
