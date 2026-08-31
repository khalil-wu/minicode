import { extractInlineCitationIndexes } from "../chat/citationProjection";
import { getContentBlocks, getToolCallsFromMessage } from "../lib/content-blocks";
import { planStepProgressStatus, shouldSurfacePlanProgress } from "../lib/planVisibility";
import { providerProgressLabel } from "../lib/provider-progress";
import {
  artifactFallbackLabel as projectionArtifactFallbackLabel,
  artifactMediaTypeForProjection,
  artifactSummaryForRecord,
  canonicalArtifactKind,
  cleanArtifactLabel,
  normalizeArtifactPreview,
} from "../lib/artifact-projection";
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
  ScheduledTaskRunEntry,
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
  retryAttempt?: number;
  maxRetries?: number;
  providerState?: AgentProgressEntry["providerState"];
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
  status: "running" | "stalled" | "completed" | "failed" | "cancelled" | "idle";
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
  scheduledTaskRuns?: ScheduledTaskRunEntry[];
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
      detail: input.activeGoal.status === "paused" ? "已暂停的目标" : "进行中的目标",
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
    const currentStep = input.plan.plan.find((step) => step.status === "in_progress")
      || input.plan.plan.find((step) => step.status === "pending");
    if (currentStep) {
      progress.push({
        id: `plan-step-${input.plan.plan.indexOf(currentStep)}`,
        label: currentStep.step,
        status: planStepProgressStatus(currentStep, Boolean(input.isStreaming)),
      });
    }
  }

  const conversationKey = input.conversationId?.trim();
  const scopedProgress = input.agentProgress
    .filter((entry) => Boolean(conversationKey) && entry.conversationId === conversationKey && entry.visibility !== "debug");
  const compactProgress = serializeMainAgentProgress(
    scopedProgress.slice(-8),
  ).slice(-4);

  for (const entry of compactProgress) {
    // Provider retry rows have typed terminal states as well as the running
    // reconnect ladder. Keep this label identical to the transcript timeline.
    const label = providerProgressLabel(entry) || (
      entry.id.startsWith("provider:")
        ? entry.message || entry.summary || entry.label || ""
        : entry.summary || entry.message || entry.label || ""
    );
    if (!label.trim()) continue;
    progress.push({
      id: entry.id,
      label,
      detail: formatProgressDetail(entry.detail),
      status: progressStatus(entry.status, Boolean(input.isStreaming)),
      ...(typeof entry.retryAttempt === "number" ? { retryAttempt: entry.retryAttempt } : {}),
      ...(typeof entry.maxRetries === "number" ? { maxRetries: entry.maxRetries } : {}),
      ...(entry.providerState ? { providerState: entry.providerState } : {}),
    });
  }

  return dedupeBy(progress, (item) => item.id);
}

function formatProgressDetail(detail?: string): string | undefined {
  const value = String(detail || "").trim();
  if (!value) return undefined;
  return value
    .replace(/duration_ms=(\d+)/gi, (_, raw: string) => `${formatDuration(Number(raw))}`)
    .replace(/provider_wait_ms=(\d+)/gi, (_, raw: string) => `${formatDuration(Number(raw))}`)
    .replace(/waiting_on=/gi, "等待：")
    .replace(/blocking_reason=/gi, "原因：");
}

function formatDuration(durationMs: number): string {
  if (!Number.isFinite(durationMs) || durationMs < 0) return "";
  if (durationMs < 1000) return `${Math.round(durationMs)} 毫秒`;
  const seconds = durationMs / 1000;
  return `${seconds >= 10 ? Math.round(seconds) : seconds.toFixed(1)} 秒`;
}

function serializeMainAgentProgress(entries: AgentProgressEntry[]): AgentProgressEntry[] {
  let lastRunningIndex = -1;
  entries.forEach((entry, index) => {
    if (entry.status === "running" && !entry.id.startsWith("provider:")) lastRunningIndex = index;
  });
  if (lastRunningIndex < 0) return entries;
  return entries.map((entry, index) =>
    index < lastRunningIndex && entry.status === "running" && !entry.id.startsWith("provider:")
      ? { ...entry, status: "completed" }
      : entry,
  );
}

