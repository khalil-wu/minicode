import type { GoalInfo, McpTransport, RuntimeSessionSnapshot, TurnPlanStep } from "../protocol/events";
import type { AgentCapabilitiesPayload } from "../protocol/capabilities";
import type { CompletionNotification } from "../protocol/notification-types";
import type {
  AgentProgressPhase,
  AgentProgressProviderState,
  AgentProgressStage,
  AgentProgressStatus,
} from "../protocol/streaming-types";
import type { ToolCallRecord } from "../lib/tool-call-reducer";
import type { ShortcutActionId, ShortcutBindings } from "../lib/keyboard-shortcuts";
import type { AppearanceColorRole, AppearanceColors, AppearanceVariant } from "../lib/appearance-colors";
import type { AppearanceThemes, AppearanceThemeSelection } from "../lib/appearance-themes";
import type { CodeThemeId } from "../lib/code-themes";
import type { CodeThemeAppearance } from "../lib/theme-sharing";
import type {
  PreviewLaunchConfigInfo,
  PreviewLaunchProcessInfo,
  PreviewServerInfo,
} from "../protocol/preview-types";
export type {
  PreviewLaunchConfigInfo,
  PreviewLaunchProcessInfo,
  PreviewServerInfo,
  PreviewServerOutputLine,
} from "../protocol/preview-types";

// ── UI Slice ──────────────────────────────────────────────────────

export type ThemeMode = "system" | "dark" | "light";
export type ResolvedTheme = "dark" | "light";
export type ViewMode = "normal" | "verbose" | "summary";
export type AppMode = "chat" | "code" | "cowork";
export type SessionFilter = "all" | "running" | "waiting" | "idle" | "archived";
export type SessionGroupBy = "none" | "project" | "branch";
export type RightStackTab = "preview" | "browser" | "files" | "terminal" | "sidechat" | "tasks" | "diff" | "plan" | "goal" | "subagents" | "artifacts" | "inspector" | "diagnostics" | `mcp-app:${string}` | `automation:${string}` | `file:${string}`;
export type EffortLevel =
  | "off"
  | "minimal"
  | "low"
  | "medium"
  | "high"
  | "xhigh"
  | "max"
  | "ultra"
  | (string & Record<never, never>);
export type SendShortcut = "enter" | "cmdIfMultiline" | "mod-enter";
export type FollowUpBehavior = "queue" | "steer";

export interface SkillInfo {
  name: string;
  description: string;
  path?: string;
  display_name?: string;
  short_description?: string;
  icon?: string;
  icon_large?: string;
  brand_color?: string;
  version?: string;
  mcp_dependencies?: string[];
  allow_implicit_invocation?: boolean;
  user_invocable?: boolean;
  default_prompt?: string;
  source_level?: string;
  plugin_enabled?: boolean;
  active?: boolean;
}

export interface MarketplaceSkill {
  name: string;
  title: string;
  description: string;
  triggers: string[];
  installed: boolean;
  source?: string;
  path?: string;
  iconUrl?: string;
  websiteUrl?: string;
}

export interface SlashCommandArg {
  value: string;
  description: string;
}

export interface SlashCommand {
  id?: string;
  name: string;
  command: string;
  label: string;
  description: string;
  type: "local" | "template" | "protocol";
  kind?: string;
  source?: string;
  availability?: {
    kind: string;
    scope: string;
    reason?: string;
  };
  panel?: string;
  /** Structured argument options for local commands (second-stage menu). */
  args?: SlashCommandArg[];
  extensionPath?: string;
  sourcePath?: string;
  template?: string;
  searchText?: string;
  argumentHint?: string;
  argumentNames?: string[];
  baseDir?: string;
  isSkillFile?: boolean;
}

export interface QuickOpenResult {
  path: string;
  name: string;
}

