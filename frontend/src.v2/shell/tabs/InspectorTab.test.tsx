/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const {
  fetchWorkspaceGitStatusMock,
  sendClientCommandAwaitResultMock,
  sendClientCommandMock,
} = vi.hoisted(() => ({
  fetchWorkspaceGitStatusMock: vi.fn(async () => ({
    branch: 'main',
    modified: [],
    staged: [],
    untracked: [],
  })),
  sendClientCommandAwaitResultMock: vi.fn(),
  sendClientCommandMock: vi.fn(() => true),
}))

vi.mock('../../desktop/runtime', () => ({
  isDesktop: () => false,
  revealPath: vi.fn(),
}))

vi.mock('../../hooks/useWebSocket', () => ({
  getWebSocket: () => null,
}))

vi.mock('../../protocol/workspace', () => ({
  fetchWorkspaceGitStatus: fetchWorkspaceGitStatusMock,
}))

vi.mock('../../protocol/ws-outbox', () => ({
  commandResultSucceeded: (event: { level?: string }) => !['error', 'failed'].includes(String(event.level || '').toLowerCase()),
  sendClientCommand: sendClientCommandMock,
  sendClientCommandAwaitResult: sendClientCommandAwaitResultMock,
}))

import { useAppStore } from '../../stores'
import { InspectorTab } from './InspectorTab'

const successfulPermissionResult = () => ({
  type: 'command.result',
  command: 'permissions.rules.list',
  level: 'success',
  message: 'Permission rules loaded',
  data: {
    conversation_id: 'conv-inspector',
    rules: {
      mode: 'default',
      context_source: 'conversation',
      system_deny: [],
      session_deny: [],
      session_overrides: [],
      session_prompt_rules: [],
    },
  },
})

