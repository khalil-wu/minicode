import { extractInlineCitationIndexes } from "../chat/citationProjection";
import { shouldShowInActivity } from "../lib/display-intent";
import { getContentBlocks, getToolCallsFromMessage } from "../lib/content-blocks";
import { planStepProgressStatus, shouldSurfacePlanProgress } from "../lib/planVisibility";
import { isAgentControlToolName } from "../lib/tool-call-reducer";
import type {
  AgentProgressEntry,
  ArtifactContentState,
  ArtifactPreview,
  BackgroundTaskEntry,
  BrowserAnnotation,
  ChatMessage,
  Citation,
  ContextUsage,
  ConversationGoal,
  MessageAttachmentRef,
  PlanState,
  PreviewLaunchProcessInfo,
  PreviewServerInfo,
  PreviewVerificationInfo,
  ReplyAttachmentMeta,
  ScheduledTaskEntry,
  SubagentState,
  TerminalSessionInfo,
  TerminalSnapshotInfo,
  TodoItem,
  WorkspaceGitState,
} from "../stores/types";

export type ActivityProgressStatus = "pending" | "running" | "completed" | "failed";

export interface ActivityProgressItem {
  id: string;
  label: string;
  detail?: string;
  status: ActivityProgressStatus;
}

export interface ActivityOutputItem {
  id: string;
  label: string;
  kind: string;
  detail?: string;
  artifactId?: string;
  url?: string;
  path?: string;
  mediaType?: string;
}

export interface ActivityBrowserItem {
  id: string;
  label: string;
  url: string;
  host: string;
  detail?: string;
  status: "idle" | "running" | "verified" | "failed";
}

export interface ActivityBrowserAnnotationItem {
  id: string;
  label: string;
  url: string;
  host: string;
  note: string;
  selector?: string;
  xPercent?: number;
  yPercent?: number;
  title?: string;
  detail?: string;
  createdAt: number;
  screenshotDetail?: string;
}

export interface ActivitySourceItem {
  id: string;
  kind: "web" | "file";
  label: string;
  title?: string;
  url?: string;
  path?: string;
  host?: string;
}

export interface ActivitySummaryItem {
  id: string;
  label: string;
  detail?: string;
  kind: "goal" | "context";
  status?: "running" | "completed" | "failed" | "info";
}

export interface ActivityWorkspaceItem {
  id: string;
  label: string;
  detail?: string;
  kind: "branch" | "worktree" | "workspace";
  path?: string;
  status?: "running" | "completed" | "failed" | "info";
}

export interface ActivityAttachmentItem {
  id: string;
  messageId: string;
  label: string;
  kind: "image" | "document" | "file";
  detail?: string;
  artifactId?: string;
  docId?: string;
  path?: string;
  mediaType?: string;
}

export interface RuntimeItem {
  id: string;
  label: string;
  kind: "terminal" | "background-command" | "preview" | "agent" | "automation";
  detail?: string;
  status: "running" | "completed" | "failed" | "idle";
  terminalId?: string;
  automationId?: string;
  previewId?: string;
  agentId?: string;
  startedAt?: number;
  attention?: boolean;
}

export type ActivityRunItem = RuntimeItem;

export interface ActivitySidebarState {
  hasConversation: boolean;
  summary: ActivitySummaryItem[];
  workspace: ActivityWorkspaceItem[];
  progress: ActivityProgressItem[];
  output: ActivityOutputItem[];
  browser: ActivityBrowserItem[];
  sources: ActivitySourceItem[];
  attachments: ActivityAttachmentItem[];
  runs: ActivityRunItem[];
  browserAnnotations: ActivityBrowserAnnotationItem[];
  isEmpty: boolean;
}

