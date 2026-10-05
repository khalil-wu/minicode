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
const resource = (patch: Partial<ConversationResource> = {}): ConversationResource => ({ id: 'artifact:old', artifact_id: 'old', source: 'artifact', kind: 'file', name: 'historical.txt', media_type: 'text/plain', size_bytes: 10, occurred_at: 1, execution_result: false, message_id: 'old-producer', turn_id: 'old-turn', conversation_id: 'owner', workspace_root: '/project-a', ...patch })
const page = (items: ConversationResource[]): ConversationResourcePage => ({ conversation_id: items[0]?.conversation_id || 'owner', workspace_root: items[0]?.workspace_root || '/project-a', items, total: items.length, has_more: false, after: items.at(-1)?.id || '' })
beforeEach(() => { fetchResources.mockReset(); useAppStore.setState({ conversationId: 'owner', workingDirectory: '/project-a', isConnected: true, messages: [], previewArtifact: null, messageRevealTarget: null }) })
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
    act(() => useAppStore.setState({ workingDirectory: '/project-b' }))
    await screen.findByText('new-project.txt')
    await act(async () => oldResolve(page([resource()])))
    expect(screen.queryByText('historical.txt')).toBeNull()
    expect(screen.getByText('new-project.txt')).toBeTruthy()
  })
  it('merges realtime artifact metadata without suppressing an upload with the same identifier', () => {
    const inventory = [resource(), resource({ id: 'attachment:old', source: 'attachment', kind: 'attachment', name: 'uploaded.txt' })]
    const result = mergeResourceInventory([{ id: 'old', artifactId: 'old', kind: 'file', label: 'current.txt', conversationId: 'owner', occurredAt: 2 }], inventory)
    expect(result.map((item) => item.label)).toEqual(['current.txt', 'uploaded.txt'])
    expect(result[0].messageId).toBe('old-producer')
  })
})