describe('InspectorTab control-plane refresh', () => {
  beforeEach(() => {
    fetchWorkspaceGitStatusMock.mockClear()
    sendClientCommandMock.mockReset()
    sendClientCommandMock.mockReturnValue(true)
    sendClientCommandAwaitResultMock.mockReset()
    sendClientCommandAwaitResultMock.mockResolvedValue(successfulPermissionResult())
    useAppStore.setState({
      conversationId: 'conv-inspector',
      conversations: [{
        id: 'conv-inspector',
        title: 'Inspector task',
        updatedAt: '2026-08-15T00:00:00.000Z',
        workspaceRoot: 'C:\\Desktop\\MiniCode',
      }],
      workingDirectory: 'C:\\Desktop\\MiniCode',
      isConnected: false,
      workspaceGit: null,
      contextUsage: null,
      terminalSessions: [],
      activeEditorPath: null,
      messages: [],
      agentProgress: [],
      inspectorEntries: [],
      inspectorFocus: null,
      conversationHydration: {},
      permissionRulesByConversation: {},
      checkpointsByConversation: {},
      runCheckpointsByConversation: {},
      checkpointResumeByConversation: {},
      guidelineReloadsByConversation: {},
      recentWorkspaces: [],
    })
  })

  afterEach(() => cleanup())

  it('waits while offline and automatically refreshes after the websocket reconnects', async () => {
    render(<InspectorTab />)

    const refreshButton = screen.getByRole('button', { name: '刷新检查点' }) as HTMLButtonElement
    expect(refreshButton.disabled).toBe(true)
    expect(screen.getByText('后端未连接 · 重连后自动刷新')).toBeTruthy()
    expect(sendClientCommandMock).not.toHaveBeenCalled()
    expect(sendClientCommandAwaitResultMock).not.toHaveBeenCalled()

    act(() => useAppStore.setState({ isConnected: true }))

    await waitFor(() => expect(sendClientCommandMock).toHaveBeenCalledTimes(2))
    expect(sendClientCommandMock).toHaveBeenNthCalledWith(1, {
      type: 'checkpoint.list',
      conversation_id: 'conv-inspector',
      workspace_root: 'C:\\Desktop\\MiniCode',
      limit: 50,
    }, { silent: true })
    expect(sendClientCommandMock).toHaveBeenNthCalledWith(2, {
      type: 'checkpoint.run.list',
      conversation_id: 'conv-inspector',
      workspace_root: 'C:\\Desktop\\MiniCode',
    }, { silent: true })
    expect(sendClientCommandAwaitResultMock).toHaveBeenCalledWith({
      type: 'conversation.permission.rules.list',
      conversation_id: 'conv-inspector',
      source: 'frontend.inspector',
    }, 'permissions.rules.list', { silent: true })
    await waitFor(() => expect((screen.getByRole('button', { name: '刷新检查点' }) as HTMLButtonElement).disabled).toBe(false))
    expect(screen.queryByText(/刷新失败/)).toBeNull()
  })

  it('keeps automatic refresh quiet on success but presents a concrete permission error inline', async () => {
    sendClientCommandAwaitResultMock.mockResolvedValueOnce({
      type: 'command.result',
      command: 'permissions.rules.list',
      level: 'error',
      message: 'Conversation permission scope is unavailable',
      data: { conversation_id: 'conv-inspector' },
    })
    useAppStore.setState({ isConnected: true })

    render(<InspectorTab />)

    expect(await screen.findByText('刷新失败 · Conversation permission scope is unavailable')).toBeTruthy()
    expect(sendClientCommandAwaitResultMock).toHaveBeenCalledWith(expect.any(Object), 'permissions.rules.list', { silent: true })
  })

  it('shows safe Anthropic provider metadata without rendering refusal explanations', () => {
    useAppStore.setState({
      inspectorEntries: [{
        targetKind: 'provider',
        targetId: 'trace-anthropic-1',
        timestamp: 1,
        payload: {
          kind: 'provider_trace',
          provider: 'anthropic',
          model: 'claude-opus-audit',
          finish_reason: 'refusal',
          usage: {
            input_tokens: 12,
            output_tokens: 2,
            cache_read_input_tokens: 8,
            cache_creation_input_tokens: 4,
            cache_deleted_input_tokens: 3,
          },
          raw_usage: {
            service_tier: 'priority',
            inference_geo: 'us',
            cache_creation: {
              ephemeral_5m_input_tokens: 1,
              ephemeral_1h_input_tokens: 3,
            },
            server_tool_use: {
              web_search_requests: 2,
              web_fetch_requests: 1,
            },
          },
          search_sources: [{ title: 'Release', url: 'https://example.test/release' }],
          citations: [{
            source: 'anthropic:document:abc',
            title: 'Release report',
            label: 'Pages 2–3',
            range: [2, 3],
          }],
          container: {
            id: 'container-1',
            expires_at: '2026-08-16T20:00:00Z',
          },
          refusal: {
            type: 'refusal',
            category: 'cyber',
            explanation_available: true,
            explanation: 'DO_NOT_RENDER_REFUSAL_EXPLANATION',
          },
        },
      }],
      inspectorFocus: { kind: 'provider', id: 'trace-anthropic-1' },
    })

    render(<InspectorTab />)
    fireEvent.click(screen.getByRole('button', { name: /高级诊断/ }))

    expect(screen.getByText('5m 1 · 1h 3')).toBeTruthy()
    expect(screen.getByText('search 2 · fetch 1')).toBeTruthy()
    expect(screen.getByText('1 source')).toBeTruthy()
    expect(screen.getByText('1 · 1 document location')).toBeTruthy()
    expect(screen.getByText('container-1 · expires 2026-08-16T20:00:00Z')).toBeTruthy()
    expect(screen.getByText('declined · cyber')).toBeTruthy()
    expect(document.body.textContent).not.toContain('DO_NOT_RENDER_REFUSAL_EXPLANATION')
  })

  it('does not present empty optional Provider objects as real Container or refusal state', () => {
    useAppStore.setState({
      inspectorEntries: [{
        targetKind: 'provider',
        targetId: 'trace-anthropic-empty-optionals',
        timestamp: 1,
        payload: {
          kind: 'provider_trace',
          provider: 'anthropic',
          model: 'claude-opus-audit',
          container: {},
          refusal: {},
        },
      }],
      inspectorFocus: { kind: 'provider', id: 'trace-anthropic-empty-optionals' },
    })

    render(<InspectorTab />)
    fireEvent.click(screen.getByRole('button', { name: /高级诊断/ }))

    expect(screen.queryByText('Container')).toBeNull()
    expect(screen.queryByText('拒绝')).toBeNull()
    expect(screen.queryByText('declined')).toBeNull()
  })

  it('renders Provider runtime spans as events instead of empty model traces', () => {
    useAppStore.setState({
      inspectorEntries: [{
        targetKind: 'provider',
        targetId: 'provider:iter:1:1',
        timestamp: 1,
        payload: {
          type: 'runtime.span',
          span_event: 'provider.request.completed',
          status: 'completed',
          data: {
            finish_reason: 'end_turn',
            input_tokens: 29,
            output_tokens: 14,
          },
        },
      }],
      inspectorFocus: { kind: 'provider', id: 'provider:iter:1:1' },
    })

    render(<InspectorTab />)
    fireEvent.click(screen.getByRole('button', { name: /高级诊断/ }))

    expect(screen.getByText('provider.request.completed · completed · end_turn · 29 in / 14 out')).toBeTruthy()
    expect(screen.queryByText('0 in / 0 out / 0 reasoning')).toBeNull()
    expect(screen.queryByText('unknown · retention off · store n/a')).toBeNull()
    expect(document.body.textContent).toContain('"finish_reason": "end_turn"')
  })
})