export interface ActivitySidebarStateInput {
  conversationId: string | null;
  messages: ChatMessage[];
  todos: TodoItem[];
  plan: PlanState | null;
  isStreaming?: boolean;
  agentProgress: AgentProgressEntry[];
  activeGoal?: ConversationGoal | null;
  contextUsage?: ContextUsage | null;
  currentModel?: string;
  currentProvider?: string;
  workspaceGit?: WorkspaceGitState | null;
  workingDirectory?: string;
  livePreviewUrl: string | null;
  previewArtifact: ArtifactContentState | null;
  previewVerification: PreviewVerificationInfo | null;
  previewServers: PreviewServerInfo[];
  previewLaunchProcesses: PreviewLaunchProcessInfo[];
  terminalSnapshots?: Record<string, TerminalSnapshotInfo>;
  terminalSessions?: TerminalSessionInfo[];
  activeTerminalSessionId?: string | null;
  backgroundTasks?: BackgroundTaskEntry[];
  subagents?: SubagentState[];
  scheduledTasks?: ScheduledTaskEntry[];
  browserAnnotations?: BrowserAnnotation[];
}

export function buildActivitySidebarState(input: ActivitySidebarStateInput): ActivitySidebarState {
  if (!input.conversationId) {
    return emptyState(false);
  }

  const summary = buildSummary(input);
  const workspace = buildWorkspace(input);
  const progress = buildProgress(input);
  const output = buildOutput(input.messages, input.previewArtifact);
  const browser = buildBrowser(input);
  const sources = buildSources(input.messages);
  const attachments = buildAttachments(input.messages);
  const runs = buildRuns(input);
  const browserAnnotations = buildBrowserAnnotations(input);

  return {
    hasConversation: true,
    summary,
    workspace,
    progress,
    output,
    browser,
    sources,
    attachments,
    runs,
    browserAnnotations,
    isEmpty: summary.length === 0 &&
      workspace.length === 0 &&
      progress.length === 0 &&
      output.length === 0 &&
      browser.length === 0 &&
      sources.length === 0 &&
      attachments.length === 0 &&
      runs.length === 0 &&
      browserAnnotations.length === 0,
  };
}

function emptyState(hasConversation: boolean): ActivitySidebarState {
  return {
    hasConversation,
    summary: [],
    workspace: [],
    progress: [],
    output: [],
    browser: [],
    sources: [],
    attachments: [],
    runs: [],
    browserAnnotations: [],
    isEmpty: true,
  };
}

function buildSummary(input: ActivitySidebarStateInput): ActivitySummaryItem[] {
  const items: ActivitySummaryItem[] = [];

  if (input.activeGoal?.text) {
    items.push({
      id: "goal",
      label: input.activeGoal.text,
      detail: input.activeGoal.status === "paused" ? "Paused goal" : "Active goal",
      kind: "goal",
      status: input.activeGoal.status === "paused" ? "info" : "running",
    });
  }

  if (input.contextUsage) {
    const percent = input.contextUsage.limit > 0
      ? Math.round((input.contextUsage.used / input.contextUsage.limit) * 100)
      : 0;
    items.push({
      id: "context",
      label: `${formatNumber(input.contextUsage.used)} / ${formatNumber(input.contextUsage.limit)} tokens`,
      detail: input.contextUsage.compactSummary || `${percent}% context used`,
      kind: "context",
      status: percent >= 90 ? "failed" : percent >= 75 ? "running" : "info",
    });
  }

  return items;
}

function buildWorkspace(input: ActivitySidebarStateInput): ActivityWorkspaceItem[] {
  const items: ActivityWorkspaceItem[] = [];
  const git = input.workspaceGit;

  if (git?.branch) {
    items.push({
      id: "branch",
      label: git.branch,
      detail: "Git branch",
      kind: "branch",
      status: "info",
    });
  }

  if (git) {
    const nonGitWorkspace = isNonGitWorkspaceError(git.error);
    items.push({
      id: "worktree",
      label: git.isWorktree ? "Isolated worktree" : "Main workspace",
      detail: git.worktreeCount || git.isolatedCount
        ? `${git.worktreeCount ?? 0} worktrees, ${git.isolatedCount ?? 0} isolated`
        : nonGitWorkspace
          ? git.currentPath || "Local folder"
          : git.error || git.currentPath,
      kind: "worktree",
      path: git.currentPath,
      status: git.error && !nonGitWorkspace ? "failed" : git.isWorktree ? "running" : "info",
    });
  } else if (input.workingDirectory) {
    items.push({
      id: "workspace",
      label: basename(input.workingDirectory),
      detail: input.workingDirectory,
      kind: "workspace",
      path: input.workingDirectory,
      status: "info",
    });
  }

  return items;
}

