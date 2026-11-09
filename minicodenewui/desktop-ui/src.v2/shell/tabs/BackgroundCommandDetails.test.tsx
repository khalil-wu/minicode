/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BackgroundCommandDetails } from './BackgroundCommandDetails'
import { ActivityTab } from './ActivityTab'
import { useAppStore } from '../../stores'

const { read, stop } = vi.hoisted(() => {
  Object.defineProperty(globalThis, 'matchMedia', { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) })
  return { read: vi.fn(), stop: vi.fn() }
})
vi.mock('../../protocol/conversation-resources', () => ({ fetchBackgroundCommand: read, stopBackgroundCommand: stop }))
const detail = { command_id: 'owned', command: 'owned command', description: '', cwd: '/project', status: 'completed', exit_code: 0, started_at: 10, completed_at: 12, conversation_id: 'owner', cleanup_pending: false, cleanup_reason: '', cleanup_error: {}, managed: true, output: 'first ', next_cursor: 6, has_more: true }
beforeEach(() => { read.mockReset(); stop.mockReset(); useAppStore.setState({ conversationId: 'owner', isConnected: true, conversations: [{ id: 'owner', title: 'Task', createdAt: '', updatedAt: '', archived: false }], messages: [], backgroundTasks: [], todos: [], plan: null }) })
afterEach(cleanup)

describe('background command details', () => {
  it('continues output from the returned cursor and shows the real terminal exit status', async () => {
    read.mockResolvedValueOnce(detail).mockResolvedValueOnce({ ...detail, output: 'second', next_cursor: 12, has_more: false })
    render(<BackgroundCommandDetails conversationId="owner" commandId="owned" onBack={() => {}} />)
    await waitFor(() => expect(screen.getByLabelText('后台进程日志').textContent).toBe('first second'))
    expect(read).toHaveBeenNthCalledWith(2, 'owner', 'owned', 6, expect.any(AbortSignal))
    expect(screen.getAllByText('已完成').length).toBeGreaterThan(0)
    expect(screen.getByText('0')).toBeTruthy()
  })
  it('stops the selected command and displays cancellation without changing another command', async () => {
    read.mockResolvedValueOnce({ ...detail, status: 'running', exit_code: null, completed_at: null, has_more: false })
      .mockResolvedValue({ ...detail, status: 'cancelled', exit_code: -1, has_more: false })
    stop.mockResolvedValue({ ...detail, status: 'cancelled', exit_code: -1, has_more: false, stopped: true })
    render(<BackgroundCommandDetails conversationId="owner" commandId="owned" onBack={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: '停止进程' }))
    expect(stop).toHaveBeenCalledWith('owner', 'owned')
    await waitFor(() => expect(screen.getAllByText('已停止').length).toBeGreaterThan(0))
    expect(screen.queryByRole('button', { name: '停止进程' })).toBeNull()
  })
  it('opens the background row using its raw command identity rather than its display id', async () => {
    read.mockResolvedValue({ ...detail, has_more: false })
    useAppStore.setState({ backgroundTasks: [{ id: 'owned', conversationId: 'owner', command: 'owned command', status: 'completed', timestamp: 10 }] })
    render(<ActivityTab />)
    fireEvent.click(screen.getByText('owned command'))
    await screen.findByLabelText('后台进程详情')
    expect(read).toHaveBeenCalledWith('owner', 'owned', 0, expect.any(AbortSignal))
  })
})
