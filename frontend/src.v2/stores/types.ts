import type { GoalInfo, PlanStep, RuntimeSessionSnapshot } from "../protocol/events";
import type { ToolCallRecord } from "../lib/tool-call-reducer";

// ── UI Slice ──────────────────────────────────────────────────────

export type ThemeMode = "system" | "dark" | "light";
export type ViewMode = "normal" | "verbose" | "summary";
export type AppMode = "chat" | "code" | "cowork";
export type SessionFilter = "all" | "running" | "waiting" | "idle" | "archived";
export type SessionGroupBy = "none" | "project" | "branch";
export type RightStackTab = "preview" | "browser" | "terminal" | "tasks" | "diff" | "plan" | "subagents" | "inspector" | "diagnostics";
export type EffortLevel = "low" | "medium" | "high" | "max";

export interface SkillInfo {
  name: string;
  description: string;
  version?: string;
  triggers?: string[];
  tools_required?: string[];
  source_level?: string;
  active?: boolean;
}

export interface MarketplaceSkill {
  name: string;
  title: string;
  description: string;
  triggers: string[];
  installed: boolean;
}

export interface SlashCommandArg {
  value: string;
  description: string;
}

export interface SlashCommand {
  name: string;
  command: string;
  label: string;
  description: string;
  type: "local" | "template" | "protocol";
  /** Structured argument options for local commands (second-stage menu). */
  args?: SlashCommandArg[];
}

export interface QuickOpenResult {
  path: string;
  name: string;
}

export interface ArtifactContentState {
  artifactId: string;
  content: string;
  preview?: string;
  name?: string;
  mediaType?: string;
  url?: string;
  loadedAt: number;
}

export type McpLifecyclePhase =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "auth_required"
  | "expired"
  | "failed"
  | "stopped";

export interface McpServerProgress {
  operation: string;
  message?: string;
  progress?: number;
  status: "running" | "completed" | "failed";
}

export interface McpServerStatus {
  name: string;
  status: "connected" | "disconnected" | "error" | "reconnecting" | "starting" | "offline";
  tools?: number;
  transport?: "stdio" | "http" | "sse" | "streamable-http";
  command?: string;
  url?: string;
  lastError?: string;
  phase?: McpLifecyclePhase;
  recoverable?: boolean;
  requiresUserAction?: boolean;
  setupHint?: string;
  docsUrl?: string;
  progress?: McpServerProgress;
}

export interface PreviewServerInfo {
  port: number;
  url: string;
  name: string;
  framework?: string;
}

export interface PreviewLaunchConfigInfo {
  name: string;
  command: string;
  cwd: string;
  port: number;
  url: string;
  auto_port?: boolean;
  source?: string;
}

export interface PreviewServerOutputLine {
  stream: "stdout" | "stderr";
  line: string;
  timestamp?: number;
}

export interface PreviewLaunchProcessInfo extends PreviewLaunchConfigInfo {
  id: string;
  pid?: number;
  status: "starting" | "running" | "ready" | "exited" | "crashed" | "unhealthy";
  stderr_tail?: string[];
  output_tail?: PreviewServerOutputLine[];
}

export interface PreviewVerificationInfo {
  url: string;
  ok: boolean;
  status_code?: number | null;
  elapsed_ms: number;
  error?: string;
  checkedAt: number;
}

export interface WorkspaceGitState {
  branch: string;
  isWorktree: boolean;
  currentPath: string;
  mainRepoPath?: string | null;
  worktreeCount?: number;
  isolatedCount?: number;
  error?: string;
}

export interface DiffReviewFile {
  path: string;
  patch?: string | null;
  additions?: number;
  deletions?: number;
  isLarge?: boolean;
  isTruncated?: boolean;
  decision?: "approved" | "rejected" | "pending";
}

export interface DiffReviewState {
  requestId: string;
  protocol?: "legacy" | "control";
  conversationId?: string;
  toolName?: string;
  diff: string;
  files: DiffReviewFile[];
  selectedPath?: string;
  status: "pending" | "submitted" | "approved" | "rejected" | "error" | "viewing";
  mode?: "approval" | "view";
  error?: string;
  fileDecisions: Record<string, "approved" | "rejected">;
  lineComments?: DiffLineComment[];
}