function buildProgress(input: ActivitySidebarStateInput): ActivityProgressItem[] {
  const progress: ActivityProgressItem[] = [];

  if (input.todos.length > 0) {
    const currentTodo =
      input.todos.find((todo) => todo.status === "blocked") ||
      input.todos.find((todo) => todo.status === "in_progress") ||
      input.todos.find((todo) => todo.status === "pending");
    if (currentTodo) {
      progress.push({
        id: currentTodo.id,
        label: currentTodo.status === "in_progress" && currentTodo.activeForm ? currentTodo.activeForm : currentTodo.content,
        detail: currentTodo.status === "blocked" ? "Needs attention" : undefined,
        status: todoStatus(currentTodo.status, Boolean(input.isStreaming)),
      });
    }
  } else if (shouldSurfacePlanProgress(input.plan)) {
    const currentStep = input.plan.steps.find((step) => step.status === "running")
      || input.plan.steps.find((step) => step.status === "failed")
      || input.plan.steps.find((step) => step.status === "pending");
    if (currentStep) {
      progress.push({
        id: currentStep.id || "plan-step-current",
        label: currentStep.title,
        detail: currentStep.detail,
        status: planStepProgressStatus(input.plan, currentStep, Boolean(input.isStreaming)),
      });
    }
  }

  const conversationKey = input.conversationId || "__active__";
  const scopedProgress = input.agentProgress
    .filter((entry) =>
      (entry.conversationId === conversationKey || entry.conversationId === "__active__" || !entry.conversationId) &&
      entry.visibility !== "debug" &&
      !isDelegatedAgentProgress(entry) &&
      !isInternalAgentPhaseProgress(entry) &&
      shouldShowInActivity(entry) &&
      (entry.stage === "planning" || entry.stage === "verification" || entry.stage === "final" || entry.stage === "approval" || entry.stage === "tool"),
    );
  const phaseRunIds = new Set(
    scopedProgress
      .map((entry) => agentPhaseRunId(entry.id))
      .filter((runId): runId is string => Boolean(runId)),
  );
  const compactProgress = serializeMainAgentProgress(
    scopedProgress
      .filter((entry) => {
        const runId = agentRunId(entry.id);
        return !runId || !phaseRunIds.has(runId);
      })
      .slice(-8),
  ).slice(-4);

  for (const entry of compactProgress) {
    const label = entry.summary || entry.message || entry.label || "";
    if (!label.trim()) continue;
    progress.push({
      id: entry.id,
      label,
      detail: entry.detail,
      status: progressStatus(entry.status, Boolean(input.isStreaming)),
    });
  }

  return dedupeBy(progress, (item) => item.id);
}

function isDelegatedAgentProgress(entry: AgentProgressEntry): boolean {
  if (isAgentControlToolName(entry.toolName || "")) return true;
  const text = [entry.id, entry.label, entry.message, entry.summary]
    .filter(Boolean)
    .join(" ");
  return /(?:subagent|delegated task|子代理|子任务)/i.test(text);
}

function agentRunId(id: string): string {
  return id.match(/^agent-run:([^:]+)/)?.[1] ?? "";
}

function agentPhaseRunId(id: string): string {
  return id.match(/^agent-phase:([^:]+)/)?.[1] ?? "";
}

function isInternalAgentPhaseProgress(entry: AgentProgressEntry): boolean {
  if (entry.requiresAttention) return false;
  if (entry.visibility === "compact") return false;
  const id = String(entry.id || "");
  const label = String(entry.summary || entry.message || entry.label || "").trim().toLowerCase();
  if (/^agent-run:/.test(id) || /^agent-phase:/.test(id)) return true;
  return [
    "agent run started",
    "agent run completed",
    "preparing agent context",
    "model deciding next action",
  ].includes(label);
}

