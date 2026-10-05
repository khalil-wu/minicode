import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from './api'
import { getWebSocket } from '../hooks/useWebSocket'

export interface ConversationResource {
  id: string
  artifact_id?: string
  source: 'artifact' | 'attachment' | 'workspace'
  kind: string
  name: string
  path?: string
  media_type: string
  size_bytes: number
  occurred_at: number
  execution_result: boolean
  message_id?: string
  turn_id?: string
  conversation_id: string
  workspace_root: string
}

export interface ConversationResourcePage {
  conversation_id: string
  workspace_root: string
  items: ConversationResource[]
  total: number
  has_more: boolean
  after: string
}

export interface BackgroundCommandDetail {
  command_id: string
  command: string
  description: string
  cwd: string
  status: 'running' | 'stalled' | 'completed' | 'failed' | 'cancelled' | 'unknown'
  exit_code: number | null
  started_at: number
  completed_at: number | null
  conversation_id: string
  cleanup_pending: boolean
  cleanup_reason: string
  cleanup_error: Record<string, unknown>
  managed: boolean
  output: string
  next_cursor: number
  has_more: boolean
  stopped?: boolean
}

const resourceUrl = (conversationId: string, path: string): URL => {
  const sessionId = getWebSocket()?.sessionId
  if (!sessionId) throw new Error('连接后端后可读取历史资源与后台进程。')
  const url = new URL(`${apiBase()}/api/conversations/${encodeURIComponent(conversationId)}/${path}`)
  url.searchParams.set('session_id', sessionId)
  return url
}

export const fetchConversationResources = async (conversationId: string, options: { after?: string; query?: string; kind?: string; workspaceRoot?: string; signal?: AbortSignal } = {}): Promise<ConversationResourcePage> => {
  const url = resourceUrl(conversationId, 'resources')
  url.searchParams.set('limit', '60')
  if (options.after) url.searchParams.set('after', options.after)
  if (options.query) url.searchParams.set('query', options.query)
  if (options.kind) url.searchParams.set('kind', options.kind)
  if (options.workspaceRoot !== undefined) url.searchParams.set('workspace_root', options.workspaceRoot)
  const response = await fetchWithTimeout(url, { headers: authHeaders(), signal: options.signal })
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText))
  return response.json() as Promise<ConversationResourcePage>
}

export const fetchBackgroundCommand = async (conversationId: string, commandId: string, cursor: number, signal?: AbortSignal): Promise<BackgroundCommandDetail> => {
  const url = resourceUrl(conversationId, `background-commands/${encodeURIComponent(commandId)}`)
  url.searchParams.set('cursor', String(cursor))
  const response = await fetchWithTimeout(url, { headers: authHeaders(), signal })
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText))
  return response.json() as Promise<BackgroundCommandDetail>
}

export const stopBackgroundCommand = async (conversationId: string, commandId: string): Promise<BackgroundCommandDetail> => {
  const url = resourceUrl(conversationId, `background-commands/${encodeURIComponent(commandId)}/stop`)
  const response = await fetchWithTimeout(url, { method: 'POST', headers: authHeaders() })
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText))
  return response.json() as Promise<BackgroundCommandDetail>
}