export interface DiffLineComment {
  filePath: string;
  lineIndex: number;
  content: string;
}

export interface GitChangeFile {
  path: string;
  patch?: string;
  additions: number;
  deletions: number;
  isBinary?: boolean;
}

export interface GitChangesState {
  workingTree: GitChangeFile[];
  staged: GitChangeFile[];
  untracked: string[];
  loading: boolean;
}

export interface ContextUsage {
  used: number;
  limit: number;
  compactedAt?: number;
  compactSummary?: string;
}

export interface UISlice {
  themeMode: ThemeMode;
  textScale: number;
  viewMode: ViewMode;
  appMode: AppMode;
  rightStackTab: RightStackTab;
  rightStackTabLocked: boolean;
  contextUsage: ContextUsage | null;
  commandPaletteOpen: boolean;
  settingsOpen: boolean;
  shortcutsHelpOpen: boolean;
  quickOpenVisible: boolean;
  quickOpenResults: QuickOpenResult[];
  quickOpenLoading: boolean;
  currentModel: string;
  currentProvider: string;
  availableModels: string[];
  availableSkills: SkillInfo[];
  marketplaceSkills: MarketplaceSkill[];
  slashCommands: SlashCommand[];
  workingDirectory: string;
  workspaceGit: WorkspaceGitState | null;
  diffReview: DiffReviewState | null;
  previewArtifact: ArtifactContentState | null;
  livePreviewUrl: string | null;
  previewServers: PreviewServerInfo[];
  previewLaunchConfigs: PreviewLaunchConfigInfo[];
  previewLaunchProcesses: PreviewLaunchProcessInfo[];
  previewVerification: PreviewVerificationInfo | null;
  fileChanges: { path: string; event: string; timestamp: number }[];
  fileTreeVersion: number;
  mcpServers: McpServerStatus[];
  envVars: { name: string; description: string; scope: string }[];
  gitChanges: GitChangesState;
  skillsMarketplaceOpen: boolean;
  liveArtifactsOpen: boolean;
  setThemeMode: (m: ThemeMode) => void;
  setTextScale: (s: number) => void;
  setViewMode: (m: ViewMode) => void;
  setAppMode: (m: AppMode) => void;
  ensureCodeLayout: () => void;
  setRightStackTab: (t: RightStackTab, options?: { automatic?: boolean }) => void;
  setRightStackTabLocked: (locked: boolean) => void;
  setContextUsage: (u: ContextUsage | null) => void;
  toggleCommandPalette: () => void;
  toggleSettings: () => void;
  toggleShortcutsHelp: () => void;
  toggleQuickOpen: () => void;
  toggleSkillsMarketplace: () => void;
  toggleLiveArtifacts: () => void;
  setCurrentModel: (m: string) => void;
  setCurrentProvider: (p: string) => void;
  setAvailableModels: (models: string[]) => void;
  setAvailableSkills: (skills: SkillInfo[]) => void;
  setMarketplaceSkills: (skills: MarketplaceSkill[]) => void;
  setSlashCommands: (cmds: SlashCommand[]) => void;
  setWorkingDirectory: (d: string) => void;
  setWorkspaceGit: (state: WorkspaceGitState | null) => void;
  setDiffReviewState: (state: DiffReviewState | null) => void;
  updateDiffReviewFile: (path: string, patch: Partial<DiffReviewFile>) => void;
  setDiffReviewSelectedPath: (path: string | undefined) => void;
  setDiffFileDecision: (path: string, decision: "approved" | "rejected") => void;
  addDiffLineComment: (comment: DiffLineComment) => void;
  removeDiffLineComment: (filePath: string, lineIndex: number) => void;
  submitDiffReviewWithComments: () => void;
  submitPartialApproval: () => void;
  setPreviewArtifact: (artifact: ArtifactContentState | null) => void;
  setLivePreviewUrl: (url: string | null) => void;
  openLivePreview: (url: string) => void;
  setPreviewServers: (servers: PreviewServerInfo[]) => void;
  addPreviewServer: (server: PreviewServerInfo) => void;
  removePreviewServer: (port: number) => void;
  setPreviewLaunchConfigs: (configs: PreviewLaunchConfigInfo[]) => void;
  setPreviewLaunchProcesses: (processes: PreviewLaunchProcessInfo[]) => void;
  upsertPreviewLaunchProcess: (process: PreviewLaunchProcessInfo) => void;
  removePreviewLaunchProcess: (id: string) => void;
  setPreviewVerification: (verification: PreviewVerificationInfo | null) => void;
  setQuickOpenResults: (results: QuickOpenResult[]) => void;
  setQuickOpenLoading: (loading: boolean) => void;
  addFileChange: (change: { path: string; event: string; timestamp: number }) => void;
  bumpFileTreeVersion: () => void;
  setMcpServers: (servers: McpServerStatus[]) => void;
  setEnvVars: (entries: { name: string; description: string; scope: string }[]) => void;
  setGitChanges: (changes: Partial<GitChangesState>) => void;
  setGitChangesLoading: (loading: boolean) => void;
  requestGitChanges: () => void;
}