function serializeMainAgentProgress(entries: AgentProgressEntry[]): AgentProgressEntry[] {
  let lastRunningIndex = -1;
  entries.forEach((entry, index) => {
    if (entry.status === "running") lastRunningIndex = index;
  });
  if (lastRunningIndex < 0) return entries;
  return entries.map((entry, index) =>
    index < lastRunningIndex && entry.status === "running"
      ? { ...entry, status: "completed" }
      : entry,
  );
}

function buildOutput(messages: ChatMessage[], previewArtifact: ArtifactContentState | null): ActivityOutputItem[] {
  const items: ActivityOutputItem[] = [];
  const seen = new Set<string>();

  for (const message of messages) {
    for (const artifact of message.artifacts ?? []) {
      if (!artifact.artifactId || seen.has(artifact.artifactId)) continue;
      seen.add(artifact.artifactId);
      items.push(outputFromArtifact(artifact));
    }
  }

  if (previewArtifact?.artifactId && !seen.has(previewArtifact.artifactId)) {
    seen.add(previewArtifact.artifactId);
    items.unshift({
      id: previewArtifact.artifactId,
      label: previewArtifact.name || artifactFallbackLabel(undefined, previewArtifact.mediaType),
      kind: kindFromMediaType(previewArtifact.mediaType) || "artifact",
      detail: previewArtifact.mediaType,
      artifactId: previewArtifact.artifactId,
      url: previewArtifact.url,
      mediaType: previewArtifact.mediaType,
    });
  }

  return items.slice(-12).reverse();
}

function buildBrowser(input: ActivitySidebarStateInput): ActivityBrowserItem[] {
  if (!input.livePreviewUrl) return [];
  const url = input.livePreviewUrl;
  const server = input.previewServers.find((item) => item.url === url || sameHost(item.url, url));
  const process = input.previewLaunchProcesses.find((item) => item.url === url || sameHost(item.url, url));
  const verification = input.previewVerification?.url === url ? input.previewVerification : null;
  const status: ActivityBrowserItem["status"] = verification
    ? verification.ok ? "verified" : "failed"
    : process && ["starting", "running"].includes(process.status)
      ? "running"
      : "idle";
  const detail = verification
    ? verification.ok
      ? `${verification.status_code ?? "OK"} in ${verification.elapsed_ms}ms`
      : verification.error || `Failed${verification.status_code ? ` (${verification.status_code})` : ""}`
    : process
      ? process.status
      : server?.framework;

  return [{
    id: "live-preview",
    label: server?.name || process?.name || "Live preview",
    url,
    host: hostLabel(url),
    detail,
    status,
  }];
}

function buildBrowserAnnotations(input: ActivitySidebarStateInput): ActivityBrowserAnnotationItem[] {
  return (input.browserAnnotations ?? [])
    .slice(0, 10)
    .map((annotation) => {
      const coordinateLabel = browserAnnotationCoordinateLabel(annotation);
      const screenshotDetail = annotation.screenshotCapturedAt
        ? [
            `screenshot ${new Date(annotation.screenshotCapturedAt).toLocaleTimeString()}`,
            annotation.screenshotWidth && annotation.screenshotHeight
              ? `${annotation.screenshotWidth}x${annotation.screenshotHeight}`
              : "",
            coordinateLabel,
          ].filter(Boolean).join(" - ")
        : undefined;
      return {
        id: annotation.id,
        label: annotation.selector || coordinateLabel || annotation.title || "Page note",
        url: annotation.url,
        host: hostLabel(annotation.url),
        note: annotation.note,
        selector: annotation.selector,
        xPercent: annotation.xPercent,
        yPercent: annotation.yPercent,
        title: annotation.title,
        detail: annotation.title || annotation.url,
        createdAt: annotation.createdAt,
        screenshotDetail,
      };
    });
}

function browserAnnotationCoordinateLabel(annotation: Pick<BrowserAnnotation, "xPercent" | "yPercent">): string | undefined {
  const { xPercent, yPercent } = annotation;
  if (typeof xPercent !== "number" || typeof yPercent !== "number") return undefined;
  if (!Number.isFinite(xPercent) || !Number.isFinite(yPercent)) return undefined;
  return `Point ${Math.round(xPercent)}%, ${Math.round(yPercent)}%`;
}