export interface ArtifactContentState {
  artifactId: string;
  content: string;
  preview?: string;
  /** Parser diagnostic shown as a warning, never as extracted content. */
  warning?: string;
  name?: string;
  mediaType?: string;
  url?: string;
  /** The attachment store has the original bytes, independently of preview support. */
  hasNative?: boolean;
  kind?: string;
  sizeBytes?: number;
  contentChars?: number;
  truncated?: boolean;
  loading?: boolean;
  error?: string;
  source?: "artifact" | "attachment" | "workspace" | "browser" | "local" | "skill";
  /** Real disk origin of a workspace or registered Skill preview. */
  sourcePath?: string;
  workspaceRoot?: string;
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

export type McpAuthStatus = "unsupported" | "not_logged_in" | "oauth";

export interface McpServerProgress {
  operation: string;
  message?: string;
  progress?: number;
  status: "running" | "completed" | "failed";
}

export interface McpServerStatus {
  enabledTools?: string[] | null;
  disabledTools?: string[];
  name: string;
  status: "connected" | "disconnected" | "error" | "reconnecting" | "starting" | "offline";
  tools?: number;
  capabilities?: {
    tools?: boolean;
    resources?: boolean;
    resources_subscribe?: boolean;
    resources_list_changed?: boolean;
    prompts?: boolean;
    logging?: boolean;
  };
  transport?: McpTransport;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  headers?: Record<string, string>;
  headersHelper?: string;
  oauth?: { clientId?: string; callbackPort?: number };
  envVars?: Array<string | { name?: string; source?: string }>;
  environmentId?: string;
  bearerTokenEnvVar?: string;
  envHttpHeaders?: Record<string, string>;
  cwd?: string;
  url?: string;
  autoStart?: boolean;
  editable?: boolean;
  enabled?: boolean;
  disabledReason?: string;
  source?: string;
  pluginId?: string | null;
  pluginServerName?: string | null;
  approvalStatus?: "approved" | "rejected" | "pending" | "not_applicable";
  configPath?: string;
  projectWorkspace?: string;
  lastError?: string;
  authStatus?: McpAuthStatus;
  phase?: McpLifecyclePhase;
  recoverable?: boolean;
  requiresUserAction?: boolean;
  setupHint?: string;
  docsUrl?: string;
  cleanup?: {
    pending: boolean;
    reason: string;
    requestedAt?: number | null;
    completedAt?: number | null;
  };
  operationFailures?: Array<{
    operation: string;
    failureKind: string;
    message: string;
    retryable: boolean;
  }>;
  progress?: McpServerProgress;
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
  workspaceRoot: string;
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
  codeReview?: import("../protocol/code-review-types").CodeReviewScope;
  modelComments?: import("../lib/code-comment").CodeReviewComment[];
  workspaceRoot?: string;
  requestId: string;
  conversationId?: string;
  turnId?: string;
  messageId?: string;
  taskId?: string;
  previewReturnTarget?: { conversationId: string; tab: "browser" | "preview"; url?: string; targetId?: string };
  toolName?: string;
  sourceAgent?: string;
  sourceThread?: string;
  sourceTool?: string;
  diff: string;
  files: DiffReviewFile[];
  selectedPath?: string;
  status: "pending" | "submitted" | "approved" | "rejected" | "error" | "viewing";
  mode?: "approval" | "view";
  truncated?: boolean;
  error?: string;
  fileDecisions: Record<string, "approved" | "rejected">;
  lineComments?: DiffLineComment[];
}

export interface DiffLineComment {
  filePath: string;
  position: import("../lib/diff-comments").DiffCommentPosition;
  content: string;
  localDiffHunk?: string;
  localWorkspaceRoot?: string;
}

export interface ConversationWorkbenchState {
  retainedInputEdit?: import("./composer-drafts").RetainedInputEdit;
  queuedEdit?: import("./composer-drafts").QueuedMessageEdit;
  agentMode?: AgentMode;
  goalMode?: boolean;
  diffReview: DiffReviewState | null;
  gitReviewRequest?: GitReviewRequest | null;
  previewArtifact: ArtifactContentState | null;
  livePreviewUrl: string | null;
  previewServers: PreviewServerInfo[];
  previewLaunchConfigs: PreviewLaunchConfigInfo[];
  previewLaunchProcesses: PreviewLaunchProcessInfo[];
  previewVerification: PreviewVerificationInfo | null;
  terminalSessions?: TerminalSessionInfo[];
  activeTerminalSessionId: string | null;
  rightStackTab: RightStackTab;
  rightPanelOpen: boolean;
  rightPanelExpanded?: boolean;
  rightStackTabLocked: boolean;
  draft: string;
  richDraft?: import("../composer/rich-composer-document").RichComposerDocument;
  attachments: ComposerAttachment[];
  quotedMessage: ComposerQuote | null;
  selectedMentions: ComposerSlice["selectedMentions"];
  selectedSkills: SkillContextRef[];
  allowedRemoteImageDomains: string[];
}

export type RemoteImagePolicy = "ask" | "allow" | "block";

export interface GitChangeFile {
  path: string;
  oldPath?: string;
  patch?: string;
  additions: number;
  deletions: number;
  isBinary?: boolean;
  status?: "A" | "M" | "D" | "R" | "C" | "T" | "U";
  oldMode?: string;
  newMode?: string;
  oldOid?: string | null;
  newOid?: string | null;
  revision?: string;
}

export interface GitDiffSnapshot {
  conversationId: string;
  workspaceRoot: string;
  environmentId?: string;
  cwdUri?: string;
  source: GitDiffSource;
  generation: string;
  hideWhitespace: boolean;
}

export type GitDiffSource = "uncommitted" | "unstaged" | "staged" | "branch" | "commit";
export interface GitReviewSelection {
  source: GitDiffSource;
  baseBranch?: string;
  commitSha?: string;
}
export interface GitComparisonState extends GitReviewSelection {
  files: GitChangeFile[];
  loading: boolean;
  requestId?: string;
  snapshot?: GitDiffSnapshot;
  status?: "success" | "expected";
  reason?: string | null;
  error?: string;
  comparison?: { base_branch?: string; merge_base_sha?: string; commit_sha?: string; parent_sha?: string };
}

export interface GitChangesState {
  isGitRepo?: boolean;
  error?: string;
  workingTree: GitChangeFile[];
  staged: GitChangeFile[];
  untracked: string[];
  loading: boolean;
  workspaceRoot?: string;
  workingTreeRequestId?: string;
  stagedRequestId?: string;
  untrackedFiles?: GitChangeFile[];
  workingTreeSnapshot?: GitDiffSnapshot;
  stagedSnapshot?: GitDiffSnapshot;
  comparison?: GitComparisonState;
}

export interface TurnDiffState {
  threadId: string;
  turnId: string;
  messageId?: string;
  taskId?: string;
  diff: string | null;
  deferred?: boolean;
  files?: GitChangeFile[];
  truncated?: boolean;
  source?: string;
  revision?: number;
  toolCallId?: string;
  updatedAt: number;
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

export type SettingsTab =
  | "general"
  | "import"
  | "storage"
  | "config"
  | "hooks"
  | "worktrees"
  | "voice"
  | "appearance"
  | "personalization"
  | "pets"
  | "shortcuts"
  | "provider"
  | "plugins"
  | "marketplace"
  | "skills"
  | "connectors"
  | "computerUse"
  | "connections"
  | "browser"
  | "scheduler"
  | "workspaceGit"
  | "features"
  | "advanced"
  | "archived";

export interface UISlice {
  projectsOpen: boolean;
  projectEditTarget: string | null;
  compactPanel: "left" | "right" | null;
  setCompactPanel: (panel: "left" | "right" | null) => void;
  contextCardCollapsed: boolean;
  setContextCardCollapsed: (collapsed: boolean) => void;
  contextCardPinned: boolean;
  setContextCardPinned: (pinned: boolean) => void;
  toggleContextCard: (width: number) => void;
  themeMode: ThemeMode;
  resolvedTheme: ResolvedTheme;
  appearanceColors: AppearanceColors;
  appearanceThemes: AppearanceThemes;
  textScale: number;
  codeTextScale: number;
  codeFontBase: 12 | 14;
  reducedMotion: boolean;
  reducedMotionPreference: "system" | "on" | "off";
  pointerCursors: boolean;
  diffMarkerStyle: "color" | "symbols";
  viewMode: ViewMode;
  sendShortcut: SendShortcut;
  followUpBehavior: FollowUpBehavior;
  defaultTerminalLocation: "bottom" | "right";
  composerPlainTextMode: boolean;
  primaryNumberShortcutTarget: "tabs" | "sidebar";
  setDefaultTerminalLocation: (location: "bottom" | "right") => Promise<void>;
  setComposerPlainTextMode: (plain: boolean) => Promise<void>;
  shortcutBindings: ShortcutBindings;
  appMode: AppMode;
  workbenchLayout: "tabs" | "split";
  rightStackTab: RightStackTab;
  rightStackTabLocked: boolean;
  focusedSubagentId: string | null;
  contextUsage: ContextUsage | null;
  remoteImagePolicy: RemoteImagePolicy;
  allowedRemoteImageDomains: string[];
  commandPaletteOpen: boolean;
  settingsOpen: boolean;
  settingsTab: SettingsTab;
  browserSettingsSection: "general" | "history" | "downloads" | "passwords" | "contacts" | "sites";
  setBrowserSettingsSection: (section: UISlice["browserSettingsSection"]) => void;
  shortcutsHelpOpen: boolean;
  quickOpenVisible: boolean;
  quickOpenResults: QuickOpenResult[];
  quickOpenLoading: boolean;
  currentModel: string;
  modelSelectionMode: "default" | "model" | null;
  defaultModelSelection: { provider: string; model: string; reasoningEffort: EffortLevel; conversationId: string; workingDirectory: string } | null;
  availableModelOptions: { model: string; reasoningEffortLevels: EffortLevel[]; defaultReasoningEffort: EffortLevel }[];
  currentProvider: string;
  currentProviderId: string;
  currentProviderBaseUrl: string;
  currentWireApi: string;
  availableModels: string[];
  availableModelLabels: Record<string, string>;
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
  previewOwnerConversationId: string | null;
  previewServiceManagerRequest: { conversationId: string; workspaceRoot: string } | null;
  fileChanges: { path: string; event: string; timestamp: number; sequence: number; workspaceRoot: string }[];
  fileTreeVersion: number;
  fileTreeRevealRequests: FileTreeRevealRequest[];
  mcpServers: McpServerStatus[];
  envVars: { name: string; description: string; scope: string; credential_status?: "stored" | "missing" }[];
  gitChanges: GitChangesState;
  skillsMarketplaceOpen: boolean;
  skillsMarketplaceTab: "plugins" | "skills";
  skillsMarketplaceSelectedPluginId: string | null;
  skillsMarketplaceReturnTarget: "app" | "settings";
  liveArtifactsOpen: boolean;
  agentEditorOpen: boolean;
  setThemeMode: (m: ThemeMode) => void;
  setAppearanceColor: (variant: AppearanceVariant, role: AppearanceColorRole, value: string) => void;
  setCodeThemeId: (variant: AppearanceVariant, id: CodeThemeId) => Promise<void>;
  importAppearanceTheme: (variant: AppearanceVariant, snapshot: CodeThemeAppearance) => Promise<boolean>;
  setTextScale: (s: number) => void;
  setCodeTextScale: (s: number) => void;
  setReducedMotion: (reduced: boolean) => void;
  setReducedMotionPreference: (preference: "system" | "on" | "off") => void;
  setPointerCursors: (enabled: boolean) => void;
  setDiffMarkerStyle: (style: "color" | "symbols") => void;
  resetAdvancedAppearance: (supportsTranslucency: boolean) => void;
  patchAppearanceTheme: (variant: AppearanceVariant, patch: Partial<Pick<AppearanceThemeSelection, "fonts" | "contrast" | "opaqueWindows">>) => Promise<void>;
  setViewMode: (m: ViewMode) => void;
  setSendShortcut: (shortcut: SendShortcut) => void;
  setFollowUpBehavior: (behavior: FollowUpBehavior) => void;
  setShortcutBinding: (action: ShortcutActionId, binding: string, index?: number) => void;
  resetShortcutBindings: () => void;
  setAppMode: (m: AppMode) => void;
  setWorkbenchLayout: (layout: "tabs" | "split") => void;
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
  setSettingsTab: (tab: SettingsTab) => void;
  toggleShortcutsHelp: () => void;
  toggleQuickOpen: () => void;
  toggleSkillsMarketplace: (returnTarget?: "app" | "settings", tab?: "plugins" | "skills") => void;
  toggleLiveArtifacts: () => void;
  toggleAgentEditor: () => void;
  setCurrentModel: (m: string) => void;
  setCurrentProvider: (p: string) => void;
  setCurrentProviderMeta: (meta: { providerId?: string; baseUrl?: string; wireApi?: string }) => void;
  setAvailableModels: (models: string[]) => void;
  setAvailableModelLabels: (labels: Record<string, string>) => void;
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
  removeDiffLineComment: (position: import("../lib/diff-comments").DiffCommentPosition) => void;
  submitDiffReviewWithComments: () => Promise<void>;
  submitPartialApproval: () => Promise<void>;
  setPreviewArtifact: (artifact: ArtifactContentState | null) => void;
  setConversationPreviewArtifact: (conversationId: string, artifact: ArtifactContentState | null) => void;
  setPreviewOwnerConversationId: (conversationId: string | null) => void;
  restorePreviewState: (conversationId?: string) => void;
  setLivePreviewUrl: (url: string | null, conversationId?: string) => void;
  openLivePreview: (url: string, conversationId?: string) => void;
  setPreviewServers: (servers: PreviewServerInfo[], conversationId?: string) => void;
  addPreviewServer: (server: PreviewServerInfo, conversationId?: string) => void;
  removePreviewServer: (port: number, conversationId?: string) => void;
  setPreviewLaunchConfigs: (configs: PreviewLaunchConfigInfo[], conversationId?: string) => void;
  setPreviewLaunchProcesses: (processes: PreviewLaunchProcessInfo[], conversationId?: string) => void;
  upsertPreviewLaunchProcess: (process: PreviewLaunchProcessInfo, conversationId?: string) => void;
  removePreviewLaunchProcess: (id: string, conversationId?: string) => void;
  setPreviewVerification: (verification: PreviewVerificationInfo | null, conversationId?: string) => void;
  setQuickOpenResults: (results: QuickOpenResult[]) => void;
  setQuickOpenLoading: (loading: boolean) => void;
  addFileChange: (change: { path: string; event: string; timestamp: number }) => void;
  bumpFileTreeVersion: () => void;
  requestFileTreeReveal: (path: string, kind?: FileTreeRevealRequest["kind"]) => void;
  consumeFileTreeRevealRequest: (id: string) => void;
  setMcpServers: (servers: McpServerStatus[]) => void;
  setEnvVars: (entries: { name: string; description: string; scope: string; credential_status?: "stored" | "missing" }[]) => void;
  setGitChanges: (changes: Partial<GitChangesState>) => void;
  setGitChangesLoading: (loading: boolean) => void;
  requestGitChanges: () => void;
  requestGitComparison: (selection: GitReviewSelection) => void;
}

// ── Workspace Slice ───────────────────────────────────────────────

export type PanelKind =
  | "chat"
  | "diff"
  | "editor"
  | "file"
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
  fileTabId?: string;
  label?: string;
  size?: number;
  focused?: boolean;
  maximized?: boolean;
}

export interface TerminalSessionInfo {
  id: string;
  conversationId: string;
  pid?: number;
  shell: string;
  cwd: string;
  status?: "running" | "exited";
  createdAt?: number;
  exitCode?: number | null;
  exitSignal?: number | string | null;
  cleanupPending?: boolean;
  cleanupReason?: string;
  exitedAt?: number;
  terminalMode?: "pty" | "pipe";
}

export interface TerminalSnapshotInfo {
  id: string;
  conversationId: string;
  pid?: number | null;
  shell: string;
  cwd: string;
  status?: "running" | "exited";
  exitCode?: number | null;
  exitSignal?: number | string | null;
  cleanupPending?: boolean;
  cleanupReason?: string;
  exitedAt?: number;
  terminalMode?: "pty" | "pipe";
  output: string;
  outputChars?: number;
  totalOutputChars?: number;
  outputStartCursor?: number;
  outputEndCursor?: number;
  truncated?: boolean;
  capturedAt: number;
  error?: string;
}

export interface EditorOpenRequest {
  preview?: boolean;
  id: string;
  path: string;
  exact?: boolean;
  line?: number;
  column?: number;
  endLine?: number;
  endColumn?: number;
}

export interface CodeSelectionRange {
  startLineNumber: number;
  startColumn: number;
  endLineNumber: number;
  endColumn: number;
}

export interface CodeSelectionContext {
  text: string;
  source?: string;
  workspaceRoot?: string;
  range?: CodeSelectionRange;
}

export interface BackgroundTaskEntry {
  id: string;
  command: string;
  status: "running" | "stalled" | "completed" | "failed" | "cancelled";
  exitCode?: number;
  cleanupPending?: boolean;
  cleanupReason?: string;
  terminalStatus?: string;
  duration?: number;
  timestamp: number;
  completedAt?: number;
  conversationId: string;
  cwd?: string;
  outputPreview?: string;
  stalledTail?: string;
  stalledAdvice?: string;
  stalledAt?: number;
}

export interface BrowserAnnotation {
  id: string;
  targetId?: string;
  url: string;
  title?: string;
  selector?: string;
  xPercent?: number;
  yPercent?: number;
  widthPercent?: number;
  heightPercent?: number;
  viewportWidth?: number;
  viewportHeight?: number;
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
  execution_workspace_root?: string | null;
  projectless_base?: string | null;
  provider?: string | null;
  model?: string | null;
  reasoning_effort?: string | null;
  timezone?: string;
  isolation?: "worktree" | "workspace";
  conversation_id?: string;
  kind?: "cron" | "heartbeat";
  notification_policy?: "failed_runs_only" | null;
  permission_mode: string;
  enabled: boolean;
  last_run_at?: string | null;
  next_run_at?: string | null;
  created_at?: string;
  workspace_root?: string;
  last_run_id?: string | null;
  last_run_status?: "pending" | "running" | "completed" | "partial" | "failed" | "cancelled" | string | null;
  last_error?: string | null;
}

export interface ScheduledTaskRunEntry {
  id: string;
  task_id: string;
  scheduled_at: string;
  started_at?: string;
  finished_at?: string | null;
  status: "pending" | "running" | "completed" | "failed" | "cancelled" | string;
  conversation_id?: string;
  workspace_root?: string;
  execution_workspace_root?: string | null;
  result_summary?: string;
  message_id?: string;
  completion_notification?: CompletionNotification | null;
  error?: string;
}

export interface GitReviewRequest {
  id: string;
  path: string;
  section: "staged" | "working" | "untracked";
  workspaceRoot: string;
  conversationId: string | null;
  source?: GitDiffSource;
  baseBranch?: string;
  fileOwner?: WorkspaceFileOwner;
}

export interface WorkspaceFileOwner {
  scopeId: string;
  conversationId: string | null;
  executionCwd: string;
  browserRoot: string;
  environmentId?: string;
}

export interface WorkspaceFileTab {
  id: string;
  owner: WorkspaceFileOwner;
  path: string;
  title: string;
  open: boolean;
  pinned: boolean;
  pane: "left" | "right";
  line?: number;
  column?: number;
  endLine?: number;
  endColumn?: number;
  isPreview?: boolean;
  locationRequestId?: string;
}

export interface WorkspaceFileOpenOptions {
  column?: number;
  endLine?: number;
  endColumn?: number;
  isPreview?: boolean;
}

export interface WorkspaceSlice {
  initializeWorkspaceView: (conversationId: string, fullView: boolean) => void;
  workspaceFileTabs: WorkspaceFileTab[];
  fileBrowserRoots: Record<string, string>;
  pendingWorkspaceFileTabId: string | null;
  reconcileWorkspaceFileTabs: () => void;
  openWorkspaceFileTab: (owner: WorkspaceFileOwner, path: string, title?: string, line?: number, options?: WorkspaceFileOpenOptions) => string;
  activateWorkspaceFileTab: (id: string) => void;
  closeWorkspaceFileTab: (id: string) => void;
  pinWorkspaceFileTab: (id: string, pinned: boolean) => void;
  moveWorkspaceFileTab: (id: string, pane: "left" | "right") => void;
  setFileBrowserRoot: (scopeId: string, path: string) => void;
  leftSidebarWidth: number;
  leftSidebarExpandedWidth: number;
  rightSidebarWidth: number;
  rightSidebarWidthRatio: number | null;
  rightPanelOpen: boolean;
  rightPanelExpanded: boolean;
  gitReviewRequest: GitReviewRequest | null;
  dockHeight: number;
  dockCollapsed: boolean;
  activeBottomTab: "terminal" | "git" | "tasks" | "timeline" | "debug" | "budget" | "problems";
  panelSlots: PanelSlot[];
  sideChatOpen: boolean;
  activeSideChatId: string | null;
  sideChatPendingContext: CodeSelectionContext | null;
  terminalSessions: TerminalSessionInfo[];
  terminalSnapshots: Record<string, TerminalSnapshotInfo>;
  backgroundTasks: BackgroundTaskEntry[];
  browserAnnotations: BrowserAnnotation[];
  activeTerminalSessionId: string | null;
  editorOpenRequests: EditorOpenRequest[];
  activeEditorOpenRequestId: string | null;
  activeEditorPath: string | null;
  setLeftSidebarWidth: (w: number, options?: { persist?: boolean }) => void;
  setRightSidebarWidth: (w: number, options?: { mainContentWidth?: number; persist?: boolean }) => void;
  toggleRightPanel: () => void;
  setRightPanelExpanded: (expanded: boolean) => void;
  openGitReview: (request: Omit<GitReviewRequest, "id">) => void;
  setDockHeight: (h: number) => void;
  toggleDock: () => void;
  openBottomTab: (t: WorkspaceSlice["activeBottomTab"]) => void;
  openTerminal: () => void;
  toggleTerminal: () => void;
  closeBottomDock: () => void;
  setActiveBottomTab: (t: WorkspaceSlice["activeBottomTab"]) => void;
  addPanel: (slot: PanelSlot | (Omit<PanelSlot, "kind"> & { kind: LegacyPanelKind })) => void;
  removePanel: (id: string) => void;
  focusPanel: (id: string, options?: { preserveRightPanelExpanded?: boolean }) => void;
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
  openEditorFile: (path: string, label?: string, target?: { line?: number; column?: number; endLine?: number; endColumn?: number; exact?: boolean; preview?: boolean }) => void;
  consumeEditorOpenRequest: (id: string) => void;
  toggleSideChat: () => void;
  closeSideChat: () => void;
  openSideChatWithSelection: (text: string, source?: string, location?: Pick<CodeSelectionContext, "range" | "workspaceRoot">) => void;
  addBackgroundTask: (task: BackgroundTaskEntry) => void;
  addBrowserAnnotation: (annotation: BrowserAnnotation) => void;
  removeBrowserAnnotation: (id: string) => void;
  clearBrowserAnnotations: (target?: { targetId?: string; url?: string }) => void;
  prStatus: PrStatus | null;
  ciChecks: CiCheck[];
  setPrStatus: (pr: PrStatus | null, checks: CiCheck[]) => void;
  scheduledTasks: ScheduledTaskEntry[];
  setScheduledTasks: (tasks: ScheduledTaskEntry[]) => void;
  scheduledTaskRuns: ScheduledTaskRunEntry[];
  setScheduledTaskRuns: (runs: ScheduledTaskRunEntry[]) => void;
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
  /** UTF-16 code-unit offset where the artifact appeared in assistant text. */
  textOffset?: number;
  source?: "tool" | "image_generation";
  toolCallId?: string;
  callSource?: ToolCallRecord["callSource"];
  operation?: string;
}

export interface Citation {
  source: string;
  range: [number, number];
  label?: string;
  url?: string;
  title?: string;
  locationType?: string;
  /** True only for citation ownership supplied directly by the model provider. */
  providerNative?: boolean;
}

export interface MessageUsage {
  input: number;
  ordinaryInput?: number;
  inputIncludesCacheRead?: boolean;
  inputIncludesCacheWrite?: boolean;
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
  range?: CodeSelectionRange;
  text?: string;
  workspaceRoot?: string;
}

export interface SkillContextRef {
  kind: "skill";
  name: string;
  path?: string;
  description?: string;
  sourceLevel?: string;
  displayName?: string;
  icon?: string;
  iconVariant?: "small" | "large";
}

export interface PluginContextRef {
  kind: "plugin";
  name: string;
  path: string;
  configName: string;
  description?: string;
  pluginPath?: string;
  iconUrl?: string;
  iconVariant?: "composer" | "logo" | "logo-dark";
  iconVariants?: ("composer" | "logo" | "logo-dark")[];
}

export interface BrowserAnnotationContextRef {
  kind: "browser_annotation";
  name: string;
  path: string;
  url: string;
  note: string;
  selector?: string;
  targetId?: string;
  xPercent?: number;
  yPercent?: number;
  widthPercent?: number;
  heightPercent?: number;
  viewportWidth?: number;
  viewportHeight?: number;
}

export interface DiffCommentContextRef {
  kind: "diff_comment";
  name: string;
  path: string;
  comment: import("../lib/diff-comments").DiffComment;
}

export type MessageContextRef = FileContextRef | SkillContextRef | PluginContextRef | BrowserAnnotationContextRef | DiffCommentContextRef;

export interface MessageAttachmentRef {
  id: string;
  name: string;
  kind: "image" | "document" | "file";
  mediaType: string;
  sizeBytes: number;
  artifactId?: string;
  docId?: string;
  dataUrl?: string;
  inputSource?: "pasted_text" | "upload";
  sourceCharCount?: number;
}

/** Origin metadata for a transcript message supplied by the backend. */
export interface ChatMessageSource {
  kind: "scheduled_task";
  taskId?: string;
  runId?: string;
}

export interface ThinkingContentBlock {
  type: "thinking";
  content: string;
  source?: "provider" | "model_preamble" | "post_tool" | "runtime" | string;
  visibility?: "debug" | "timeline" | "compact" | string;
  phase?: string;
  providerReasoningType?: string;
  item_id?: string;
  content_index?: number;
  lifecycle?: "start" | "delta" | "end" | string;
}
/** Provider-native citation extracted from LLM response annotations. */
export interface ProviderRawCitation {
  source?: string;
  url?: string;
  title?: string;
  label?: string;
  location_type?: "char_location" | "page_location" | "content_block_location" | "search_result_location" | string;
  range?: [number, number];
}

export interface ProviderSearchSource {
  title?: string;
  url?: string;
}

export interface ProviderContainerMetadata {
  id?: string;
  expires_at?: string;
}

export interface ProviderRefusalMetadata {
  type?: string;
  category?: string;
  /** Signals that the backend consumed a provider explanation without exposing it. */
  explanation_available?: boolean;
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
  transport?: {
    mode?: "http" | "websocket";
    incremental?: boolean;
    connection_reused?: boolean;
    response_items_complete?: boolean;
    input_items_logical_len?: number;
    input_items_sent_len?: number;
    request_json_bytes?: number;
    fallback_status?: number;
  };
  instructions_len?: number;
  instructions_sent_len?: number;
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
  request_params?: Record<string, unknown>;
  request_param_keys?: string[];
  turn_aborted_marker_present?: boolean;
  input_items_len?: number;
  input_items_sent_len?: number;
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
  kind?: string;
  provider?: string;
  model?: string;
  citations?: ProviderRawCitation[];
  search_sources?: ProviderSearchSource[];
  container?: ProviderContainerMetadata;
  refusal?: ProviderRefusalMetadata;
  usage?: Record<string, unknown>;
  raw_usage?: Record<string, unknown>;
  finish_reason?: string;
  event_type?: string;
  request_id?: string;
  trace_id?: string;
  iteration_id?: string;
  call_index?: number;
  diagnostics_deferred?: boolean;
  diagnostics_ref?: string;
  diagnostics_bytes?: number;
  diagnostics_loaded?: boolean;
  output_items?: ProviderRawOutputItem[];
  provider_timeline?: ProviderTimelineEvent[];
  request_summary?: ProviderRequestSummary;
  loop_metrics?: ProviderLoopMetrics;
  safety?: ProviderTraceSafety;
  prompt_cache_diagnostic?: PromptCacheDiagnostic;
}

export interface AsyncUserQuestion { title: string; options?: string[]; }

export interface TextContentBlock {
  type: "text";
  itemId?: string;
  content: string;
  source?: "stream" | "reply" | "model_final" | "retained" | "commentary" | "model_preamble" | "post_tool" | "runtime" | string;
  status?: "in_progress" | "completed" | "partial" | string;
  isStreaming?: boolean;
  providerRaw?: ProviderRawMetadata;
  finishReason?: string;
  delivery?: "async";
  questions?: AsyncUserQuestion[];
}
export interface ProcessContentBlock {
  type: "process";
  id: string;
  itemKind: "process_text" | "observation" | "status" | "plan" | "tool_group" | string;
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
  skillName?: string;
  triggerMode?: "explicit" | "implicit" | "model" | string;
  sourceLevel?: string;
  reason?: string;
  tokenEstimate?: number;
  seq?: number;
  order?: number;
  timestamp: number;
  startedAt?: number;
  finishedAt?: number;
}
export interface ToolCallContentBlock { type: "tool_call"; record: ToolCallRecord; }
export interface ProgressContentBlock {
  type: "progress";
  id: string;
  stage: AgentProgressStage;
  phase?: AgentProgressPhase;
  status: AgentProgressStatus;
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
  ephemeral?: boolean;
  retryAttempt?: number;
  maxRetries?: number;
  retryAfterMs?: number;
  statusCode?: number;
  providerErrorCode?: string;
  errorMessage?: string;
  operationId?: string;
  providerState?: AgentProgressProviderState;
  subagentId?: string;
  subagentName?: string;
  subagentIdentity?: string;
  subagentStatus?: SubagentState["status"];
  timestamp: number;
}
export interface AgentProgressEntry extends ProgressContentBlock {
  conversationId?: string;
}
export interface GuardianAssessmentContentBlock { type: "guardian_assessment"; review: import("../protocol/guardian-types").GuardianAssessment; }
export interface GuardianWarningContentBlock { type: "guardian_warning"; message: string; turnId: string; interruption?: boolean; }
export interface UserInputContentBlock { type: "user_input"; userMessageId: string; }
export type ContentBlock = (GuardianAssessmentContentBlock | GuardianWarningContentBlock | UserInputContentBlock | ThinkingContentBlock | TextContentBlock | ProcessContentBlock | ToolCallContentBlock | ProgressContentBlock) & { transcriptIndex?: number };

export interface ToolHistoryPage { before: number; remaining: number; total: number; revision?: string }

export interface ChatMessage {
  realtimeItem?: import("../protocol/realtime-types").RealtimeItem;
  realtimeCompleted?: boolean;
  asyncRequestId?: string;
  id: string;
  turnId?: string;
  toolPage?: ToolHistoryPage;
  /** Authoritative file changes owned by this assistant turn. */
  turnDiff?: TurnDiffState;
  role: MessageRole;
  content: string;
  /** Exact assembled model input for a user message with separate display text. */
  backendContent?: string;
  codeReview?: import("../protocol/code-review-types").CodeReviewScope;
  quotedMessage?: ComposerQuote | null;
  messageSource?: ChatMessageSource;
  contextRefs?: MessageContextRef[];
  attachmentRefs?: MessageAttachmentRef[];
  blocks?: ContentBlock[];
  artifacts: ArtifactPreview[];
  citations?: Citation[];
  usage?: MessageUsage;
  timestamp: number;
  /** Original execution start received from main run-start or stream_resume. */
  runStartedAt?: number;
  completedAt?: number;
  durationMs?: number;
  isStreaming?: boolean;
  isThinkingStreaming?: boolean;
  terminalStatus?: "completed" | "partial" | "failed" | "interrupted";
  /** Local delivery lifecycle for a user row awaiting durable admission. */
  deliveryStatus?: "sending" | "sent" | "failed";
  terminationReason?: string;
  failureMessage?: string;
  failureRecoverable?: boolean;
  /** Short label for transcript-only system records; content remains the
   * information-bearing notice body instead of being forced into a title. */
  systemNoticeTitle?: string;
  systemNoticeProcessing?: boolean;
  queueState?: "queued" | "cancelled";
  queuePosition?: number;
  queuePaused?: boolean;
  queueMessageId?: string;
  steeredIntoMessageId?: string;
  /** Verified workspace deliverables produced by tools during this reply.
   * The transport name is retained for persisted transcript compatibility. */
  replyAttachments?: ReplyAttachmentMeta[];
}

/** Metadata for a generated workspace deliverable (local-first, no upload). */
export interface ReplyAttachmentMeta {
  /** Absolute or workspace-relative file path. */
  path: string;
  /** File size in bytes. */
  size: number;
  /** True for previewable image formats. */
  isImage: boolean;
}

export interface ConversationMeta {
  agentMode?: AgentMode;
  id: string;
  title: string;
  updatedAt: string;
  createdAt?: string;
  revision?: number;
  summary?: string;
  compactionState?: string;
  compactionSummary?: string;
  conversationType?: "main" | "side_chat";
  threadSource?: string;
  archived?: boolean;
  pinned?: boolean;
  pinnedAt?: string;
  resourceReferences?: import("../protocol/thread-metadata").ThreadResourceReference[];
  hasUnreadTurn?: boolean;
  projectId?: string | null;
  memoryMode?: "enabled" | "disabled" | "polluted" | string;
  memoryPolluted?: boolean;
  memoryPollutionSources?: string[];
  workspaceRoot?: string;
  executionCwd?: string;
  projectlessOutputDir?: string;
  projectlessBrowserRoot?: string;
  gitBranch?: string;
  worktreePath?: string;
  gitIsolated?: boolean;
  sessionStatus?: "running" | "waiting" | "idle";
  messageCount?: number;
  dispatchBadge?: boolean;
  environment?: "local" | "remote" | "ssh";
  goal?: ConversationGoal | null;
  parentConversationId?: string;
  parentMessageIndex?: number;
  forkId?: string;
  branchKind?: string;
  mergedIntoConversationId?: string;
  mergedAt?: string;
}

export interface ConversationGoal {
  tokenBudget?: number | null;
  tokensUsed?: number;
  timeUsedSeconds?: number;
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
    tokenBudget: goal?.token_budget,
    tokensUsed: goal?.tokens_used,
    timeUsedSeconds: goal?.time_used_seconds,
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

export type ConnectionPhase = "connecting" | "connected" | "reconnecting" | "failed";

export interface ConnectionStateDetails {
  phase: ConnectionPhase;
  attempt: number;
  maxAttempts: number | null;
  error: string | null;
}

export interface SideChatThread {
  id: string;
  messages: ChatMessage[];
  isStreaming: boolean;
  draft: string;
  richDraft?: import("../composer/rich-composer-document").RichComposerDocument;
  inheritedContext?: string;
  parentConversationId?: string;
  selectedContext?: CodeSelectionContext;
  attachments?: ComposerAttachment[];
  contextRefs?: MessageContextRef[];
  workspaceRoot?: string;
  model?: string;
  permissionMode?: PermissionMode;
}

export interface ConversationHistoryPage {
  beforeMessageId: string;
  hasMore: boolean;
  loading: boolean;
}

export interface ConversationDraftContext {
  id: string;
  pendingUserMessage?: ChatMessage;
  workspaceRoot: string;
  bindWorkspace: boolean;
  projectlessBase?: string;
  executionCwd?: string;
  appMode: AppMode;
  sidebarSectionId?: string;
  permissionMode: PermissionMode;
  permissionEdited: boolean;
  modelSelection: { mode: "default" | "model"; model?: string; effort?: EffortLevel };
  modelProjection?: import("../protocol/api").DraftModelSelection;
  selectionLoading: boolean;
  selectionError: string | null;
  preferencesLoading?: boolean;
  materializing?: boolean;
  requestedConversationId?: string;
  creationCommandId?: string;
  conversationId?: string;
  adopted?: boolean;
}

export interface ConversationDraftOptions {
  bindWorkspace?: boolean;
  workspaceRoot?: string;
  appMode?: AppMode;
  sidebarSectionId?: string;
  forceNew?: boolean;
}

export interface ChatSlice {
  activeConversationDraftId: string | null;
  conversationDrafts: Record<string, ConversationDraftContext>;
  preparingComposerMessages: Record<string, ChatMessage>;
  retainedComposerInputs: Record<string, import("./composer-drafts").RetainedComposerInput>;
  homeConversationDraftIds: Record<string, string>;
  startConversationDraft: (options?: ConversationDraftOptions) => string;
  selectConversationDraft: (id: string) => void;
  refreshConversationDraftSelection: (id?: string) => Promise<boolean>;
  materializeConversationDraft: (id: string) => Promise<string | null>;
  messageRevealTarget: { conversationId: string; messageId: string; requestId: string } | null;
  conversationId: string | null;
  pendingConversationSwitchId: string | null;
  pendingConversationCreateId: string | null;
  conversations: ConversationMeta[];
  conversationInventoryInstanceId: string | null;
  conversationInventoryRevision: number;
  pinnedConversationIds: string[];
  pinnedConversationSortMode: "manual" | "updated_at";
  sidebarOrder: import("../protocol/conversation-types").SidebarOrderPayload | null;
  activeGoal: ConversationGoal | null;
  messages: ChatMessage[];
  conversationMessages: Record<string, ChatMessage[]>;
  conversationHistoryPages: Record<string, ConversationHistoryPage>;
  conversationStreaming: Record<string, boolean>;
  /** Provider retry frames that arrived before their exact assistant owner. */
  pendingProviderProgress: Record<string, ProgressContentBlock[]>;
  conversationRecallTruncations: Record<string, { removedIds: string[]; retainedIds: string[]; updatedAt: number }>;
  isStreaming: boolean;
  isPaused: boolean;
  isConnected: boolean;
  connectionPhase: ConnectionPhase;
  reconnectAttempt: number;
  reconnectMaxAttempts: number | null;
  connectionError: string | null;
  lastUsage: { input: number; ordinaryInput?: number; inputIncludesCacheRead?: boolean; inputIncludesCacheWrite?: boolean; output: number; cacheRead: number; cacheWrite: number; cacheDeleted?: number; promptCacheTotal?: number; promptCacheHitRate?: number; reasoning?: number } | null;
  usageTotals: { input: number; ordinaryInput?: number; output: number; cacheRead: number; cacheWrite: number; promptCacheTotal?: number; reasoning?: number; turns: number };
  sideChats: Record<string, SideChatThread>;
  toolCallCount: number;
  sendMessage: (content: string, options?: { assistant?: boolean; contextRefs?: MessageContextRef[]; attachmentRefs?: MessageAttachmentRef[] }) => void;
  upsertSystemMessage: (id: string, content: string, options?: { conversationId?: string; replacePrefix?: string; title?: string; processing?: boolean; failureMessage?: string; process?: Omit<ProcessContentBlock, "type" | "timestamp"> & { timestamp?: number } }) => void;
  recallMessage: (id: string) => Promise<boolean>;
  removeEmptyStreamingAssistant: (conversationId?: string, messageId?: string) => void;
  interrupt: () => void;
  requestConversationSwitch: (id: string) => void;
  applyConversationSwitched: (payload: { conversationId: string }) => void;
  switchConversation: (id: string) => void;
  ensureComposerConversation: () => Promise<string | null>;
  createConversation: (options?: { bindWorkspace?: boolean; workspaceRoot?: string; appMode?: AppMode; sidebarSectionId?: string; activate?: boolean; conversationId?: string; clientCommandId?: string; permissionMode?: PermissionMode; agentMode?: AgentMode; initialPrompt?: string; projectlessBase?: string; preferences?: import("../desktop/runtime").DesktopPreferences | null; onCreated?: (result: import("../protocol/events").CommandResultEvent) => void }) => Promise<boolean>;
  removeConversation: (id: string) => Promise<boolean>;
  getVisibleMessages: (conversationId?: string | null) => ChatMessage[];
  setActiveGoal: (goal: ConversationGoal | null, conversationId?: string, revision?: number) => void;
  hydrateConversationMessages: (id: string, messages: ChatMessage[], options?: { activate?: boolean; isStreaming?: boolean; historyPage?: ConversationHistoryPage; confirmedUserMessageIds?: string[] }) => void;
  recordTurnUserInput: (message: ChatMessage, conversationId: string, assistantMessageId: string) => void;
  bindStreamingTurn: (conversationId: string | undefined, messageId: string | undefined, turnId: string | undefined, startedAt?: number) => void;
  startAgentMessage: (
    itemId: string,
    conversationId?: string,
    messageId?: string,
    source?: string,
  ) => void;
  appendAgentMessageDelta: (
    itemId: string,
    delta: string,
    conversationId?: string,
    messageId?: string,
    source?: string,
  ) => void;
  setFinalAnswerAttachments: (conversationId: string | undefined, attachments: ReplyAttachmentMeta[], messageId?: string) => void;
  completeAgentMessage: (
    item: { id: string; text: string; source?: string; status?: string; delivery?: "async"; questions?: AsyncUserQuestion[] },
    conversationId?: string,
    metadata?: Pick<TextContentBlock, "providerRaw" | "finishReason">,
    messageId?: string,
  ) => void;
  appendThinkingChunk: (
    content: string,
    conversationId?: string,
    metadata?: Partial<Omit<ThinkingContentBlock, "type" | "content">>,
    messageId?: string,
  ) => void;
  settleThinking: (conversationId?: string, messageId?: string) => void;
  appendProcessItem: (
    item: Omit<ProcessContentBlock, "type" | "timestamp"> & { timestamp?: number },
    conversationId?: string,
    messageId?: string,
  ) => void;
  upsertMessageProgress: (
    progress: Omit<ProgressContentBlock, "type" | "timestamp"> & { timestamp?: number },
    conversationId?: string,
    messageId?: string,
  ) => void;
  flushPendingProviderProgress: (conversationId: string, messageId: string) => void;
  clearPendingProviderProgress: (conversationId?: string, messageId?: string) => void;
  removeProcessItem: (
    itemId: string,
    conversationId?: string,
    messageId?: string,
  ) => void;
  appendProcessItemDelta: (
    itemId: string,
    delta: string,
    conversationId?: string,
    messageId?: string,
  ) => void;
  appendToolCallBlock: (tc: ToolCallRecord, conversationId?: string, messageId?: string) => void;
  updateToolCall: (
    id: string,
    patch: Partial<ToolCallRecord>,
    conversationId?: string,
    scope?: { turnId?: string; iterationId?: string; stepId?: string },
    messageId?: string,
  ) => void;
  finishStreaming: (
    conversationId?: string,
    usage?: MessageUsage,
    terminalStatus?: "completed" | "partial" | "failed" | "interrupted",
    messageId?: string,
    failureMessage?: string,
    failureRecoverable?: boolean,
    durationMs?: number,
    terminationReason?: string,
  ) => void;
  resumeStreaming: (
    conversationId?: string,
    toolCallsPending?: PendingToolCallResume[],
    messageId?: string,
    turnId?: string,
    snapshotBlocks?: ContentBlock[],
    startedAt?: number,
  ) => void;
  setConnected: (c: boolean) => void;
  setConnectionState: (
    phase: ConnectionPhase,
    details?: Partial<Omit<ConnectionStateDetails, "phase">>,
  ) => void;
  setLastUsage: (u: ChatSlice["lastUsage"]) => void;
  ensureSideChat: (id: string, owner?: { workspaceRoot: string; permissionMode: PermissionMode; parentConversationId?: string }) => void;
  removeSideChat: (id: string) => void;
  setSideChatDraft: (id: string, draft: string, richDraft?: import("../composer/rich-composer-document").RichComposerDocument) => void;
  startSideChatMessage: (
    id: string,
    content: string,
    messageIds: { assistantMessageId: string; userMessageId: string },
  ) => void;
}

export type PendingToolCallResume = {
  mcpApp?: ToolCallRecord["mcpApp"];
  mcp_app?: ToolCallRecord["mcpApp"];
  mcpAppResourceUri?: string;
  mcp_app_resource_uri?: string;
  mcpAppUi?: ToolCallRecord["mcpAppUi"];
  mcp_app_ui?: ToolCallRecord["mcpAppUi"];
  id: string;
  name: string;
  args: Record<string, unknown>;
  command_actions?: ToolCallRecord["commandActions"];
  commandActions?: ToolCallRecord["commandActions"];
  status?: ToolCallRecord["status"] | "completed" | "error" | "cancelled" | "waiting_approval";
  transition?: string;
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
  finished_at?: number;
  finishedAt?: number;
  duration_ms?: number;
  durationMs?: number;
  waiting_on?: string;
  waitingOn?: string;
  blocking_reason?: string;
  blockingReason?: string;
  outputPreview?: string;
  stdoutPreview?: string;
  stderrPreview?: string;
  exitCode?: number;
  recipientId?: string;
  recipientIds?: string[];
};

// ── Composer Slice ────────────────────────────────────────────────

export type PermissionMode = "confirm" | "plan" | "auto" | "bypass";
export type AgentMode = "build" | "plan" | "review" | "explore";

export interface ComposerAttachment {
  id: string;
  draftId?: string;
  name: string;
  type: string;
  size: number;
  status: "uploading" | "ready" | "error";
  progress?: number;
  uploadPhase?: "uploading" | "processing";
  conversationId?: string;
  workspaceRoot?: string;
  turnId?: string;
  messageId?: string;
  artifactId?: string;
  docId?: string;
  attachment?: Record<string, unknown>;
  dataUrl?: string;
  error?: string;
  inputSource?: "pasted_text" | "upload";
  sourceCharCount?: number;
  localFile?: File;
  remoteSourcePath?: string;
}

export interface ComposerQuote {
  id: string;
  role: MessageRole;
  content: string;
}

export interface ComposerSlice {
  queuedEdit?: import("./composer-drafts").QueuedMessageEdit;
  retainedInputEdit?: import("./composer-drafts").RetainedInputEdit;
  goalMode: boolean;
  workspaceComposerDrafts: Record<string, import("./composer-drafts").ComposerDraft>;
  composerDraftsReady: boolean;
  composerDraftStoragePending: boolean;
  composerDraftStorageError: string | null;
  composerDraftRevision: number;
  draft: string;
  richDraft?: import("../composer/rich-composer-document").RichComposerDocument;
  attachments: ComposerAttachment[];
  quotedMessage: ComposerQuote | null;
  permissionMode: PermissionMode;
  pendingPermissionModeChanges: Record<string, { workspaceRoot: string; confirmation: Promise<boolean> }>;
  agentMode: AgentMode;
  pendingAgentModeChanges: Record<string, { commandId: string; mode: AgentMode }>;
  latestAgentModeCommandIds: Record<string, string>;
  effortLevel: EffortLevel;
  prMonitor: PRMonitorState | null;
  prStatusIssue: { message: string; code: string } | null;
  actionChip: { label: string; description?: string } | null;
  mentionResults: { path: string; name: string; kind: "file" | "folder"; score?: number }[];
  selectedMentions: Array<FileContextRef | PluginContextRef | BrowserAnnotationContextRef>;
  selectedSkills: SkillContextRef[];
  slashPanelOpen: boolean;
  mentionPanelOpen: boolean;
  setDraft: (d: string, richDraft?: import("../composer/rich-composer-document").RichComposerDocument) => void;
  setQuotedMessage: (message: ComposerQuote) => void;
  clearQuotedMessage: () => void;
  addAttachment: (a: ComposerAttachment) => void;
  updateAttachment: (id: string, patch: Partial<ComposerAttachment>) => void;
  removeAttachment: (id: string) => void;
  clearAttachments: () => void;
  setPermissionMode: (m: PermissionMode) => Promise<boolean>;
  setAgentMode: (m: AgentMode, conversationId?: string) => Promise<boolean>;
  syncConversationAgentMode: (m: AgentMode, conversationId: string, commandId?: string, revision?: number) => void;
  setEffortLevel: (e: EffortLevel) => Promise<boolean>;
  setModelSelection: (selection: { mode: "model"; model: string } | { mode: "default" }) => Promise<boolean>;
  setPRMonitor: (pr: PRMonitorState | null) => void;
  setPRStatusIssue: (issue: ComposerSlice["prStatusIssue"]) => void;
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
  threadId: string;
  turnId: string;
  plan: TurnPlanStep[];
  explanation?: string;
}

export interface SubagentMessageState {
  messageId: string;
  senderId: string;
  recipientId: string;
  content: string;
  createdAt: number;
  seq?: number;
  senderMailboxEpoch?: number;
  recipientMailboxEpoch?: number;
  deliveryStatus?: "sending" | "sent" | "failed";
}

export interface SubagentState {
  workspaceRoot?: string;
  id: string;
  model?: string;
  provider?: string;
  reasoningEffort?: string;
  role: string;
  status: "pending" | "running" | "blocked" | "done" | "partial" | "cancelled" | "error";
  cleanupPending?: boolean;
  cleanupReason?: string;
  agentPath?: string;
  mailboxEpoch?: number;
  summary?: string;
  parentRunId?: string;
  turnId?: string;
  taskId?: string;
  dependsOn?: string[];
  blockedBy?: string[];
  objective?: string;
  currentActivity?: string;
  waitingOn?: string;
  lastProgressAt?: number;
  acceptedAt?: number;
  startedAt?: number;
  completedAt?: number;
  background?: boolean;
  needsInput?: boolean;
  teammateName?: string;
  displayName?: string;
  teamName?: string;
  awaitingPlanApproval?: boolean;
  activePlanRequestId?: string;
  isIdle?: boolean;
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
  transcriptMessages?: ChatMessage[];
  transcriptSeq?: number;
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
  turnDiffs: Record<string, TurnDiffState>;
  todos: TodoItem[];
  subagents: SubagentState[];
  agentProgress: AgentProgressEntry[];
  conversationAgentStates: Record<string, ConversationAgentState>;
  runtimeSession: RuntimeSessionSnapshot | null;
  runtimeCapabilities: AgentCapabilitiesPayload | null;
  budgetBuckets: BudgetBucket[];
  totalBudgetPercent: number;
  setPlan: (p: PlanState | null, conversationId?: string) => void;
  setTurnDiff: (conversationId: string, diff: TurnDiffState | null) => void;
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
  finishAgentProgress: (conversationId?: string, status?: "completed" | "partial" | "failed") => void;
  clearAgentProgress: (conversationId?: string) => void;
  snapshotAgentState: (conversationId?: string) => void;
  restoreAgentState: (conversationId?: string) => void;
  clearConversationAgentState: (conversationId: string) => void;
  setBudget: (buckets: BudgetBucket[], total: number) => void;
}

// ── Approval Slice ───────────────────────────────────────────────

export interface PendingApproval {
  requestId: string;
  conversationId?: string;
  turnId?: string;
  messageId?: string;
  taskId?: string;
  toolName: string;
  requestDigest?: string;
  command?: string[];
  displayCommand?: string;
  cwd?: string;
  approvalReason?: string | null;
  proposedExecpolicyAmendment?: string[] | null;
  availableDecisions?: import("../protocol/common-types").ControlCanUseToolRequest["available_decisions"];
  networkApprovalContext?: import("../protocol/common-types").NetworkApprovalContext;
  proposedNetworkPolicyAmendments?: import("../protocol/common-types").NetworkPolicyAmendment[];
  args: Record<string, unknown>;
  sourceAgent?: string;
  sourceThread?: string;
  sourceTool?: string;
  networkUnisolated?: boolean;
  expiresAt?: number;
  status?: "pending" | "submitted" | "error";
  error?: string;
  permissionRequest?: {
    permissions: import("../protocol/common-types").AdditionalPermissionProfile;
    reason?: string;
    cwd?: string;
    environmentId?: string;
  };
  stdinAction?: Record<string, unknown>;
  mcpApproval?: import("../protocol/common-types").ControlCanUseToolRequest["mcp_approval"];
}

export interface PendingDiffReview {
  requestId: string;
  conversationId?: string;
  turnId?: string;
  messageId?: string;
  taskId?: string;
  diff: string;
  filePath?: string;
  sourceAgent?: string;
  sourceThread?: string;
  sourceTool?: string;
  expiresAt?: number;
  reviewState?: DiffReviewState;
}

export interface PendingAskUserOption {
  label: string;
  value: string;
  description?: string;
}

/** A teammate plan the leader must approve before the teammate may implement.
 *
 * Answered with the subagent.plan_review command instead of a text answer, so
 * the prompt carries the teammate identity the runtime needs to route it. */
export interface PendingSubagentPlanReview {
  subagentId: string;
  teammateName?: string;
  teamName?: string;
  plan_file_path?: string;
  planContent?: string;
}

export interface PendingAskUser {
  requestId: string;
  conversationId?: string;
  turnId?: string;
  messageId?: string;
  taskId?: string;
  status?: "submitted" | "error";
  error?: string;
  prompt?: string;
  question: string;
  provider?: string;
  promptType?: "text" | "secret" | "select" | "manual_code";
  placeholder?: string;
  allowEmpty?: boolean;
  allowCustom?: boolean;
  secret?: boolean;
  expiresAt?: number;
  inputSchema?: Record<string, unknown>;
  options?: PendingAskUserOption[];
  planReview?: PendingSubagentPlanReview;
  requestUserInput?: {
    toolCallId: string;
    questions: import("../protocol/common-types").RequestUserInputQuestion[];
    isBlocking: boolean;
  };
  elicitation?: {
    mode: "form" | "url";
    url?: string;
    elicitationId?: string;
    serverName?: string;
    meta?: Record<string, unknown>;
  };
}

export interface UserInputQuestionDraft {
  questionIndex: number;
  answers: Record<string, { selectedOptionId: string | null; freeformText: string }>;
}

export interface AgentPromptDraft {
  requestId: string;
  conversationId?: string;
  inputOwner?: { turnId?: string; messageId?: string; taskId?: string };
  answer?: string;
  elicitationValues?: Record<string, string | boolean | string[] | undefined>;
  selectedOption?: number | null;
  initialPlan?: string;
  plan?: string;
  editing?: boolean;
  planExpanded?: boolean;
  rejecting?: boolean;
  rejectionFeedback?: string;
  amending?: boolean;
  feedback?: string;
  userInputDraft?: UserInputQuestionDraft;
  planImplementationSettled?: boolean;
}

export interface ApprovalSlice {
  promptDrafts: Record<string, AgentPromptDraft>;
  updatePromptDraft: (request: Pick<AgentPromptDraft, "requestId" | "conversationId" | "inputOwner">, patch: Partial<AgentPromptDraft>) => void;
  pendingApproval: PendingApproval | null;
  approvalQueue: PendingApproval[];
  pendingDiffReview: PendingDiffReview | null;
  diffReviewQueue: PendingDiffReview[];
  pendingAskUser: PendingAskUser | null;
  askUserQueue: PendingAskUser[];
  setApproval: (a: PendingApproval) => void;
  markApprovalSubmitted: (requestId: string, owner?: import("../protocol/prompt-responses").PromptOwner) => void;
  markApprovalError: (requestId: string, error: string, owner?: import("../protocol/prompt-responses").PromptOwner) => void;
  clearApproval: (requestId?: string, owner?: import("../protocol/prompt-responses").PromptOwner) => void;
  clearApprovals: (requestIds: string[], owner?: import("../protocol/prompt-responses").PromptOwner) => void;
  setDiffReview: (d: PendingDiffReview) => void;
  updateDiffReviewStatus: (requestId: string, patch: Pick<DiffReviewState, "status" | "error">, owner?: import("../protocol/prompt-responses").PromptOwner) => void;
  clearDiffReview: (requestId?: string, owner?: import("../protocol/prompt-responses").PromptOwner) => void;
  clearDiffReviews: (requestIds: string[], owner?: import("../protocol/prompt-responses").PromptOwner) => void;
  setAskUser: (a: PendingAskUser) => void;
  updateAskUserStatus: (requestId: string, patch: Pick<PendingAskUser, "status" | "error">, owner?: import("../protocol/prompt-responses").PromptOwner) => void;
  clearAskUser: (requestId?: string, conversationId?: string, inputOwner?: AgentPromptDraft["inputOwner"]) => void;
  clearAskUsers: (requestIds: string[], conversationId?: string, inputOwner?: AgentPromptDraft["inputOwner"]) => void;
}

// ── Control-plane projection slice ───────────────────────────────

export interface ConversationHydrationProjection {
  isHydrating: boolean;
  updatedAt: number;
}

export interface PermissionRuleProjection {
  pattern?: string;
  source: string;
  level?: string;
  tool?: string;
  ruleContent?: string;
  behavior?: string;
  destination?: string;
}

export interface PermissionRulesProjection {
  sessionId: string;
  conversationId: string;
  source: string;
  mode: string;
  contextSource: string;
  systemDeny: PermissionRuleProjection[];
  sessionDeny: PermissionRuleProjection[];
  sessionOverrides: PermissionRuleProjection[];
  sessionPromptRules: PermissionRuleProjection[];
  updatedAt: number;
}

export interface CheckpointProjectionRecord {
  id: string;
  conversationId: string;
  sessionId: string;
  toolCallId: string;
  toolName: string;
  workspaceRoot: string;
  paths: string[];
  createdAt: string;
  metadata: Record<string, unknown>;
}

export interface CheckpointCollectionProjection {
  conversationId: string;
  workspaceRoot: string;
  checkpoints: CheckpointProjectionRecord[];
  updatedAt: number;
}

export interface RunCheckpointProjectionRecord {
  runId?: string;
  sessionId?: string;
  conversationId?: string;
  iteration?: number;
  iterations?: number;
  stoppedReason?: string | null;
  createdAt?: number;
  timestamp?: number;
}

export interface RunCheckpointCollectionProjection {
  sessionId: string;
  conversationId: string;
  workspaceRoot: string;
  checkpoints: RunCheckpointProjectionRecord[];
  runs: Record<string, unknown>[];
  subagents: Record<string, unknown>[];
  updatedAt: number;
}

export interface CheckpointResumeProjection {
  resumed: boolean;
  sessionId?: string;
  conversationId: string;
  workspaceRoot: string;
  checkpointRunId?: string;
  iteration?: number;
  stoppedReason?: string | null;
  message?: string;
  updatedAt: number;
}

export interface RecentWorkspaceProjection {
  path: string;
  name: string;
  projectType: string;
  lastOpened: number;
  sourceFolders?: string[];
  managedWorkspaceRoot?: string;
  pinned?: boolean;
  appearance?: import("../lib/project-appearance").ProjectAppearance | null;
  named?: boolean;
}

export interface GuidelinesReloadProjection {
  conversationId: string;
  workspaceRoot: string;
  path?: string;
  message: string;
  cacheCleared: boolean;
  effectiveFrom: "next_turn" | string;
  updatedAt: number;
}

export interface ProviderOAuthFlowLink {
  url: string;
  label?: string;
}

export interface ProviderOAuthFlowProjection {
  conversationId: string;
  provider: string;
  phase: "auth_url" | "device_code" | "info" | "progress" | "error";
  url?: string;
  instructions?: string;
  userCode?: string;
  verificationUri?: string;
  intervalSeconds?: number;
  expiresInSeconds?: number;
  expiresAt?: number;
  message?: string;
  links?: ProviderOAuthFlowLink[];
  updatedAt: number;
  eventSeq?: number;
}

export interface ControlPlaneSlice {
  conversationHydration: Record<string, ConversationHydrationProjection>;
  permissionRulesByConversation: Record<string, PermissionRulesProjection>;
  checkpointsByConversation: Record<string, CheckpointCollectionProjection>;
  runCheckpointsByConversation: Record<string, RunCheckpointCollectionProjection>;
  checkpointResumeByConversation: Record<string, CheckpointResumeProjection>;
  guidelineReloadsByConversation: Record<string, GuidelinesReloadProjection>;
  providerOAuthFlowsByConversation: Record<string, Record<string, ProviderOAuthFlowProjection>>;
  recentWorkspaces: RecentWorkspaceProjection[];
  setConversationHydration: (conversationId: string, isHydrating: boolean, updatedAt?: number) => void;
  setPermissionRulesProjection: (projection: PermissionRulesProjection) => void;
  recordCheckpointProjection: (checkpoint: CheckpointProjectionRecord, updatedAt?: number) => void;
  setCheckpointCollectionProjection: (projection: CheckpointCollectionProjection) => void;
  setRunCheckpointCollectionProjection: (projection: RunCheckpointCollectionProjection) => void;
  setCheckpointResumeProjection: (projection: CheckpointResumeProjection) => void;
  setGuidelinesReloadProjection: (projection: GuidelinesReloadProjection) => void;
  setProviderOAuthFlow: (projection: ProviderOAuthFlowProjection) => void;
  clearProviderOAuthFlow: (conversationId: string, provider?: string) => void;
  setRecentWorkspaces: (workspaces: RecentWorkspaceProjection[]) => void;
  clearConversationControlPlaneState: (conversationId: string) => void;
}

// ── Inspector Slice ──────────────────────────────────────────────

export type InspectorTargetKind =
  | "message"
  | "tool_call"
  | "artifact"
  | "file"
  | "diff"
  | "subagent"
  | "budget"
  | "provider"
  | "cache"
  | "permission"
  | "checkpoint"
  | "workspace"
  | "guidelines"
  | "session";

export interface InspectorEntry {
  targetKind: InspectorTargetKind;
  targetId: string;
  conversationId?: string;
  payload: Record<string, unknown>;
  timestamp: number;
}

export interface InspectorSlice {
  inspectorEntries: InspectorEntry[];
  inspectorFocus: { kind: string; id: string; conversationId?: string } | null;
  addInspectorEntry: (entry: InspectorEntry) => void;
  addInspectorEntries: (entries: InspectorEntry[]) => void;
  setInspectorFocus: (focus: InspectorSlice["inspectorFocus"]) => void;
  clearInspector: () => void;
}

// ── Editor Slice ─────────────────────────────────────────────────

export interface EditorTab {
  workspaceOwner?: import("../protocol/workspace-owner").WorkspaceRequestOwner;
  language?: string;
  pinned?: boolean;
  preview?: boolean;
  lastActivated?: number;
  id: string;
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
  readOnly?: boolean;
  draftRestorePending?: boolean;
  recoveryPending?: boolean;
  recoveryPath?: string;
  draftRestored?: boolean;
  pendingBufferTransactions?: import("../panels/applyWorkspaceBufferEdits").WorkspaceBufferTransaction[];
}

export interface EditorSlice {
  inlineCompletionUsage: { requests: number; inputTokens: number; outputTokens: number; lastError: string; pending: boolean };
  workbenchPreferences: import("../lib/workbench-preferences").WorkbenchPreferences;
  setWorkbenchPreferences: (patch: Partial<import("../lib/workbench-preferences").WorkbenchPreferences>) => void;
  pinEditorTab: (path: string, pinned: boolean) => void;
  keepEditorTab: (path: string) => void;
  editorTabs: EditorTab[];
  activeTabPath: string | null;
  editorExplorerOpen: boolean;
  setEditorExplorerOpen: (open: boolean) => void;
  workspaceSearchOpen: boolean;
  openWorkspaceSearch: () => void;
  closeWorkspaceSearch: () => void;
  queueEditorBufferTransaction: (path: string, transaction: import("../panels/applyWorkspaceBufferEdits").WorkspaceBufferTransaction, original: string, contentHash: string, workspaceRoot: string) => void;
  consumeEditorBufferTransactions: (path: string, workspaceRoot: string) => void;
  agentEditReviewKept: Record<string, string[]>;
  keepAgentEditBlocks: (scope: string, blockKeys: string[]) => void;
  openEditorTab: (path: string, options?: { activate?: boolean; preview?: boolean }) => void;
  closeEditorTab: (path: string) => void;
  closeOtherEditorTabs: (path: string) => void;
  closeAllEditorTabs: () => void;
  renameEditorPath: (path: string, newPath: string, workspaceRoot: string) => void;
  setActiveTab: (path: string) => void;
  setEditorTabLanguage: (path: string, language?: string) => void;
  updateTabContent: (path: string, content: string) => void;
  adoptEditorModelChanges: (changes: Array<{ path: string; content: string; original: string; contentHash: string; sizeBytes?: number }>, workspaceRoot: string) => void;
  markTabLoaded: (
    path: string,
    content: string,
    error?: string | null,
    contentHash?: string,
    meta?: Pick<EditorTab, "largeFile" | "loadWarning" | "sizeBytes" | "readOnly" | "workspaceOwner">,
  ) => void;
  markTabSaved: (path: string, savedContent: string, contentHash?: string, sizeBytes?: number, workspaceRoot?: string) => void;
  markTabExternalChanged: (path: string, options?: { workspaceRoot?: string; changed?: boolean }) => void;
}

// ── Combined Store ────────────────────────────────────────────────

export type AppStore = UISlice & WorkspaceSlice & ChatSlice & ComposerSlice & AgentSlice & ApprovalSlice & ControlPlaneSlice & InspectorSlice & EditorSlice;
