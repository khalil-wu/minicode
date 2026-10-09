/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { sendClientCommandMock, openWorkspaceFolderMock } = vi.hoisted(() => ({
  sendClientCommandMock: vi.fn(() => true),
  openWorkspaceFolderMock: vi.fn(async (): Promise<string | null> => 'C:\\OpenedProject'),
}))

vi.mock('../desktop/runtime', () => ({ isDesktop: () => false, revealPath: vi.fn() }))
vi.mock('../overlays/ToastContainer', () => ({ pushToast: vi.fn() }))
vi.mock('../workspace/openWorkspaceFolder', () => ({ openWorkspaceFolder: openWorkspaceFolderMock }))
vi.mock('../protocol/ws-outbox', () => ({
  sendClientCommand: sendClientCommandMock,
  sendClientCommandAwaitResult: vi.fn(async (command: { type: string }) => ({
    type: 'command.result', command: command.type, level: 'success', message: '', data: {},
  })),
  sendConversationDeleteCommand: vi.fn(async () => true),
  commandResultSucceeded: (event: { level?: string }) => !['error', 'failed'].includes(String(event.level || '')),
}))

import { useAppStore } from '../stores'
import { ConversationsTab } from './ConversationsTab'
import { sendClientCommandAwaitResult, sendConversationDeleteCommand } from '../protocol/ws-outbox'
import { pushToast } from '../overlays/ToastContainer'
import { handleRuntimeEvent } from '../chat/runtimeEvents'