function buildSources(messages: ChatMessage[]): ActivitySourceItem[] {
  const items: ActivitySourceItem[] = [];
  const seen = new Set<string>();
  const assistantMessages = messages.filter((message) => message.role === "assistant");

  for (const message of assistantMessages) {
    for (const record of getToolCallsFromMessage(message)) {
      if (!shouldShowInActivity(record)) continue;
      const webUrl = toolSourceUrl(record);
      if (webUrl && !seen.has(`web:${webUrl}`)) {
        seen.add(`web:${webUrl}`);
        items.push({
          id: `web:${webUrl}`,
          kind: "web",
          label: hostLabel(webUrl),
          title: record.displaySummary || record.summary,
          url: webUrl,
          host: hostLabel(webUrl),
        });
      }

    }

    const citedIndexes = extractInlineCitationIndexes(message.content || "");
    if (citedIndexes.size === 0) continue;
    const citations = (message.citations ?? []).filter((citation, index) =>
      citedIndexes.has(index + 1),
    );
    for (const citation of citations) {
      const url = citationUrl(citation);
      if (!url || seen.has(`web:${url}`)) continue;
      seen.add(`web:${url}`);
      items.push({
        id: `web:${url}`,
        kind: "web",
        label: citation.label || hostLabel(url),
        title: citation.title,
        url,
        host: hostLabel(url),
      });
    }
  }

  return items.slice(-8);
}

function buildAttachments(messages: ChatMessage[]): ActivityAttachmentItem[] {
  const items: ActivityAttachmentItem[] = [];
  const seen = new Set<string>();

  for (const message of messages) {
    for (const attachment of message.attachmentRefs ?? []) {
      const id = attachment.artifactId || attachment.docId || attachment.id || `${message.id}:${attachment.name}`;
      if (seen.has(id)) continue;
      seen.add(id);
      items.push(attachmentFromRef(message.id, attachment));
    }
    for (const attachment of message.replyAttachments ?? []) {
      const id = attachment.path || `${message.id}:reply:${items.length}`;
      if (seen.has(id)) continue;
      seen.add(id);
      items.push(attachmentFromReply(message.id, id, attachment));
    }
  }

  return items.slice(-12).reverse();
}

function buildRuns(input: ActivitySidebarStateInput): ActivityRunItem[] {
  const items: ActivityRunItem[] = [];
  if ((input.terminalSessions ?? []).length === 0 && input.activeTerminalSessionId) {
    const snapshot = input.terminalSnapshots?.[input.activeTerminalSessionId];
    if (snapshot) items.push({
      id: `terminal:${snapshot.id}`,
      terminalId: snapshot.id,
      kind: "terminal",
      label: snapshot.shell || "Terminal",
      detail: terminalDetail(snapshot),
      status: snapshot.status === "exited" ? "completed" : "running",
    });
  }
  for (const session of input.terminalSessions ?? []) {
    const snapshot = input.terminalSnapshots?.[session.id];
    items.push({
      id: `terminal:${session.id}`,
      terminalId: session.id,
      kind: "terminal",
      label: session.shell || "Terminal",
      detail: snapshot ? terminalDetail(snapshot) : session.cwd,
      status: session.status === "exited" ? (session.exitCode && session.exitCode !== 0 ? "failed" : "completed") : "running",
      startedAt: session.createdAt,
      attention: session.status === "exited" && Boolean(session.exitCode),
    });
  }

  for (const task of input.backgroundTasks ?? []) {
    items.push({
      id: `background:${task.id}`,
      kind: "background-command",
      label: cleanDisplayText(task.command) || "Background command",
      detail: task.exitCode == null ? undefined : `exit ${task.exitCode}`,
      status: task.status,
      startedAt: task.timestamp,
      attention: task.status === "failed",
    });
  }

  for (const process of input.previewLaunchProcesses ?? []) {
    const running = ["starting", "running", "ready"].includes(process.status);
    items.push({
      id: `preview:${process.id}`,
      previewId: process.id,
      kind: "preview",
      label: process.name || "Preview",
      detail: process.url || process.command,
      status: running ? "running" : process.status === "exited" ? "completed" : "failed",
      attention: process.status === "crashed" || process.status === "unhealthy",
    });
  }

  for (const agent of input.subagents ?? []) {
    const running = ["pending", "running", "blocked"].includes(agent.status);
    items.push({
      id: `agent:${agent.id}`,
      agentId: agent.id,
      kind: "agent",
      label: cleanDisplayText(agent.objective || agent.summary || agent.role) || "Subagent",
      detail: agent.currentActivity || agent.detail,
      status: running ? "running" : agent.status === "done" ? "completed" : "failed",
      startedAt: agent.lastProgressAt || agent.lastEventAt,
      attention: agent.status === "blocked" || agent.status === "partial" || agent.status === "error",
    });
  }

  for (const task of input.scheduledTasks ?? []) {
    items.push({
      id: `automation:${task.id}`,
      automationId: task.id,
      kind: "automation",
      label: cleanDisplayText(task.name) || "Automation",
      detail: task.schedule || task.prompt,
      status: task.enabled ? "idle" : "completed",
    });
  }

  return items
    .sort((a, b) => Number(b.status === "running") - Number(a.status === "running") || Number(b.attention) - Number(a.attention) || runtimeKindOrder(a.kind) - runtimeKindOrder(b.kind) || (b.startedAt ?? 0) - (a.startedAt ?? 0))
    .slice(0, 16);
}

