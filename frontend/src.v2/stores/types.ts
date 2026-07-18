import type { GoalInfo, PlanStep, RuntimeSessionSnapshot } from "../protocol/events";
import type { AgentCapabilitiesPayload } from "../protocol/capabilities";
import type { AgentProgressPhase } from "../protocol/streaming-types";
import type { ToolCallRecord } from "../lib/tool-call-reducer";

// ── UI Slice ──────────────────────────────────────────────────────

export type ThemeMode = "system" | "dark" | "light";
export type ViewMode = "normal" | "verbose" | "summary";
export type AppMode = "chat" | "code" | "cowork";
export type SessionFilter = "all" | "running" | "waiting" | "idle" | "archived";
export type SessionGroupBy = "none" | "project" | "branch";
export type RightStackTab = "preview" | "browser" | "terminal" | "tasks" | "diff" | "plan" | "subagents" | "artifacts" | "inspector" | "diagnostics";
export type EffortLevel = "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max";

export interface SkillInfo {
  name: string;
  description: string;
  display_name?: string;
  icon?: string;
  version?: string;
  triggers?: string[];
  tools_required?: string[];
  mcp_required?: string[];
  mcp_dependencies?: string[];
  allow_implicit_invocation?: boolean;
  default_prompt?: string;
  source_level?: string;
  active?: boolean;
  usage?: SkillUsageStats;
}