// ── Workspace Slice ───────────────────────────────────────────────

export type PanelKind =
  | "chat"
  | "diff"
  | "editor"
  | "preview"
  | "terminal"
  | "plan"
  | "tasks"
  | "subagents"
  | "inspector";

export type LegacyPanelKind = "subagent";

export interface PanelSlot {
  id: string;
  kind: PanelKind;
  label?: string;
  size?: number;
  focused?: boolean;
  maximized?: boolean;
}

export interface TerminalSessionInfo {
  id: string;
  pid?: number;
  shell: string;
  cwd: string;
  status?: "running" | "exited";
  createdAt?: number;
}

export interface TerminalSnapshotInfo {
  id: string;
  pid?: number | null;
  shell: string;
  cwd: string;
  status?: "running" | "exited";
  output: string;
  outputChars?: number;
  totalOutputChars?: number;
  truncated?: boolean;
  capturedAt: number;
  error?: string;
}

export interface EditorOpenRequest {
  id: string;
  path: string;
  line?: number;
  column?: number;
}

export interface BackgroundTaskEntry {
  id: string;
  command: string;
  status: "running" | "completed" | "failed";
  exitCode?: number;
  duration?: number;
  timestamp: number;
}

export interface PrStatus {
  number: number;
  title: string;
  state: string;
  url: string;
  branch: string;
}

export interface CiCheck {
  name: string;
  status: string;
  url: string;
}

export interface ScheduledTaskEntry {
  id: string;
  name: string;
  prompt: string;
  schedule: string;
  permission_mode: string;
  enabled: boolean;
  last_run_at?: string | null;
  created_at?: string;
}

export interface MarketplaceConnector {
  name: string;
  title: string;
  description: string;
  transport: string;
  command?: string;
  args?: string[];
  url?: string;
  tags?: string[];
  installed: boolean;
  auth?: "none" | "token" | "oauth" | "local_app" | string;
  requiresUserAction?: boolean;
  setupHint?: string;
  docsUrl?: string;
  autoStart?: boolean;
  maxRetries?: number;
}