const runtimeKindOrder = (kind: ActivityRunItem["kind"]): number =>
  ({ terminal: 0, agent: 1, preview: 2, "background-command": 3, automation: 4 })[kind];

function toolSourceUrl(record: ReturnType<typeof getToolCallsFromMessage>[number]): string {
  if (String(record.extractionStatus || "").toLowerCase() === "failed") return "";
  const candidate = record.sourceUrl || stringArg(record.args.url) || stringArg(record.args.source_url);
  if (!/^https?:\/\//i.test(candidate)) return "";
  const evidence = String(record.evidenceType || "").toLowerCase();
  if (evidence === "candidate") return "";
  if (evidence === "fetched") return candidate;
  if (/fetch/i.test(`${record.name} ${record.resultKind || ""}`)) return candidate;
  return "";
}

function toolSourcePath(record: ReturnType<typeof getToolCallsFromMessage>[number]): string {
  const candidate = pathArg(record.args.file_path ?? record.args.path ?? record.args.target ?? record.args.filename);
  if (!candidate || /^https?:\/\//i.test(candidate)) return "";
  if (!isFileEvidenceTool(record)) return "";
  return candidate;
}

function isFileEvidenceTool(record: ReturnType<typeof getToolCallsFromMessage>[number]): boolean {
  const name = String(record.name || "");
  if (/(?:write|edit|patch|delete|remove|create|move|rename|save|apply_patch|run_command|terminal|shell|bash|powershell|cmd)/i.test(name)) {
    return false;
  }
  if (/(?:read|grep|glob|search|list|find|scan|inspect|cat|select-string)/i.test(name)) {
    return true;
  }
  const kind = String(record.resultKind || record.activityKind || "");
  if (/(?:edit|command|terminal|process)/i.test(kind)) return false;
  return /(?:file|source|search)/i.test(kind);
}

function isNonGitWorkspaceError(error?: string): boolean {
  if (!error) return false;
  return /not\s+(?:a\s+)?git\s+(?:repository|repo)|outside\s+(?:a\s+)?git\s+(?:repository|repo)/i.test(error);
}

function stringArg(value: unknown): string {
  if (typeof value !== "string") return "";
  const trimmed = value.trim();
  return isPlaceholderText(trimmed) ? "" : trimmed;
}

function pathArg(value: unknown): string {
  const candidate = stringArg(value);
  if (!candidate) return "";
  if (/^(?:\$?null|undefined|none|nil)$/i.test(candidate)) return "";
  return candidate;
}

function attachmentFromRef(messageId: string, attachment: MessageAttachmentRef): ActivityAttachmentItem {
  const fallback = "附件";
  return {
    id: attachment.artifactId || attachment.docId || attachment.id || `${messageId}:${cleanDisplayText(attachment.name) || fallback}`,
    messageId,
    label: cleanDisplayText(attachment.name) || fallback,
    kind: attachment.kind,
    detail: attachment.mediaType || sizeLabel(attachment.sizeBytes),
    artifactId: attachment.artifactId,
    docId: attachment.docId,
    mediaType: attachment.mediaType,
  };
}

function attachmentFromReply(
  messageId: string,
  id: string,
  attachment: ReplyAttachmentMeta,
): ActivityAttachmentItem {
  return {
    id,
    messageId,
    label: cleanDisplayText(basename(attachment.path)) || "Attachment",
    kind: attachment.isImage ? "image" : "file",
    detail: sizeLabel(attachment.size),
    path: attachment.path,
    mediaType: attachment.isImage ? "image/*" : undefined,
  };
}

function terminalDetail(snapshot: TerminalSnapshotInfo): string {
  const output = snapshot.truncated ? "truncated" : `${snapshot.outputChars ?? snapshot.output.length} chars`;
  return [snapshot.cwd, snapshot.status || "running", output].filter(Boolean).join(" - ");
}

function basename(path: string): string {
  return path.replace(/\\/g, "/").split("/").pop() || path;
}

function outputFromArtifact(artifact: ArtifactPreview): ActivityOutputItem {
  const label = cleanDisplayText(artifact.summary) || artifactFallbackLabel(artifact.kind, artifact.mediaType);
  const path = artifact.kind === "file" ? pathArg(artifact.summary) : "";
  return {
    id: artifact.artifactId,
    label,
    kind: artifact.kind,
    detail: artifact.mediaType || sizeLabel(artifact.bytes),
    artifactId: artifact.artifactId,
    url: artifact.url,
    mediaType: artifact.mediaType,
    path: path || undefined,
  };
}

function artifactFallbackLabel(kind?: string, mediaType?: string): string {
  const normalized = `${kind || ""} ${mediaType || ""}`.toLowerCase();
  if (normalized.includes("image")) return "生成图片";
  if (normalized.includes("pdf")) return "生成的 PDF";
  if (normalized.includes("file") || normalized.includes("text")) return "生成文件";
  return "未命名产物";
}

function cleanDisplayText(value: unknown): string {
  if (typeof value !== "string") return "";
  const trimmed = value.trim();
  return isPlaceholderText(trimmed) ? "" : trimmed;
}

function isPlaceholderText(value: string): boolean {
  return /^(?:\$?null|undefined|none|nil)$/i.test(value.trim());
}

function todoStatus(status: TodoItem["status"], isLive: boolean): ActivityProgressStatus {
  if (status === "completed") return "completed";
  if (status === "in_progress") return isLive ? "running" : "pending";
  if (status === "blocked") return "failed";
  return "pending";
}

function progressStatus(status: AgentProgressEntry["status"], isLive: boolean): ActivityProgressStatus {
  if (status === "completed" || status === "info") return "completed";
  if (status === "running") return isLive ? "running" : "pending";
  return "failed";
}

function citationUrl(citation: Citation): string {
  const candidate = String(citation.url || citation.source || "").trim();
  return /^https?:\/\//i.test(candidate) ? candidate : "";
}

function hostLabel(url: string): string {
  try {
    return new URL(url).host.replace(/^www\./i, "");
  } catch {
    return url;
  }
}

function sameHost(left: string, right: string): boolean {
  return hostLabel(left) === hostLabel(right);
}

function kindFromMediaType(mediaType?: string): string {
  if (!mediaType) return "";
  if (mediaType.startsWith("image/")) return "image";
  if (mediaType === "application/pdf") return "pdf";
  if (mediaType.includes("json")) return "json";
  if (mediaType.startsWith("text/")) return "text";
  return mediaType;
}

function sizeLabel(bytes?: number): string | undefined {
  if (!bytes) return undefined;
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function formatNumber(value: number): string {
  return Number.isFinite(value) ? value.toLocaleString() : "0";
}

function dedupeBy<T>(items: T[], keyForItem: (item: T) => string): T[] {
  const seen = new Set<string>();
  const output: T[] = [];
  for (const item of items) {
    const key = keyForItem(item);
    if (seen.has(key)) continue;
    seen.add(key);
    output.push(item);
  }
  return output;
}