export interface SkillUsageStats {
  load_count?: number;
  reuse_count?: number;
  failure_count?: number;
  unload_count?: number;
  last_event?: string;
  last_invoked_at?: string;
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
  type: "local" | "template" | "protocol" | "local-jsx";
  /** For local-jsx commands: the overlay/panel id this command opens. */
  panel?: string;
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

export interface FileTreeRevealRequest {
  id: string;
  path: string;
  kind: "folder";
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
  sourceAgent?: string;
  sourceThread?: string;
  sourceTool?: string;
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

export interface ConversationWorkbenchState {
  diffReview: DiffReviewState | null;
  previewArtifact: ArtifactContentState | null;
  livePreviewUrl: string | null;
  activeTerminalSessionId: string | null;
  rightStackTab: RightStackTab;
  rightPanelOpen: boolean;
  rightStackTabLocked: boolean;
  draft: string;
  attachments: ComposerAttachment[];
  quotedMessage: ComposerQuote | null;
  selectedMentions: ComposerSlice["selectedMentions"];
  selectedSkills: SkillContextRef[];
  allowedRemoteImageDomains: string[];
}

export type RemoteImagePolicy = "ask" | "allow" | "block";

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
  live?: {
    files: GitChangeFile[];
    toolCallId?: string;
    progress?: number;
    updatedAt: number;
  } | null;
}

export type ContextLedgerCategory =
  | "system_runtime"
  | "guidelines"
  | "skills"
  | "files_attachments"
  | "history"
  | "tool_results"
  | "memory"
  | "compaction_summaries";

export interface ContextLedgerEntry {
  category: ContextLedgerCategory;
  label: string;
  estimated_tokens: number;
  item_count: number;
  source_count: number;
  sources: string[];
}

export interface ContextLedger {
  schema_version: 1;
  estimated_tokens: number;
  actual_tokens: number;
  compaction_count: number;
  native_attachment_tokens: number;
  native_attachment_count: number;
  entries: ContextLedgerEntry[];
}

export interface ContextUsage {
  used: number;
  limit: number;
  compactedAt?: number;
  compactSummary?: string;
  ledger?: ContextLedger;
}

export interface PluginCommandPanelPayload {
  component: string;
  command?: string;
  plugin_name?: string;
  pluginName?: string;
  source?: string;
  arg?: string;
  title?: string;
  description?: string;
  fields?: unknown[];
  prompt_template?: string;
  promptTemplate?: string;
  submit_label?: string;
  submitLabel?: string;
  [key: string]: unknown;
}

export interface UISlice {
  themeMode: ThemeMode;
  textScale: number;
  viewMode: ViewMode;
  appMode: AppMode;
  rightStackTab: RightStackTab;
  rightStackTabLocked: boolean;
  focusedSubagentId: string | null;
  contextUsage: ContextUsage | null;
  remoteImagePolicy: RemoteImagePolicy;
  allowedRemoteImageDomains: string[];
  commandPaletteOpen: boolean;
  settingsOpen: boolean;
  automationsOpen: boolean;
  shortcutsHelpOpen: boolean;
  quickOpenVisible: boolean;
  quickOpenResults: QuickOpenResult[];
  quickOpenLoading: boolean;
  currentModel: string;
  currentProvider: string;
  currentProviderId: string;
  currentProviderBaseUrl: string;
  currentWireApi: string;
  availableModels: string[];
  modelsSource: string;
  availableSkills: SkillInfo[];
  marketplaceSkills: MarketplaceSkill[];
  slashCommands: SlashCommand[];
  workingDirectory: string;
  workspaceGit: WorkspaceGitState | null;
  diffReview: DiffReviewState | null;
  conversationWorkbenchStates: Record<string, ConversationWorkbenchState>;
  previewArtifact: ArtifactContentState | null;
  livePreviewUrl: string | null;
  previewServers: PreviewServerInfo[];
  previewLaunchConfigs: PreviewLaunchConfigInfo[];
  previewLaunchProcesses: PreviewLaunchProcessInfo[];
  previewVerification: PreviewVerificationInfo | null;
  fileChanges: { path: string; event: string; timestamp: number }[];
  fileTreeVersion: number;
  fileTreeRevealRequests: FileTreeRevealRequest[];
  mcpServers: McpServerStatus[];
  envVars: { name: string; description: string; scope: string }[];
  gitChanges: GitChangesState;
  skillsMarketplaceOpen: boolean;
  liveArtifactsOpen: boolean;
  agentEditorOpen: boolean;
  pluginCommandPanelOpen: boolean;
  pluginCommandPanelPayload: PluginCommandPanelPayload | null;
  setThemeMode: (m: ThemeMode) => void;
  setTextScale: (s: number) => void;
  setViewMode: (m: ViewMode) => void;
  setAppMode: (m: AppMode) => void;
  ensureCodeLayout: () => void;
  setRightStackTab: (t: RightStackTab, options?: { automatic?: boolean }) => void;
  setRightStackTabLocked: (locked: boolean) => void;
  setFocusedSubagentId: (id: string | null) => void;
  setContextUsage: (u: ContextUsage | null) => void;
  setRemoteImagePolicy: (policy: RemoteImagePolicy) => void;
  allowRemoteImageDomain: (domain: string) => void;
  clearAllowedRemoteImageDomains: () => void;
  toggleCommandPalette: () => void;
  toggleSettings: () => void;
  toggleAutomations: () => void;
  toggleShortcutsHelp: () => void;
  toggleQuickOpen: () => void;
  toggleSkillsMarketplace: () => void;
  toggleLiveArtifacts: () => void;
  toggleAgentEditor: () => void;
  openPluginCommandPanel: (payload: PluginCommandPanelPayload) => void;
  closePluginCommandPanel: () => void;
  setCurrentModel: (m: string) => void;
  setCurrentProvider: (p: string) => void;
  setCurrentProviderMeta: (meta: { providerId?: string; baseUrl?: string; wireApi?: string }) => void;
  setAvailableModels: (models: string[]) => void;
  setModelsSource: (source: string) => void;
  setAvailableSkills: (skills: SkillInfo[]) => void;
  setMarketplaceSkills: (skills: MarketplaceSkill[]) => void;
  setSlashCommands: (cmds: SlashCommand[]) => void;
  setWorkingDirectory: (d: string) => void;
  setWorkspaceGit: (state: WorkspaceGitState | null) => void;
  setDiffReviewState: (state: DiffReviewState | null) => void;
  snapshotWorkbenchState: (conversationId?: string) => void;
  restoreWorkbenchState: (conversationId?: string) => void;
  clearConversationWorkbenchState: (conversationId: string) => void;
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
  requestFileTreeReveal: (path: string, kind?: FileTreeRevealRequest["kind"]) => void;
  consumeFileTreeRevealRequest: (id: string) => void;
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
  | "artifacts"
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
  exitCode?: number;
  exitedAt?: number;
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

export interface BrowserAnnotation {
  id: string;
  targetId?: string;
  url: string;
  title?: string;
  selector?: string;
  xPercent?: number;
  yPercent?: number;
  note: string;
  createdAt: number;
  screenshotCapturedAt?: number;
  screenshotWidth?: number;
  screenshotHeight?: number;
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
  next_run_at?: string | null;
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
  browserAnnotations: BrowserAnnotation[];
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
  addBrowserAnnotation: (annotation: BrowserAnnotation) => void;
  removeBrowserAnnotation: (id: string) => void;
  clearBrowserAnnotations: (target?: { targetId?: string; url?: string }) => void;
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
  promptCacheTotal?: number;
  promptCacheHitRate?: number;
  reasoning?: number;
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
  dataUrl?: string;
  inputSource?: "pasted_text" | "upload";
  sourceCharCount?: number;
}

export interface ThinkingContentBlock {
  type: "thinking";
  content: string;
  source?: "provider" | "model_preamble" | "post_tool" | "runtime" | string;
  visibility?: "debug" | "timeline" | "compact" | string;
  is_raw_provider_reasoning?: boolean;
  provider_reasoning_type?: string;
}
/** Provider-native citation extracted from LLM response annotations. */
export interface ProviderRawCitation {
  url: string;
  title: string;
  range?: [number, number];
}

export interface ProviderRawOutputItem {
  type: string;
  index: number;
  id?: string;
  status?: string;
  call_id?: string;
  name?: string;
  role?: string;
  phase?: string;
  content_types?: string[];
  arguments_chars?: number;
  summary_count?: number;
  has_encrypted_content?: boolean;
  action_type?: string;
}

export interface ProviderTimelineEvent {
  event: string;
  response_id_hash?: string;
  item_type?: string;
  item_id?: string;
  call_id?: string;
  name?: string;
  status?: string;
  finish_reason?: string;
  output_index?: number;
  content_index?: number;
  sequence_number?: number;
  delta_chars?: number;
  text_chars?: number;
  arguments_chars?: number;
  annotation_count?: number;
  output_items_len?: number;
  usage_present?: boolean;
  omitted?: number;
  [key: string]: unknown;
}

export interface ToolSchemaSizeSummaryRow {
  name?: string;
  chars?: number;
}

export interface ProviderInputSizeSummaryRow {
  index?: number;
  type?: string;
  role?: string;
  name?: string;
  chars?: number;
  content_hash?: string;
}

export interface ProviderDuplicateInputContentRow {
  type?: string;
  role?: string;
  content_hash?: string;
  count?: number;
  chars?: number;
}

export interface ProviderRequestSummary {
  model?: string;
  wire_api?: string;
  instructions_len?: number;
  instructions_sent_len?: number;
  instructions_omitted_by_continuation?: boolean;
  instructions_hash?: string;
  instructions_full_hash?: string;
  tools_len?: number;
  tools_chars?: number;
  tools_hash?: string;
  tool_names?: string[];
  tool_schema_hashes?: Record<string, string>;
  largest_tools?: ToolSchemaSizeSummaryRow[];
  metadata_keys?: string[];
  prompt_cache_key_present?: boolean;
  prompt_cache_key_hash?: string;
  previous_response_id_present?: boolean;
  previous_response_id_hash?: string;
  request_params?: Record<string, unknown>;
  request_param_keys?: string[];
  turn_aborted_marker_present?: boolean;
  input_items_len?: number;
  input_items_sent_len?: number;
  input_items_omitted_by_continuation?: number;
  input_items_logical_len?: number;
  input_chars?: number;
  input_item_counts?: Record<string, number>;
  largest_input_items?: ProviderInputSizeSummaryRow[];
  duplicate_input_content?: ProviderDuplicateInputContentRow[];
  prompt_section_summary?: PromptSectionSummary;
}

export interface ProviderTraceSafety {
  redacted_prompt?: boolean;
  has_encrypted_reasoning?: boolean;
  synthetic_trace?: boolean;
}

export interface ProviderStatefulContinuationSummary {
  configured?: boolean;
  enabled?: boolean;
  used?: boolean;
  input_items_omitted?: number;
  disabled_reason?: string;
  reset_reason?: string;
  stored_response_id_hash?: string;
  covered_items?: number;
  response_output_items_covered?: number;
}

export interface ProviderLoopMetrics {
  provider_call_count?: number;
  iteration?: number;
  iteration_limit?: number;
  iteration_hard_limit?: number;
  tool_batch_count?: number;
  tool_call_count?: number;
  completed_tool_call_count?: number;
  pending_tool_call_count?: number;
  elapsed_ms?: number;
  dynamic_iteration_budget_enabled?: boolean;
}

export interface PromptSectionSummaryRow {
  index?: number;
  name?: string;
  layer?: "stable" | "context" | "volatile" | string;
  chars?: number;
  lines?: number;
  cache_break?: boolean;
  content_hash?: string;
}

export interface PromptSectionLayerSummary {
  chars?: number;
  sections?: number;
  cache_break_sections?: number;
}

export interface PromptSectionSummaryLargest {
  name?: string;
  layer?: "stable" | "context" | "volatile" | string;
  chars?: number;
}

export interface PromptSectionSummary {
  section_count?: number;
  total_chars?: number;
  layers?: Record<string, PromptSectionLayerSummary>;
  sections?: PromptSectionSummaryRow[];
  largest_sections?: PromptSectionSummaryLargest[];
}

export interface PromptSectionDeltaRow {
  name?: string;
  changes?: string[];
  before_layer?: string;
  after_layer?: string;
  chars_delta?: number;
}

export interface PromptSectionDelta {
  status?: "unchanged" | "changed" | string;
  added?: string[];
  removed?: string[];
  changed_sections?: PromptSectionDeltaRow[];
  section_count_delta?: number;
  total_chars_delta?: number;
  layer_char_deltas?: Record<string, number>;
}

export interface PromptCacheDiagnostic {
  status?: string;
  reason?: string;
  tracking_key_hash?: string;
  previous_cache_read_tokens?: number;
  cache_read_tokens?: number;
  cache_creation_tokens?: number;
  token_drop?: number;
  changes?: string[];
  tool_delta?: Record<string, unknown>;
  prompt_section_delta?: PromptSectionDelta;
  seconds_since_previous_observation?: number;
  instructions_len_delta?: number;
}

/** Provider-native metadata forwarded on the DONE event (citations, usage, finish_reason). */
export interface ProviderRawMetadata {
  provider?: string;
  model?: string;
  citations?: ProviderRawCitation[];
  usage?: Record<string, unknown>;
  finish_reason?: string;
  event_type?: string;
  trace_id?: string;
  output_items?: ProviderRawOutputItem[];
  provider_timeline?: ProviderTimelineEvent[];
  request_summary?: ProviderRequestSummary;
  stateful_continuation?: ProviderStatefulContinuationSummary;
  loop_metrics?: ProviderLoopMetrics;
  safety?: ProviderTraceSafety;
  prompt_cache_diagnostic?: PromptCacheDiagnostic;
}

export interface TextContentBlock {
  type: "text";
  content: string;
  source?: "stream" | "reply" | string;
  visibility?: "final" | "timeline" | "unsealed" | "draft" | "debug" | string;
  role?: "assistant" | "runtime" | string;
  phase?: "final" | "model" | "tool" | "recover" | string;
  segmentId?: string;
  iterationIndex?: number;
  streamAttempt?: number;
  sealReason?: string;
  sealed?: boolean;
  isStreaming?: boolean;
  promoteAllUnsealedNarration?: boolean;
  providerRaw?: ProviderRawMetadata;
  finishReason?: string;
}
export interface ProcessContentBlock {
  type: "process";
  id: string;
  itemKind: "process_text" | "action_summary" | "observation" | "status" | "plan" | "tool_group" | string;
  content: string;
  title?: string;
  summary?: string;
  source?: "model" | "runtime" | "system" | "tool" | string;
  status?: "running" | "completed" | "failed" | "info" | string;
  role?: "assistant" | "runtime" | string;
  visibility?: "timeline" | "compact" | "debug" | string;
  loopId?: string;
  iterationId?: string;
  parentId?: string;
  groupId?: string;
  stepId?: string;
  toolCallIds?: string[];
  defaultCollapsed?: boolean;
  displayScope?: string;
  panelHint?: RightStackTab | string;
  requiresAttention?: boolean;
  skillName?: string;
  triggerMode?: "explicit" | "implicit" | "model" | string;
  sourceLevel?: string;
  reason?: string;
  tokenEstimate?: number;
  seq?: number;
  order?: number;
  timestamp: number;
}
export interface ToolCallContentBlock { type: "tool_call"; record: ToolCallRecord; }
export interface ProgressContentBlock {
  type: "progress";
  id: string;
  stage: "status" | "planning" | "tool" | "approval" | "verification" | "final";
  phase?: AgentProgressPhase;
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
  displayScope?: string;
  panelHint?: RightStackTab | string;
  requiresAttention?: boolean;
  ephemeral?: boolean;
  timestamp: number;
}
export interface AgentProgressEntry extends ProgressContentBlock {
  conversationId?: string;
}
export type ContentBlock = ThinkingContentBlock | TextContentBlock | ProcessContentBlock | ToolCallContentBlock | ProgressContentBlock;

export interface ChatMessage {
  id: string;
  turnId?: string;
  role: MessageRole;
  content: string;
  contextRefs?: MessageContextRef[];
  attachmentRefs?: MessageAttachmentRef[];
  blocks?: ContentBlock[];
  artifacts: ArtifactPreview[];
  citations?: Citation[];
  usage?: MessageUsage;
  timestamp: number;
  completedAt?: number;
  isStreaming?: boolean;
  isThinkingStreaming?: boolean;
  resumeState?: "resumed";
  terminalStatus?: "completed" | "partial" | "failed" | "interrupted";
  failureMessage?: string;
  queueState?: "queued" | "cancelled";
  queuePosition?: number;
  queueMessageId?: string;
  /** Attachments carried by a BriefTool (send_message) reply on this message.
   * Rendered as the focused assistant reply. */
  replyAttachments?: ReplyAttachmentMeta[];
}

/** Metadata for a BriefTool reply attachment (local-first, no upload). */
export interface ReplyAttachmentMeta {
  /** Absolute or workspace-relative file path. */
  path: string;
  /** File size in bytes. */
  size: number;
  /** True for previewable image formats. */
  isImage: boolean;
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
  conversationRecallTruncations: Record<string, { removedIds: string[]; retainedIds: string[]; updatedAt: number }>;
  isStreaming: boolean;
  isPaused: boolean;
  isConnected: boolean;
  lastUsage: { input: number; output: number; cacheRead: number; cacheWrite: number; promptCacheTotal?: number; promptCacheHitRate?: number; reasoning?: number } | null;
  usageTotals: { input: number; output: number; cacheRead: number; cacheWrite: number; promptCacheTotal?: number; reasoning?: number; turns: number };
  sideChats: Record<string, SideChatThread>;
  toolCallCount: number;
  sendMessage: (content: string, options?: { assistant?: boolean; contextRefs?: MessageContextRef[]; attachmentRefs?: MessageAttachmentRef[] }) => void;
  deleteMessage: (id: string) => void;
  upsertSystemMessage: (id: string, content: string, options?: { conversationId?: string; replacePrefix?: string }) => void;
  recallMessage: (id: string) => void;
  removeEmptyStreamingAssistant: (conversationId?: string, messageId?: string) => void;
  interrupt: () => void;
  pauseStreaming: () => void;
  requestConversationSwitch: (id: string) => void;
  applyConversationSwitched: (payload: { conversationId: string }) => void;
  switchConversation: (id: string) => void;
  createConversation: (options?: { bindWorkspace?: boolean; workspaceRoot?: string; appMode?: AppMode }) => void;
  removeConversation: (id: string) => void;
  getVisibleMessages: (conversationId?: string | null) => ChatMessage[];
  setActiveGoal: (goal: ConversationGoal | null, conversationId?: string) => void;
  hydrateConversationMessages: (id: string, messages: ChatMessage[], options?: { activate?: boolean; isStreaming?: boolean }) => void;
  bindStreamingTurn: (conversationId: string | undefined, messageId: string | undefined, turnId: string | undefined) => void;
  appendTextChunk: (
    content: string,
    conversationId?: string,
    source?: string,
    metadata?: Partial<Omit<TextContentBlock, "type" | "content" | "source">>,
    messageId?: string,
  ) => void;
  setFinalAnswerAttachments: (conversationId: string | undefined, attachments: ReplyAttachmentMeta[], messageId?: string) => void;
  finalizeStreamingText: (
    conversationId: string | undefined,
    source?: string,
    metadata?: Partial<Omit<TextContentBlock, "type" | "content" | "source">>,
    messageId?: string,
  ) => void;
  appendThinkingChunk: (
    content: string,
    conversationId?: string,
    metadata?: Partial<Omit<ThinkingContentBlock, "type" | "content">>,
    messageId?: string,
  ) => void;
  appendProcessItem: (
    item: Omit<ProcessContentBlock, "type" | "timestamp"> & { timestamp?: number },
    conversationId?: string,
    messageId?: string,
  ) => void;
  appendProgress: (progress: Omit<ProgressContentBlock, "type" | "timestamp">, conversationId?: string, messageId?: string) => void;
  replaceEphemeralProgress: (progress: Omit<ProgressContentBlock, "type" | "timestamp">, conversationId?: string, messageId?: string) => void;
  appendToolCallBlock: (tc: ToolCallRecord, conversationId?: string, messageId?: string) => void;
  updateToolCall: (
    id: string,
    patch: Partial<ToolCallRecord>,
    conversationId?: string,
    scope?: { turnId?: string; iterationId?: string; stepId?: string },
  ) => void;
  finishStreaming: (
    conversationId?: string,
    usage?: MessageUsage,
    terminalStatus?: "completed" | "partial" | "failed" | "interrupted",
    messageId?: string,
    failureMessage?: string,
  ) => void;
  resumeStreaming: (conversationId?: string, toolCallsPending?: PendingToolCallResume[], messageId?: string, turnId?: string) => void;
  replaceStreamingText: (
    conversationId: string,
    fullText: string,
    source?: string,
    metadata?: Partial<Omit<TextContentBlock, "type" | "content" | "source">>,
    messageId?: string,
  ) => void;
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
  turn_id?: string;
  turnId?: string;
  iteration_id?: string;
  iterationId?: string;
  phase?: string;
};

// ── Composer Slice ────────────────────────────────────────────────

export type PermissionMode = "ask_permissions" | "plan" | "auto" | "bypass";
export type PromptPersona = "minicode" | "codex";
export type AgentMode = "build" | "plan" | "review" | "explore";

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
  inputSource?: "pasted_text" | "upload";
  sourceCharCount?: number;
  localFile?: File;
}

export interface ComposerQuote {
  id: string;
  role: MessageRole;
  content: string;
}

export interface ComposerSlice {
  draft: string;
  attachments: ComposerAttachment[];
  quotedMessage: ComposerQuote | null;
  permissionMode: PermissionMode;
  agentMode: AgentMode;
  promptPersona: PromptPersona;
  effortLevel: EffortLevel;
  prMonitor: PRMonitorState | null;
  actionChip: { label: string; description?: string } | null;
  mentionResults: { path: string; name: string; kind: "file" | "folder"; score?: number }[];
  selectedMentions: { path: string; name: string; kind: "file" | "folder" | "url" }[];
  selectedSkills: SkillContextRef[];
  slashPanelOpen: boolean;
  mentionPanelOpen: boolean;
  setDraft: (d: string) => void;
  setQuotedMessage: (message: ComposerQuote) => void;
  clearQuotedMessage: () => void;
  addAttachment: (a: ComposerAttachment) => void;
  updateAttachment: (id: string, patch: Partial<ComposerAttachment>) => void;
  removeAttachment: (id: string) => void;
  clearAttachments: () => void;
  setPermissionMode: (m: PermissionMode) => void;
  setAgentMode: (m: AgentMode) => void;
  setPromptPersona: (persona: PromptPersona) => void;
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

export interface SubagentMessageState {
  messageId: string;
  senderId: string;
  recipientId: string;
  content: string;
  createdAt: number;
  seq?: number;
  deliveryStatus?: "sending" | "sent" | "failed";
}

export interface SubagentState {
  id: string;
  role: string;
  status: "pending" | "running" | "blocked" | "done" | "partial" | "cancelled" | "error";
  summary?: string;
  parentRunId?: string;
  turnId?: string;
  workflowId?: string;
  workflowName?: string;
  workflowMode?: string;
  nodeId?: string;
  taskId?: string;
  dependsOn?: string[];
  blockedBy?: string[];
  objective?: string;
  currentActivity?: string;
  waitingOn?: string;
  blocksFinalReply?: boolean;
  lastProgressAt?: number;
  requiredForFinal?: boolean;
  needsInput?: boolean;
  resultAvailable?: boolean;
  readOnly?: boolean;
  writeScope?: string[];
  order?: number;
  lastEventAt?: number;
  iteration?: number;
  maxIterations?: number;
  currentTool?: string;
  currentToolCallId?: string;
  progressSource?: string;
  detail?: string;
  activityLog?: string[];
  messages?: SubagentMessageState[];
  resultContent?: string;
  resultError?: string;
  durationMs?: number;
  toolCallCount?: number;
  terminationReason?: string;
  terminationInitiator?: "user" | "parent" | "runtime" | "provider" | "tool" | string;
  checkpointId?: string;
}

export interface BudgetBucket {
  name: string;
  used: number;
  limit: number;
}

export interface ConversationAgentState {
  plan: PlanState | null;
  todos: TodoItem[];
  subagents: SubagentState[];
  agentProgress: AgentProgressEntry[];
}

export interface AgentSlice {
  plan: PlanState | null;
  todos: TodoItem[];
  subagents: SubagentState[];
  agentProgress: AgentProgressEntry[];
  conversationAgentStates: Record<string, ConversationAgentState>;
  runtimeSession: RuntimeSessionSnapshot | null;
  runtimeCapabilities: AgentCapabilitiesPayload | null;
  budgetBuckets: BudgetBucket[];
  totalBudgetPercent: number;
  setPlan: (p: PlanState | null, conversationId?: string) => void;
  updatePlanStep: (idx: number, status: PlanStep["status"]) => void;
  setTodos: (t: TodoItem[], conversationId?: string) => void;
  updateTodo: (id: string, patch: Partial<TodoItem>, conversationId?: string) => void;
  addTodo: (todo: TodoItem, conversationId?: string) => void;
  removeTodo: (id: string, conversationId?: string) => void;
  addSubagent: (s: SubagentState, conversationId?: string) => void;
  updateSubagent: (id: string, patch: Partial<SubagentState>, conversationId?: string) => void;
  removeSubagent: (id: string, conversationId?: string) => void;
  setRuntimeSession: (session: RuntimeSessionSnapshot | null) => void;
  setRuntimeCapabilities: (capabilities: AgentCapabilitiesPayload | null) => void;
  appendAgentProgress: (progress: Omit<ProgressContentBlock, "type" | "timestamp">, conversationId?: string) => void;
  finishAgentProgress: (conversationId?: string, status?: "completed" | "failed") => void;
  clearAgentProgress: (conversationId?: string) => void;
  snapshotAgentState: (conversationId?: string) => void;
  restoreAgentState: (conversationId?: string) => void;
  clearConversationAgentState: (conversationId: string) => void;
  setBudget: (buckets: BudgetBucket[], total: number) => void;
}

// ── Approval Slice ───────────────────────────────────────────────

export interface PendingApproval {
  requestId: string;
  protocol?: "legacy" | "control";
  conversationId?: string;
  toolName: string;
  args: Record<string, unknown>;
  sourceAgent?: string;
  sourceThread?: string;
  sourceTool?: string;
  status?: "pending" | "submitted" | "error";
  error?: string;
}

export interface PendingDiffReview {
  requestId: string;
  protocol?: "legacy" | "control";
  conversationId?: string;
  diff: string;
  filePath?: string;
  sourceAgent?: string;
  sourceThread?: string;
  sourceTool?: string;
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

export type InspectorTargetKind = "message" | "tool_call" | "artifact" | "file" | "diff" | "subagent" | "budget" | "provider" | "cache";

export interface InspectorEntry {
  targetKind: InspectorTargetKind;
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