export interface WorkspaceSlice {
  leftSidebarWidth: number;
  rightSidebarWidth: number;
  rightPanelOpen: boolean;
  dockHeight: number;
  dockCollapsed: boolean;
  activeBottomTab: "terminal" | "git" | "tasks" | "timeline" | "debug" | "budget";
  panelSlots: PanelSlot[];
  sideChatOpen: boolean;
  terminalSessions: TerminalSessionInfo[];
  terminalSnapshots: Record<string, TerminalSnapshotInfo>;
  backgroundTasks: BackgroundTaskEntry[];
  activeTerminalSessionId: string | null;
  editorOpenRequests: EditorOpenRequest[];
  activeEditorPath: string | null;
  setLeftSidebarWidth: (w: number) => void;
  setRightSidebarWidth: (w: number) => void;
  toggleRightPanel: () => void;
  setDockHeight: (h: number) => void;
  toggleDock: () => void;
  setActiveBottomTab: (t: WorkspaceSlice["activeBottomTab"]) => void;
  addPanel: (slot: PanelSlot | (Omit<PanelSlot, "kind"> & { kind: LegacyPanelKind })) => void;
  removePanel: (id: string) => void;
  focusPanel: (id: string) => void;
  movePanel: (id: string, direction: -1 | 1) => void;
  reorderPanels: (fromIndex: number, toIndex: number) => void;
  resizePanel: (id: string, delta: number) => void;
  togglePanelMaximized: (id: string) => void;
  resetPanelLayout: () => void;
  setTerminalSessions: (sessions: TerminalSessionInfo[]) => void;
  upsertTerminalSession: (session: TerminalSessionInfo) => void;
  upsertTerminalSnapshot: (snapshot: TerminalSnapshotInfo) => void;
  removeTerminalSession: (id: string) => void;
  setActiveTerminalSession: (id: string | null) => void;
  openEditorFile: (path: string, label?: string, target?: { line?: number; column?: number }) => void;
  consumeEditorOpenRequest: (path: string) => void;
  toggleSideChat: () => void;
  addBackgroundTask: (task: BackgroundTaskEntry) => void;
  prStatus: PrStatus | null;
  ciChecks: CiCheck[];
  setPrStatus: (pr: PrStatus | null, checks: CiCheck[]) => void;
  scheduledTasks: ScheduledTaskEntry[];
  setScheduledTasks: (tasks: ScheduledTaskEntry[]) => void;
  marketplaceConnectors: MarketplaceConnector[];
  setMarketplaceConnectors: (connectors: MarketplaceConnector[]) => void;
}

// ── Chat Slice ────────────────────────────────────────────────────

export type MessageRole = "user" | "assistant" | "system";

export interface ArtifactPreview {
  artifactId: string;
  kind: "file" | "diff" | "image" | "json" | "code" | "text";
  summary: string;
  bytes?: number;
  mediaType?: string;
  url?: string;
}

export interface Citation {
  source: string;
  range: [number, number];
  label?: string;
  url?: string;
  title?: string;
}

export interface MessageUsage {
  input: number;
  output: number;
  cacheRead?: number;
  cacheWrite?: number;
}

export interface FileContextRef {
  kind: "file" | "folder" | "url";
  name: string;
  path: string;
}

export interface SkillContextRef {
  kind: "skill";
  name: string;
  description?: string;
  sourceLevel?: string;
}

export type MessageContextRef = FileContextRef | SkillContextRef;

export interface MessageAttachmentRef {
  id: string;
  name: string;
  kind: "image" | "document" | "file";
  mediaType: string;
  sizeBytes: number;
  artifactId?: string;
  docId?: string;
  indexedChunks?: number;
}

export interface ThinkingContentBlock {
  type: "thinking";
  content: string;
  source?: "provider" | "model_preamble" | "runtime" | string;
  visibility?: "debug" | "timeline" | "compact" | string;
  is_raw_provider_reasoning?: boolean;
}
export interface TextContentBlock { type: "text"; content: string; }
export interface ToolCallContentBlock { type: "tool_call"; record: ToolCallRecord; }
export interface ProgressContentBlock {
  type: "progress";
  id: string;
  stage: "status" | "planning" | "tool" | "approval" | "verification" | "final";
  phase?: "orienting" | "planning" | "model" | "tool" | "approval" | "verify" | "final" | "recover" | "status" | "iteration";
  status: "running" | "completed" | "failed" | "info";
  message: string;
  label?: string;
  summary?: string;
  visibility?: "timeline" | "compact" | "debug";
  detail?: string;
  toolCallId?: string;
  toolName?: string;
  groupId?: string;
  stepId?: string;
  count?: number;
  iterationId?: string;
  timestamp: number;
}
export interface AgentProgressEntry extends ProgressContentBlock {
  conversationId?: string;
}
export type ContentBlock = ThinkingContentBlock | TextContentBlock | ToolCallContentBlock | ProgressContentBlock;

export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  contextRefs?: MessageContextRef[];
  attachmentRefs?: MessageAttachmentRef[];
  blocks?: ContentBlock[];
  artifacts: ArtifactPreview[];
  citations?: Citation[];
  usage?: MessageUsage;
  timestamp: number;
  isStreaming?: boolean;
  isThinkingStreaming?: boolean;
  resumeState?: "resumed";
  terminalStatus?: "completed" | "failed" | "interrupted";
}

