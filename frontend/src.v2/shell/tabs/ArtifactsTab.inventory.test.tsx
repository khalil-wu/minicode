/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAppStore } from '../../stores'
import { ArtifactsTab, mergeResourceInventory } from './ArtifactsTab'
import type { ConversationResource, ConversationResourcePage } from '../../protocol/conversation-resources'

const { fetchResources } = vi.hoisted(() => {
  Object.defineProperty(globalThis, 'matchMedia', { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) })
  return { fetchResources: vi.fn() }
})
vi.mock('../../protocol/conversation-resources', () => ({ fetchConversationResources: fetchResources }))
vi.mock('./ConversationFilesTree', () => ({ ConversationFilesTree: () => <div role="tree" aria-label="文件" /> }))
const resource = (patch: Partial<ConversationResource> = {}): ConversationResource => ({ id: 'artifact:old', artifact_id: 'old', source: 'artifact', kind: 'file', name: 'historical.txt', media_type: 'text/plain', size_bytes: 10, occurred_at: 1, execution_result: false, message_id: 'old-producer', turn_id: 'old-turn', conversation_id: 'owner', workspace_root: '/project-a', ...patch })
const page = (items: ConversationResource[]): ConversationResourcePage => ({ conversation_id: items[0]?.conversation_id || 'owner', workspace_root: items[0]?.workspace_root || '/project-a', items, total: items.length, has_more: false, after: items.at(-1)?.id || '' })
beforeEach(() => { fetchResources.mockReset(); useAppStore.setState({ conversationId: 'owner', conversations: [{ id: 'owner', title: 'Owner', updatedAt: '', executionCwd: '/project-a' }], pendingConversationSwitchId: null, workingDirectory: '/mutable-editor-root', isConnected: true, messages: [], previewArtifact: null, messageRevealTarget: null }) })
afterEach(cleanup)

describe('persisted resource inventory', () => {
  it('loads an old resource before its transcript page and keeps the actual producing-message owner', async () => {
    fetchResources.mockResolvedValue(page([resource()]))
    render(<ArtifactsTab />)
    await screen.findByText('historical.txt')
    fireEvent.click(screen.getByRole('button', { name: '回到对话' }))
    expect(useAppStore.getState().messageRevealTarget).toMatchObject({ conversationId: 'owner', messageId: 'old-producer' })
    expect(fetchResources).toHaveBeenCalledWith('owner', expect.objectContaining({ workspaceRoot: '/project-a' }))
  })
  it('discards a late response after the same conversation moves to a different workspace', async () => {
    let oldResolve!: (value: ConversationResourcePage) => void
    fetchResources.mockImplementationOnce(() => new Promise<ConversationResourcePage>((resolve) => { oldResolve = resolve }))
    fetchResources.mockResolvedValue(page([resource({ id: 'artifact:new', artifact_id: 'new', name: 'new-project.txt', workspace_root: '/project-b' })]))
    render(<ArtifactsTab />)
    await waitFor(() => expect(fetchResources).toHaveBeenCalledTimes(1))
    act(() => useAppStore.setState({ conversations: [{ id: 'owner', title: 'Owner', updatedAt: '', executionCwd: '/project-b' }] }))
    await screen.findByText('new-project.txt')
    await act(async () => oldResolve(page([resource()])))
    expect(screen.queryByText('historical.txt')).toBeNull()
    expect(screen.getByText('new-project.txt')).toBeTruthy()
  })
  it('keeps healthy resources visible alongside damaged-store diagnostics', async () => {
    fetchResources.mockResolvedValue({ ...page([resource()]), read_errors: [{ artifact_id: '', error_type: 'JSONDecodeError', message: '附件记录无法读取；部分资源可能未列出。' }] })
    render(<ArtifactsTab />)
    await screen.findByText('historical.txt')
    expect(screen.getByRole('alert').textContent).toContain('附件记录无法读取')
  })
  it('does not apply a late paginated diagnostic to the next workspace', async () => {
    let resolveOld!: (value: ConversationResourcePage) => void
    fetchResources.mockResolvedValueOnce({ ...page([resource()]), has_more: true })
    fetchResources.mockImplementationOnce(() => new Promise<ConversationResourcePage>((resolve) => { resolveOld = resolve }))
    fetchResources.mockResolvedValue(page([resource({ name: 'next.txt', workspace_root: '/project-b' })]))
    render(<ArtifactsTab />)
    fireEvent.click(await screen.findByRole('button', { name: '读取更多历史资源' }))
    await waitFor(() => expect(fetchResources).toHaveBeenCalledTimes(2))
    act(() => useAppStore.setState({ conversations: [{ id: 'owner', title: 'Owner', updatedAt: '', executionCwd: '/project-b' }] }))
    await screen.findByText('next.txt')
    await act(async () => resolveOld({ ...page([]), read_errors: [{ artifact_id: '', error_type: 'OSError', message: 'old workspace damage' }] }))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByText('next.txt')).toBeTruthy()
  })
  it('merges realtime artifact metadata without suppressing an upload with the same identifier', () => {
    const inventory = [resource(), resource({ id: 'attachment:old', source: 'attachment', kind: 'attachment', name: 'uploaded.txt' })]
    const result = mergeResourceInventory([{ id: 'old', artifactId: 'old', kind: 'file', label: 'current.txt', conversationId: 'owner', occurredAt: 2 }], inventory)
    expect(result.map((item) => item.label)).toEqual(['current.txt', 'uploaded.txt'])
    expect(result[0].messageId).toBe('old-producer')
  })
  it('does not change resource ownership when the editor switches its working directory', async () => {
    fetchResources.mockResolvedValue(page([resource()]))
    render(<ArtifactsTab />)
    await screen.findByText('historical.txt')
    act(() => useAppStore.setState({ workingDirectory: '/another-editor-root' }))
    expect(fetchResources).toHaveBeenCalledTimes(1)
    expect(fetchResources).toHaveBeenCalledWith('owner', expect.objectContaining({ workspaceRoot: '/project-a' }))
  })
})
