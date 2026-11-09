// In-memory state for the demo backend. Everything here is fake and lives only
// for the lifetime of the page.

export type Json = Record<string, unknown>;

export const DEMO_SESSION_ID = "session_demo";
export const DEMO_WORKSPACE = "/Users/demo/Projects/aurora";
export const DEMO_WORKSPACE_NAME = "aurora";
export const DEMO_BRANCH = "main";

export const DEMO_MODELS = [
  { model: "claude-opus-5", label: "Claude Opus 5", efforts: ["low", "medium", "high", "max"], effort: "high" },
  { model: "claude-sonnet-5", label: "Claude Sonnet 5", efforts: ["low", "medium", "high"], effort: "medium" },
  { model: "claude-haiku-4-5", label: "Claude Haiku 4.5", efforts: ["low", "medium"], effort: "low" },
] as const;

export const state = {
  model: DEMO_MODELS[0].model as string,
  effort: DEMO_MODELS[0].effort as string,
  permissionMode: "confirm",
  activeConversationId: null as string | null,
  conversations: new Map<string, DemoConversation>(),
};

export interface DemoConversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  workspace_root: string;
  transcript: Json[];
  pinned?: boolean;
}

export const isoAgo = (minutes: number): string => new Date(Date.now() - minutes * 60_000).toISOString();

export const conversationSummary = (conversation: DemoConversation): Json => ({
  id: conversation.id,
  title: conversation.title,
  created_at: conversation.created_at,
  updated_at: conversation.updated_at,
  conversation_type: "main",
  memory_mode: "enabled",
  permission_mode: state.permissionMode,
  summary: "",
  compaction_state: "clean",
  compaction_summary: "",
  archived_at: "",
  archived: false,
  workspace_root: conversation.workspace_root,
  git_branch: DEMO_BRANCH,
  worktree_path: "",
  revision: conversation.transcript.length + 1,
  message_count: conversation.transcript.length,
  memory_polluted: false,
  git_isolated: false,
  memory_pollution_sources: [],
  permission_overrides: {},
  goal: {},
  parent_message_index: null,
  ...(conversation.pinned ? { pinned: true } : {}),
});

export const conversationSnapshot = (conversation: DemoConversation): Json => ({
  ...conversationSummary(conversation),
  transcript: conversation.transcript,
  transcript_page: { before_message_id: "", has_more: false, total_messages: conversation.transcript.length },
  context_snapshot: {},
});

export const sessionSnapshot = (): Json => ({
  session_id: DEMO_SESSION_ID,
  parent_session_id: null,
  active_conversation_id: state.activeConversationId,
  active_stream_conversation_ids: [],
  selected_model: state.model,
  workspace_root: DEMO_WORKSPACE,
});

export const sortedConversations = (): DemoConversation[] =>
  [...state.conversations.values()].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