export interface ConversationMeta {
  id: string;
  title: string;
  updatedAt: string;
  archived?: boolean;
  workspaceRoot?: string;
  gitBranch?: string;
  worktreePath?: string;
  gitIsolated?: boolean;
  sessionStatus?: "running" | "waiting" | "idle";
  messageCount?: number;
  dispatchBadge?: boolean;
  environment?: "local" | "remote" | "ssh";
  goal?: ConversationGoal | null;
}

export interface ConversationGoal {
  id?: string;
  text: string;
  status: "active" | "paused" | string;
  createdAt?: string;
  updatedAt?: string;
  source?: string;
}

export const toConversationGoal = (goal: GoalInfo | null | undefined): ConversationGoal | null => {
  const text = String(goal?.text || "").trim();
  if (!text) return null;
  return {
    id: goal?.id,
    text,
    status: goal?.status || "active",
    createdAt: goal?.created_at,
    updatedAt: goal?.updated_at,
    source: goal?.source,
  };
};

export interface PRMonitorState {
  prUrl: string;
  prNumber: number;
  ciStatus: "pending" | "running" | "passed" | "failed";
  autoFix: boolean;
  autoMerge: boolean;
  lastCheckedAt: number;
  checksCount?: number;
  failedChecks?: string[];
}

export interface SideChatThread {
  id: string;
  messages: ChatMessage[];
  isStreaming: boolean;
  draft: string;
  inheritedContext?: string;
}

export interface ChatSlice {
  conversationId: string | null;
  conversations: ConversationMeta[];
  activeGoal: ConversationGoal | null;
  messages: ChatMessage[];
  conversationMessages: Record<string, ChatMessage[]>;
  conversationStreaming: Record<string, boolean>;
  isStreaming: boolean;
  isPaused: boolean;
  isConnected: boolean;
  lastUsage: { input: number; output: number; cacheRead: number; cacheWrite: number } | null;
  sideChats: Record<string, SideChatThread>;
  toolCallCount: number;
  sendMessage: (content: string, options?: { assistant?: boolean; contextRefs?: MessageContextRef[]; attachmentRefs?: MessageAttachmentRef[] }) => void;
  deleteMessage: (id: string) => void;
  upsertSystemMessage: (id: string, content: string, options?: { conversationId?: string; replacePrefix?: string }) => void;
  recallMessage: (id: string) => void;
  removeEmptyStreamingAssistant: (conversationId?: string) => void;
  interrupt: () => void;
  pauseStreaming: () => void;
  resumeStreaming: () => void;
  requestConversationSwitch: (id: string) => void;
  applyConversationSwitched: (payload: { conversationId: string }) => void;
  switchConversation: (id: string) => void;
  createConversation: (options?: { bindWorkspace?: boolean; workspaceRoot?: string }) => void;
  removeConversation: (id: string) => void;
  getVisibleMessages: (conversationId?: string | null) => ChatMessage[];
  setActiveGoal: (goal: ConversationGoal | null, conversationId?: string) => void;
  hydrateConversationMessages: (id: string, messages: ChatMessage[], options?: { activate?: boolean; isStreaming?: boolean }) => void;
  appendTextChunk: (content: string, conversationId?: string) => void;
  setFinalAnswerStreaming: (conversationId: string | undefined, isStreaming: boolean) => void;
  appendThinkingChunk: (
    content: string,
    conversationId?: string,
    metadata?: Partial<Omit<ThinkingContentBlock, "type" | "content">>,
  ) => void;
  appendProgress: (progress: Omit<ProgressContentBlock, "type" | "timestamp">, conversationId?: string) => void;
  appendToolCallBlock: (tc: ToolCallRecord, conversationId?: string) => void;
  updateToolCall: (
    id: string,
    patch: Partial<ToolCallRecord>,
    conversationId?: string,
    scope?: { iterationId?: string; stepId?: string },
  ) => void;
  finishStreaming: (conversationId?: string, usage?: MessageUsage, terminalStatus?: "completed" | "failed" | "interrupted") => void;
  resumeStreaming: (conversationId: string, toolCallsPending?: PendingToolCallResume[]) => void;
  replaceStreamingText: (conversationId: string, fullText: string) => void;
  setConnected: (c: boolean) => void;
  setLastUsage: (u: ChatSlice["lastUsage"]) => void;
  ensureSideChat: (id: string) => void;
  removeSideChat: (id: string) => void;
  setSideChatDraft: (id: string, draft: string) => void;
  startSideChatMessage: (id: string, content: string) => void;
}