export function buildOutput(messages: ChatMessage[], previewArtifact: ArtifactContentState | null): ActivityOutputItem[] {
  const items: ActivityOutputItem[] = [];
  const indexes = new Map<string, number>();

  const upsert = (item: ActivityOutputItem): void => {
    const artifactId = String(item.artifactId || item.id || "").trim();
    if (!artifactId) return;
    const existingIndex = indexes.get(artifactId);
    if (existingIndex === undefined) {
      indexes.set(artifactId, items.length);
      items.push({ ...item, id: artifactId, artifactId });
      return;
    }
    items[existingIndex] = mergeOutputItems(items[existingIndex], { ...item, id: artifactId, artifactId });
  };

  for (const message of messages) {
    for (const artifact of message.artifacts ?? []) {
      upsert(outputFromArtifact(artifact));
    }
    // Tool-owned artifacts (notably browser screenshots) are kept on the
    // tool record rather than assistant message.artifacts. Project metadata
    // through the same canonical path as persisted message artifacts.
    for (const record of getToolCallsFromMessage(message)) {
      const item = outputFromToolRecord(record);
      if (item) upsert(item);
    }
  }

  const previewId = String(previewArtifact?.artifactId || "").trim();
  if (previewArtifact && previewId) {
    const previewItem = outputFromPreviewArtifact(previewArtifact, previewId);
    const existingIndex = indexes.get(previewId);
    if (existingIndex === undefined) {
      indexes.set(previewId, items.length);
      items.push(previewItem);
    } else {
      const merged = mergeOutputItems(items[existingIndex], previewItem);
      items.splice(existingIndex, 1);
      // The preview is the most recent user-visible output. It must be moved
      // before the cap is applied, otherwise slice(-12) can discard it.
      items.push(merged);
    }
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
    if (task.conversationId !== input.conversationId) {
      continue;
    }
    items.push({
      id: `background:${task.id}`,
      kind: "background-command",
      label: cleanDisplayText(task.command) || "Background command",
      detail: backgroundTaskDetail(task),
      status: task.status,
      startedAt: task.timestamp,
      attention: task.status === "failed" || task.status === "stalled",
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

  for (const run of input.scheduledTaskRuns ?? []) {
    const task = (input.scheduledTasks ?? []).find((candidate) => candidate.id === run.task_id);
    const running = run.status === "pending" || run.status === "running";
    items.push({
      id: `automation-run:${run.id}`,
      automationId: run.task_id,
      kind: "automation",
      label: cleanDisplayText(task?.name || "计划任务"),
      detail: cleanDisplayText(run.error || run.result_summary || "定时执行"),
      status: running ? "running" : run.status === "completed" ? "completed" : "failed",
      startedAt: Date.parse(run.started_at || run.scheduled_at) || undefined,
      attention: run.status === "failed" || run.status === "cancelled",
    });
  }

  return items
    .sort((a, b) => Number(isActiveRuntimeStatus(b.status)) - Number(isActiveRuntimeStatus(a.status)) || Number(b.attention) - Number(a.attention) || runtimeKindOrder(a.kind) - runtimeKindOrder(b.kind) || (b.startedAt ?? 0) - (a.startedAt ?? 0))
    .slice(0, 16);
}

const isActiveRuntimeStatus = (status: ActivityRunItem["status"]): boolean =>
  status === "running" || status === "stalled";

function backgroundTaskDetail(task: BackgroundTaskEntry): string | undefined {
  if (task.status === "stalled") {
    const prompt = compactRuntimeDetail(task.stalledTail || task.stalledAdvice || "可能正在等待交互输入");
    return prompt ? `等待输入 · ${prompt}` : "等待输入";
  }
  if (task.status === "failed" && task.outputPreview) {
    const output = compactRuntimeDetail(task.outputPreview);
    if (output) return task.exitCode == null ? output : `exit ${task.exitCode} · ${output}`;
  }
  const details = [
    task.exitCode == null ? "" : `exit ${task.exitCode}`,
    task.duration == null ? "" : `${task.duration.toFixed(1).replace(/\.0$/, "")} 秒`,
  ].filter(Boolean);
  return details.join(" · ") || undefined;
}

function compactRuntimeDetail(value: string): string {
  const singleLine = cleanDisplayText(value).replace(/\s+/g, " ");
  return singleLine.length > 160 ? `${singleLine.slice(0, 157)}...` : singleLine;
}

const runtimeKindOrder = (kind: ActivityRunItem["kind"]): number =>
  ({ terminal: 0, agent: 1, preview: 2, "background-command": 3, automation: 4 })[kind];

function toolSourceUrl(record: ReturnType<typeof getToolCallsFromMessage>[number]): string {
  if (String(record.extractionStatus || "").toLowerCase() === "failed") return "";
  const candidate = record.sourceUrl || stringArg(record.args.url) || stringArg(record.args.source_url);
  if (!/^https?:\/\//i.test(candidate)) return "";
  const evidence = String(record.evidenceType || "").toLowerCase();
  return evidence === "fetched" ? candidate : "";
}

function toolSourcePath(record: ReturnType<typeof getToolCallsFromMessage>[number]): string {
  const candidate = pathArg(record.args.file_path ?? record.args.path ?? record.args.target ?? record.args.filename);
  if (!candidate || /^https?:\/\//i.test(candidate)) return "";
  if (!isFileEvidenceTool(record)) return "";
  return candidate;
}

function isFileEvidenceTool(record: ReturnType<typeof getToolCallsFromMessage>[number]): boolean {
  const resultKind = String(record.resultKind || "").toLowerCase();
  const activityKind = String(record.activityKind || "").toLowerCase();
  return ["file", "code", "search", "workspace"].includes(resultKind)
    || ["fileread", "workspacesearch"].includes(activityKind);
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
  const normalized = normalizeArtifactPreview(artifact);
  const artifactId = normalized.artifactId.trim();
  const kind = canonicalArtifactKind(normalized.kind, normalized.mediaType);
  const mediaType = artifactMediaTypeForProjection(normalized.mediaType, kind);
  const label = cleanDisplayText(normalized.summary) || projectionArtifactFallbackLabel(kind, mediaType);
  const path = kind === "file" ? pathArg(cleanDisplayText(normalized.summary)) : "";
  return {
    id: artifactId,
    label,
    kind,
    detail: mediaType || sizeLabel(normalized.bytes),
    artifactId,
    url: normalized.url,
    mediaType,
    path: path || undefined,
  };
}

function outputFromToolRecord(record: ReturnType<typeof getToolCallsFromMessage>[number]): ActivityOutputItem | null {
  const artifactId = String(record.artifactId || "").trim();
  if (!artifactId) return null;
  const kind = canonicalArtifactKind(record.artifactKind, record.artifactMediaType, record);
  const mediaType = artifactMediaTypeForProjection(record.artifactMediaType, kind);
  const label = cleanDisplayText(artifactSummaryForRecord(record)) || projectionArtifactFallbackLabel(kind, mediaType);
  return {
    id: artifactId,
    label,
    kind,
    detail: mediaType || sizeLabel(record.artifactBytes),
    artifactId,
    mediaType,
  };
}

function outputFromPreviewArtifact(artifact: ArtifactContentState, artifactId: string): ActivityOutputItem {
  const kind = canonicalArtifactKind(artifact.kind, artifact.mediaType);
  const mediaType = artifactMediaTypeForProjection(artifact.mediaType, kind);
  return {
    id: artifactId,
    label: cleanDisplayText(artifact.name) || projectionArtifactFallbackLabel(kind, mediaType),
    kind,
    detail: mediaType,
    artifactId,
    url: artifact.url,
    mediaType,
  };
}

function mergeOutputItems(existing: ActivityOutputItem, incoming: ActivityOutputItem): ActivityOutputItem {
  const kind = existing.kind === "image" || incoming.kind === "image"
    ? "image"
    : existing.kind === "file" && incoming.kind !== "file"
      ? incoming.kind
      : existing.kind;
  return {
    ...existing,
    id: existing.id || incoming.id,
    artifactId: existing.artifactId || incoming.artifactId,
    label: isPlaceholderOutputLabel(existing.label) ? incoming.label : existing.label,
    kind,
    detail: incoming.detail || existing.detail,
    url: incoming.url || existing.url,
    path: existing.path || incoming.path,
    mediaType: incoming.kind === "image"
      ? incoming.mediaType || existing.mediaType
      : existing.mediaType || incoming.mediaType,
  };
}

function isPlaceholderOutputLabel(value: string): boolean {
  const label = cleanDisplayText(value);
  return !label || label === "未命名产物" || label === "生成文件" || label === "生成图片";
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