describe('ConversationsTab project navigation', () => {
  beforeEach(() => {
    localStorage.removeItem('minicode.sidebar.conversations.state')
    sendClientCommandMock.mockClear()
    openWorkspaceFolderMock.mockReset()
    openWorkspaceFolderMock.mockResolvedValue('C:\\OpenedProject')
    vi.mocked(sendClientCommandAwaitResult).mockClear()
    useAppStore.setState({
      appMode: 'cowork',
      conversationId: 'conv-represented',
      pendingConversationSwitchId: null,
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

  it('highlights the requested conversation before the backend confirms the switch', () => {
    useAppStore.setState({ conversations: [
      ...useAppStore.getState().conversations,
      { id: 'conv-next', title: 'Next task', updatedAt: '2026-08-15T00:00:01.000Z', workspaceRoot: 'C:\\Represented' },
    ] })
    render(<ConversationsTab conversationId="conv-represented" onSetConfirmDialog={vi.fn()} />)
    const project = screen.getByRole('region', { name: '工作区 Represented' })
    const previous = within(project).getByText('Existing workspace task').closest('button')
    const next = within(project).getByText('Next task').closest('button')
    expect(previous?.getAttribute('aria-current')).toBe('page')

    act(() => useAppStore.setState({ pendingConversationSwitchId: 'conv-next' }))
    expect(previous?.hasAttribute('aria-current')).toBe(false)
    expect(next?.getAttribute('aria-current')).toBe('page')
    act(() => useAppStore.setState({ conversationHydration: { 'conv-next': { isHydrating: true } } }))
    expect(next?.closest('[data-session-row]')?.querySelector('.session-status-spinner')).toBeNull()
    expect(screen.queryByLabelText('正在恢复会话上下文')).toBeNull()
  })

  it('drives busy indicators from live ownership and settles old metadata after a runtime snapshot', () => {
    useAppStore.setState({
      conversations: [
        { id: 'conv-represented', title: 'Completed EEG', updatedAt: '2026-10-08T00:00:00Z', sessionStatus: 'running' },
        { id: 'live', title: 'Live model question', updatedAt: '2026-10-08T00:00:01Z' },
        { id: 'old-wait', title: 'Old waiting metadata', updatedAt: '2026-10-08T00:00:02Z', sessionStatus: 'waiting' },
      ],
      conversationStreaming: { 'conv-represented': false, live: true, 'old-wait': false },
    })
    const { container } = render(<ConversationsTab conversationId="conv-represented" onSetConfirmDialog={vi.fn()} />)
    const row = (title: string) => screen.getAllByText(title)[0].closest('[data-session-row="true"]')!
    expect(row('Completed EEG').querySelector('[aria-label="任务运行中"]')).toBeNull()
    expect(row('Old waiting metadata').querySelector('.mc-session-status-icon')).toBeNull()
    expect(row('Live model question').querySelector('.session-status-spinner')).toBeTruthy()

    act(() => {
      handleRuntimeEvent({ type: 'task.update', partial: true,
        session: { active_stream_conversation_ids: [] } }, 'conv-represented')
    })
    expect(container.querySelectorAll('.session-status-spinner')).toHaveLength(0)
    expect(useAppStore.getState().conversationStreaming.live).toBe(false)
    act(() => {
      handleRuntimeEvent({ type: 'session.state_changed', conversation_id: 'conv-represented', state: 'working' })
    })
    expect(row('Completed EEG').querySelector('.session-status-spinner')).toBeTruthy()
    act(() => {
      handleRuntimeEvent({ type: 'session.state_changed', conversation_id: 'conv-represented', state: 'idle' })
    })
    expect(row('Completed EEG').querySelector('.session-status-spinner')).toBeNull()
  })

  it('removes a workspace through its context menu while retaining conversations across reopen', async () => {
    const { unmount } = render(<ConversationsTab conversationId="conv-represented" onSetConfirmDialog={vi.fn()} />)
    fireEvent.contextMenu(screen.getByRole('button', { name: 'Represented', exact: true }), { clientX: 40, clientY: 80 })
    fireEvent.click(screen.getByRole('menuitem', { name: '移除工作区' }))
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: 'workspace.recent.remove', path: 'C:\\Represented', preserve_history: true,
    }, 'workspace.recent.remove'))
    const workspaces = useAppStore.getState().recentWorkspaces
    act(() => useAppStore.getState().setRecentWorkspaces(workspaces.filter(item => item.path !== 'C:\\Represented')))
    expect(screen.queryByRole('region', { name: '工作区 Represented' })).toBeNull()
    expect(useAppStore.getState().conversations[0].archived).toBeUndefined()
    expect(sendConversationDeleteCommand).not.toHaveBeenCalled()
    unmount()
    render(<ConversationsTab conversationId="conv-represented" onSetConfirmDialog={vi.fn()} />)
    expect(screen.queryByRole('region', { name: '工作区 Represented' })).toBeNull()
    expect(within(screen.getByRole('region', { name: '最近' })).getByText('Existing workspace task')).toBeTruthy()
    act(() => useAppStore.getState().setRecentWorkspaces(workspaces))
    expect(within(screen.getByRole('region', { name: '工作区 Represented' })).getByText('Existing workspace task')).toBeTruthy()
  })

  it('keeps the workspace visible if preserving history fails', async () => {
    vi.mocked(sendClientCommandAwaitResult).mockResolvedValueOnce({
      type: 'command.result', command: 'workspace.recent.remove', level: 'error', message: '无法保存历史会话',
    })
    render(<ConversationsTab conversationId="conv-represented" onSetConfirmDialog={vi.fn()} />)
    fireEvent.contextMenu(screen.getByRole('button', { name: 'Represented', exact: true }))
    fireEvent.click(screen.getByRole('menuitem', { name: '移除工作区' }))
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith('无法保存历史会话', 'error', 5000))
    expect(within(screen.getByRole('region', { name: '工作区 Represented' })).getByText('Existing workspace task')).toBeTruthy()
  })

  it('uses one project list and refreshes its saved folders after connecting', () => {
    render(<ConversationsTab conversationId="conv-represented" onSetConfirmDialog={vi.fn()} />)
    expect(screen.getByText('项目')).toBeTruthy()
    expect(within(screen.getByRole('region', { name: '工作区 Represented' })).getByText('Existing workspace task')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '选择会话' })).toBeNull()
    expect(screen.queryByRole('region', { name: '最近工作区' })).toBeNull()
    expect(screen.queryByRole('button', { name: '清空最近工作区' })).toBeNull()
    expect(screen.queryByRole('checkbox')).toBeNull()
    expect(screen.getByRole('region', { name: '最近' })).toBeTruthy()
    expect(screen.queryByRole('region', { name: '普通任务' })).toBeNull()
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

  it('labels a registered MiniCode worktree by its project while keeping its actual action path', () => {
    const worktreePath = 'C:/Desktop/MiniCode/.minicode/worktrees/conv_5c136af99610'
    useAppStore.setState({
      appMode: 'code', conversationId: 'isolated',
      conversations: [{ id: 'isolated', title: 'Isolated MiniCode task', updatedAt: '2026-10-05', workspaceRoot: worktreePath, worktreePath }],
      recentWorkspaces: [{ path: worktreePath, name: 'conv_5c136af99610', projectType: 'python', lastOpened: 1_791_202_126 }],
    })
    const original = useAppStore.getState().createConversation
    const create = vi.fn(async () => true)
    useAppStore.setState({ createConversation: create })
    try {
      render(<ConversationsTab conversationId="isolated" onSetConfirmDialog={vi.fn()} />)
      const project = screen.getByRole('region', { name: '工作区 MiniCode' })
      expect(within(project).getByRole('button', { name: 'MiniCode', exact: true })).toBeTruthy()
      expect(within(project).getByText('Isolated MiniCode task')).toBeTruthy()
      expect(screen.queryByRole('region', { name: '工作区 Computer' })).toBeNull()
      fireEvent.click(within(project).getByText('Isolated MiniCode task').closest('button')!)
      expect(useAppStore.getState().appMode).toBe('cowork')
      expect(useAppStore.getState().conversationId).toBe('isolated')
      expect(sendClientCommandMock).not.toHaveBeenCalled()
      act(() => useAppStore.setState({ appMode: 'code' }))
      fireEvent.click(within(project).getByRole('button', { name: '在 MiniCode 中新建任务' }))
      expect(create).toHaveBeenCalledExactlyOnceWith({ bindWorkspace: true, workspaceRoot: worktreePath, appMode: 'cowork' })
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
    expect(within(screen.getByRole('region', { name: '工作区 Represented' })).getByText('Existing workspace task')).toBeTruthy()
  })

  it('does not expose session deletion from the conversation menu', () => {
    const onSetConfirmDialog = vi.fn()
    render(<ConversationsTab conversationId="conv-represented" onSetConfirmDialog={onSetConfirmDialog} />)
    fireEvent.click(within(screen.getByRole('region', { name: '工作区 Represented' })).getByRole('button', { name: '会话操作' }))
    expect(screen.queryByRole('menuitem', { name: '删除' })).toBeNull()
    expect(onSetConfirmDialog).not.toHaveBeenCalled()
  })

  it('isolates duplicate project and recent rows while renaming through the canonical conversation id', async () => {
    render(<ConversationsTab conversationId="conv-represented" showRecent onSetConfirmDialog={vi.fn()} />)
    const project = screen.getByRole('region', { name: '工作区 Represented' })
    const recent = screen.getByRole('region', { name: '最近' })
    expect(within(project).getByText('Existing workspace task')).toBeTruthy()
    expect(within(recent).getByText('Existing workspace task')).toBeTruthy()
    const projectAction = within(project).getByRole('button', { name: '会话操作' })
    const recentAction = within(recent).getByRole('button', { name: '会话操作' })

    fireEvent.click(projectAction)
    expect(screen.getAllByRole('menu', { name: '会话操作' })).toHaveLength(1)
    expect(projectAction.getAttribute('aria-expanded')).toBe('true')
    expect(recentAction.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(recentAction)
    const menu = screen.getByRole('menu', { name: '会话操作' })
    expect(screen.getAllByRole('menu', { name: '会话操作' })).toHaveLength(1)
    expect(menu.id).toBe('conversation-actions-recent:conv-represented')
    expect(projectAction.getAttribute('aria-expanded')).toBe('false')
    expect(recentAction.getAttribute('aria-expanded')).toBe('true')

    fireEvent.click(within(menu).getByRole('menuitem', { name: '重命名' }))
    expect(screen.getAllByRole('textbox', { name: '重命名 Existing workspace task' })).toHaveLength(1)
    const input = within(recent).getByRole('textbox', { name: '重命名 Existing workspace task' })
    expect(within(project).queryByRole('textbox')).toBeNull()
    expect(within(project).getByText('Existing workspace task')).toBeTruthy()
    fireEvent.change(input, { target: { value: 'Renamed canonical task' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledExactlyOnceWith({
      type: 'conversation.rename', conversation_id: 'conv-represented', title: 'Renamed canonical task',
    }, 'conversation.rename'))
    expect(useAppStore.getState().conversationId).toBe('conv-represented')
  })

  it('keeps the add-project entry available on an empty sidebar and navigates after a real folder opens', async () => {
    useAppStore.setState({ conversations: [], recentWorkspaces: [] })
    const onNavigate = vi.fn()
    render(<ConversationsTab conversationId="" onNavigate={onNavigate} onSetConfirmDialog={vi.fn()} />)
    expect(screen.getByText('项目')).toBeTruthy()
    expect(screen.getByText('开始你的第一个任务')).toBeTruthy()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '添加项目' })) })
    expect(openWorkspaceFolderMock).toHaveBeenCalledExactlyOnceWith()
    expect(onNavigate).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('region', { name: '工作区 OpenedProject' })).toBeNull()

    onNavigate.mockClear()
    openWorkspaceFolderMock.mockResolvedValueOnce(null)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '添加项目' })) })
    expect(onNavigate).not.toHaveBeenCalled()
  })

  it('shows five chats per project and retains the chosen expansion through modes and activity', () => {
    useAppStore.setState({ conversations: Array.from({ length: 7 }, (_, index) => ({
      id: `project-${index}`, title: `Project chat ${index}`, updatedAt: '2026-10-05', workspaceRoot: 'C:\\Represented',
    })) })
    const props = { conversationId: 'project-0', onSetConfirmDialog: vi.fn() }
    const { container, rerender } = render(<ConversationsTab {...props} />)
    const project = () => screen.getByRole('region', { name: '工作区 Represented' })
    const rows = () => project().querySelectorAll('[data-session-row="true"]')
    expect(rows()).toHaveLength(5)
    expect(container.querySelector('.mc-workspace-session-count')).toBeNull()
    expect(within(project()).queryByText('Project chat 6')).toBeNull()
    fireEvent.click(within(project()).getByRole('button', { name: '展开显示' }))
    expect(rows()).toHaveLength(7)

    act(() => useAppStore.setState({ appMode: 'code' }))
    expect(rows()).toHaveLength(7)
    rerender(<ConversationsTab {...props} activityView />)
    expect(screen.queryByRole('region', { name: '工作区 Represented' })).toBeNull()
    rerender(<ConversationsTab {...props} />)
    expect(rows()).toHaveLength(7)
    fireEvent.click(within(project()).getByRole('button', { name: 'Represented', exact: true }))
    expect(rows()).toHaveLength(0)
    fireEvent.click(within(project()).getByRole('button', { name: 'Represented', exact: true }))
    expect(rows()).toHaveLength(7)
    fireEvent.click(within(project()).getByRole('button', { name: '收起' }))
    expect(rows()).toHaveLength(5)
    expect(within(screen.getByRole('region', { name: '最近' })).getByText('Project chat 6')).toBeTruthy()
    expect(screen.queryByRole('region', { name: '普通任务' })).toBeNull()
  })

  it('groups real activity by priority and local calendar dates with only recorded summaries', () => {
    vi.useFakeTimers()
    const now = new Date(2026, 9, 5, 12)
    vi.setSystemTime(now)
    const localDate = (day: number) => new Date(2026, 9, day, 10).toISOString()
    useAppStore.setState({ conversationStreaming: { running: true, waiting: true }, runtimeSession: {
      pending_approvals: [{ request_id: 'waiting-request', type: 'control_request', subtype: 'elicitation', conversation_id: 'waiting' }],
    }, conversations: [
      { id: 'running', title: 'Running earlier', updatedAt: localDate(1), sessionStatus: 'running' },
      { id: 'waiting', title: 'Waiting earlier', updatedAt: localDate(2), sessionStatus: 'waiting' },
      { id: 'today', title: 'Today task', updatedAt: localDate(5), summary: 'Recorded summary only' },
      { id: 'yesterday', title: 'Yesterday task', updatedAt: localDate(4) },
      { id: 'earlier', title: 'Earlier task', updatedAt: localDate(3) },
      { id: 'archived', title: 'Archived task', updatedAt: localDate(5), archived: true },
    ] })
    const { container } = render(<ConversationsTab conversationId="conv-represented" activityView onSetConfirmDialog={vi.fn()} />)
    const priority = screen.getByRole('region', { name: '优先级' })
    expect(within(priority).getByText('Running earlier')).toBeTruthy()
    expect(within(priority).getByText('Waiting earlier')).toBeTruthy()
    expect(within(priority).getByLabelText('等待回复')).toBeTruthy()
    expect(within(priority).getAllByLabelText('任务运行中')).toHaveLength(1)
    expect(within(screen.getByRole('region', { name: '今天' })).getByText('Today task')).toBeTruthy()
    expect(within(screen.getByRole('region', { name: '昨天' })).getByText('Yesterday task')).toBeTruthy()
    expect(within(screen.getByRole('region', { name: '更早' })).getByText('Earlier task')).toBeTruthy()
    expect(screen.queryByText('Archived task')).toBeNull()
    expect(container.querySelectorAll('.mc-sidebar-activity-summary')).toHaveLength(1)
    expect(screen.getByText('Recorded summary only')).toBeTruthy()
    fireEvent.click(screen.getByText('Today task').closest('button')!)
    expect(sendClientCommandMock).toHaveBeenCalledExactlyOnceWith({ type: 'conversation.switch', conversation_id: 'today' })
    expect(useAppStore.getState().conversationId).toBe('conv-represented')
  })

  it('retains the existing twenty-row more pagination in the activity view', () => {
    useAppStore.setState({ conversations: Array.from({ length: 25 }, (_, index) => ({
      id: `activity-${index}`, title: `Activity ${index}`, updatedAt: new Date().toISOString(),
    })) })
    const { container } = render(<ConversationsTab conversationId="conv-represented" activityView onSetConfirmDialog={vi.fn()} />)
    expect(container.querySelectorAll('[data-session-row="true"]')).toHaveLength(20)
    fireEvent.click(screen.getByRole('button', { name: '显示更多' }))
    expect(container.querySelectorAll('[data-session-row="true"]')).toHaveLength(25)
    expect(screen.queryByRole('button', { name: '显示更多' })).toBeNull()
  })

  it('projects producer role summaries as real plain assistant text without inventing missing previews', () => {
    useAppStore.setState({ conversations: [
      { id: 'assistant', title: 'Assistant summary', updatedAt: new Date().toISOString(),
        summary: 'User: 调研**代码**界面 | Assistant: 较早的结果 | User: 继续 | Assistant: **已完成** [检查记录](https://example.com/checks) 与 `代码`。' },
      { id: 'user-only', title: 'User summary', updatedAt: new Date().toISOString(), summary: 'User: **保留真实要求** [文档](https://example.com/docs)' },
      { id: 'plain', title: 'Saved summary', updatedAt: new Date().toISOString(), summary: '**原有摘要** [来源](https://example.com/source)' },
      { id: 'missing', title: 'No summary', updatedAt: new Date().toISOString() },
    ] })
    const { container } = render(<ConversationsTab conversationId="conv-represented" activityView onSetConfirmDialog={vi.fn()} />)
    const previews = [...container.querySelectorAll('.mc-sidebar-activity-summary')].map((node) => node.textContent)
    expect(previews).toHaveLength(3)
    expect(previews).toEqual(expect.arrayContaining(['已完成 检查记录 与 代码。', '保留真实要求 文档', '原有摘要 来源']))
    expect(previews.join(' ')).not.toMatch(/User:|Assistant:|\*\*|https:\/\//)
    expect(previews.join(' ')).not.toContain('较早的结果')
    expect(screen.getByText('No summary').closest('.mc-sidebar-activity-task')!.querySelector('.mc-sidebar-activity-summary')).toBeNull()
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