export type PendingToolCallResume = {
  id: string;
  name: string;
  args: Record<string, unknown>;
  status?: ToolCallRecord["status"];
  started_at?: number;
  startedAt?: number;
  display_hint?: string;
  displayHint?: string;
  input_summary?: string;
  inputSummary?: string;
  iteration_id?: string;
  iterationId?: string;
  phase?: string;
};

// ── Composer Slice ────────────────────────────────────────────────

export type PermissionMode = "ask_permissions" | "auto" | "bypass";

export interface ComposerAttachment {
  id: string;
  name: string;
  type: string;
  size: number;
  status: "uploading" | "ready" | "error";
  artifactId?: string;
  docId?: string;
  indexedChunks?: number;
  attachment?: Record<string, unknown>;
  dataUrl?: string;
  error?: string;
}

export interface ComposerSlice {
  draft: string;
  attachments: ComposerAttachment[];
  permissionMode: PermissionMode;
  effortLevel: EffortLevel;
  prMonitor: PRMonitorState | null;
  actionChip: { label: string; description?: string } | null;
  mentionResults: { path: string; name: string; kind: "file" | "folder"; score?: number }[];
  selectedMentions: { path: string; name: string; kind: "file" | "folder" | "url" }[];
  selectedSkills: SkillContextRef[];
  slashPanelOpen: boolean;
  mentionPanelOpen: boolean;
  setDraft: (d: string) => void;
  addAttachment: (a: ComposerAttachment) => void;
  updateAttachment: (id: string, patch: Partial<ComposerAttachment>) => void;
  removeAttachment: (id: string) => void;
  clearAttachments: () => void;
  setPermissionMode: (m: PermissionMode) => void;
  setEffortLevel: (e: EffortLevel) => void;
  setPRMonitor: (pr: PRMonitorState | null) => void;
  setActionChip: (c: ComposerSlice["actionChip"]) => void;
  setMentionResults: (items: ComposerSlice["mentionResults"]) => void;
  addSelectedMention: (item: ComposerSlice["selectedMentions"][number]) => void;
  removeSelectedMention: (path: string) => void;
  clearSelectedMentions: () => void;
  addSelectedSkill: (skill: Omit<SkillContextRef, "kind">) => void;
  removeSelectedSkill: (name: string) => void;
  clearSelectedSkills: () => void;
  openSlashPanel: () => void;
  closeSlashPanel: () => void;
  openMentionPanel: () => void;
  closeMentionPanel: () => void;
}

// ── Agent Slice ───────────────────────────────────────────────────

export interface TodoItem {
  id: string;
  content: string;
  activeForm: string;
  status: "pending" | "in_progress" | "completed" | "blocked";
}

export interface PlanState {
  planId: string;
  status: "draft" | "accepted" | "executing" | "completed" | "cancelled";
  steps: PlanStep[];
  currentStep: number;
}

export interface SubagentState {
  id: string;
  role: string;
  status: "running" | "done" | "error";
  summary?: string;
  iteration?: number;
  maxIterations?: number;
  currentTool?: string;
  detail?: string;
}

export interface BudgetBucket {
  name: string;
  used: number;
  limit: number;
}

export interface AgentSlice {
  plan: PlanState | null;
  todos: TodoItem[];
  subagents: SubagentState[];
  agentProgress: AgentProgressEntry[];
  runtimeSession: RuntimeSessionSnapshot | null;
  budgetBuckets: BudgetBucket[];
  totalBudgetPercent: number;
  setPlan: (p: PlanState | null) => void;
  updatePlanStep: (idx: number, status: PlanStep["status"]) => void;
  setTodos: (t: TodoItem[]) => void;
  updateTodo: (id: string, patch: Partial<TodoItem>) => void;
  addTodo: (todo: TodoItem) => void;
  removeTodo: (id: string) => void;
  addSubagent: (s: SubagentState) => void;
  updateSubagent: (id: string, patch: Partial<SubagentState>) => void;
  removeSubagent: (id: string) => void;
  setRuntimeSession: (session: RuntimeSessionSnapshot | null) => void;
  appendAgentProgress: (progress: Omit<ProgressContentBlock, "type" | "timestamp">, conversationId?: string) => void;
  finishAgentProgress: (conversationId?: string, status?: "completed" | "failed") => void;
  clearAgentProgress: (conversationId?: string) => void;
  setBudget: (buckets: BudgetBucket[], total: number) => void;
}

// ── Approval Slice ───────────────────────────────────────────────

export interface PendingApproval {
  requestId: string;
  protocol?: "legacy" | "control";
  conversationId?: string;
  toolName: string;
  args: Record<string, unknown>;
  status?: "pending" | "submitted" | "error";
  error?: string;
}

export interface PendingDiffReview {
  requestId: string;
  protocol?: "legacy" | "control";
  conversationId?: string;
  diff: string;
  filePath?: string;
}

export interface PendingAskUser {
  requestId: string;
  protocol?: "legacy" | "control";
  conversationId?: string;
  question: string;
  options?: string[];
}

export interface ApprovalSlice {
  pendingApproval: PendingApproval | null;
  approvalQueue: PendingApproval[];
  pendingDiffReview: PendingDiffReview | null;
  pendingAskUser: PendingAskUser | null;
  setApproval: (a: PendingApproval) => void;
  markApprovalSubmitted: (requestId: string) => void;
  markApprovalError: (requestId: string, error: string) => void;
  clearApproval: (requestId?: string) => void;
  clearApprovals: (requestIds: string[]) => void;
  setDiffReview: (d: PendingDiffReview) => void;
  clearDiffReview: () => void;
  setAskUser: (a: PendingAskUser) => void;
  clearAskUser: () => void;
}

// ── Inspector Slice ──────────────────────────────────────────────

export interface InspectorEntry {
  targetKind: "message" | "tool_call" | "artifact" | "file" | "diff" | "subagent" | "budget";
  targetId: string;
  payload: Record<string, unknown>;
  timestamp: number;
}

export interface InspectorSlice {
  inspectorEntries: InspectorEntry[];
  inspectorFocus: { kind: string; id: string } | null;
  addInspectorEntry: (entry: InspectorEntry) => void;
  setInspectorFocus: (focus: InspectorSlice["inspectorFocus"]) => void;
  clearInspector: () => void;
}

// ── Editor Slice ─────────────────────────────────────────────────

export interface EditorTab {
  path: string;
  content: string;
  original: string;
  contentHash?: string;
  loading: boolean;
  error?: string | null;
  externalChanged?: boolean;
  largeFile?: boolean;
  loadWarning?: string | null;
  sizeBytes?: number;
}

export interface EditorSlice {
  editorTabs: EditorTab[];
  activeTabPath: string | null;
  openEditorTab: (path: string) => void;
  closeEditorTab: (path: string) => void;
  closeOtherEditorTabs: (path: string) => void;
  closeAllEditorTabs: () => void;
  setActiveTab: (path: string) => void;
  updateTabContent: (path: string, content: string) => void;
  markTabLoaded: (
    path: string,
    content: string,
    error?: string | null,
    contentHash?: string,
    meta?: Pick<EditorTab, "largeFile" | "loadWarning" | "sizeBytes">,
  ) => void;
  markTabSaved: (path: string, contentHash?: string) => void;
  markTabExternalChanged: (path: string) => void;
  reloadTab: (path: string, content: string, contentHash?: string) => void;
  insertIntoActiveEditor: (text: string) => boolean;
}

// ── Combined Store ────────────────────────────────────────────────

export type AppStore = UISlice & WorkspaceSlice & ChatSlice & ComposerSlice & AgentSlice & ApprovalSlice & InspectorSlice & EditorSlice;
