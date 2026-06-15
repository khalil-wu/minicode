import { extractInlineCitationIndexes } from "../chat/citationProjection";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import type {
  AgentProgressEntry,
  ArtifactContentState,
  ArtifactPreview,
  ChatMessage,
  Citation,
  PlanState,
  PreviewLaunchProcessInfo,
  PreviewServerInfo,
  PreviewVerificationInfo,
  TodoItem,
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

export interface ActivitySourceItem {
  id: string;
  label: string;
  title?: string;
  url: string;
  host: string;
}

export interface ActivitySidebarState {
  hasConversation: boolean;
  progress: ActivityProgressItem[];
  output: ActivityOutputItem[];
  browser: ActivityBrowserItem[];
  sources: ActivitySourceItem[];
  isEmpty: boolean;
}

export interface ActivitySidebarStateInput {
  conversationId: string | null;
  messages: ChatMessage[];
  todos: TodoItem[];
  plan: PlanState | null;
  agentProgress: AgentProgressEntry[];
  livePreviewUrl: string | null;
  previewArtifact: ArtifactContentState | null;
  previewVerification: PreviewVerificationInfo | null;
  previewServers: PreviewServerInfo[];
  previewLaunchProcesses: PreviewLaunchProcessInfo[];
}

export function buildActivitySidebarState(input: ActivitySidebarStateInput): ActivitySidebarState {
  if (!input.conversationId) {
    return emptyState(false);
  }

  const progress = buildProgress(input);
  const output = buildOutput(input.messages, input.previewArtifact);
  const browser = buildBrowser(input);
  const sources = buildSources(input.messages);

  return {
    hasConversation: true,
    progress,
    output,
    browser,
    sources,
    isEmpty: progress.length === 0 && output.length === 0 && browser.length === 0 && sources.length === 0,
  };
}

function emptyState(hasConversation: boolean): ActivitySidebarState {
  return {
    hasConversation,
    progress: [],
    output: [],
    browser: [],
    sources: [],
    isEmpty: true,
  };
}

function buildProgress(input: ActivitySidebarStateInput): ActivityProgressItem[] {
  const progress: ActivityProgressItem[] = [];

  if (input.todos.length > 0) {
    for (const todo of input.todos) {
      progress.push({
        id: todo.id,
        label: todo.status === "in_progress" && todo.activeForm ? todo.activeForm : todo.content,
        status: todoStatus(todo.status),
      });
    }
  } else if (input.plan?.steps.length) {
    for (const [index, step] of input.plan.steps.entries()) {
      progress.push({
        id: step.id || `plan-step-${index}`,
        label: step.title,
        detail: step.detail,
        status: planStepStatus(step.status),
      });
    }
  }

  const conversationKey = input.conversationId || "__active__";
  const compactProgress = input.agentProgress
    .filter((entry) =>
      (entry.conversationId === conversationKey || entry.conversationId === "__active__" || !entry.conversationId) &&
      entry.visibility !== "debug" &&
      (entry.stage === "planning" || entry.stage === "verification" || entry.stage === "final"),
    )
    .slice(-4);

  for (const entry of compactProgress) {
    const label = entry.summary || entry.message || entry.label || "";
    if (!label.trim()) continue;
    progress.push({
      id: entry.id,
      label,
      detail: entry.detail,
      status: progressStatus(entry.status),
    });
  }

  return dedupeBy(progress, (item) => item.id);
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
    for (const record of getToolCallsFromMessage(message)) {
      const preview = String(record.outputPreview || "").trim();
      if (record.status !== "success" || !preview) continue;
      const id = `command-output-${record.id}`;
      if (seen.has(id)) continue;
      seen.add(id);
      items.push({
        id,
        label: commandLabel(record.args?.command, record.name),
        kind: "command",
        detail: firstLine(preview),
      });
    }
  }

  if (previewArtifact?.artifactId && !seen.has(previewArtifact.artifactId)) {
    seen.add(previewArtifact.artifactId);
    items.unshift({
      id: previewArtifact.artifactId,
      label: previewArtifact.name || previewArtifact.artifactId,
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

function buildSources(messages: ChatMessage[]): ActivitySourceItem[] {
  const items: ActivitySourceItem[] = [];
  const seen = new Set<string>();
  const assistantMessages = messages.filter((message) => message.role === "assistant");

  for (const message of assistantMessages) {
    const citedIndexes = extractInlineCitationIndexes(message.content || "");
    if (citedIndexes.size === 0) continue;
    const citations = (message.citations ?? []).filter((citation, index) =>
      citedIndexes.has(index + 1),
    );
    for (const citation of citations) {
      const url = citationUrl(citation);
      if (!url || seen.has(url)) continue;
      seen.add(url);
      items.push({
        id: url,
        label: citation.label || hostLabel(url),
        title: citation.title,
        url,
        host: hostLabel(url),
      });
    }
  }

  return items.slice(-8);
}

function outputFromArtifact(artifact: ArtifactPreview): ActivityOutputItem {
  return {
    id: artifact.artifactId,
    label: artifact.summary || artifact.artifactId,
    kind: artifact.kind,
    detail: artifact.mediaType || sizeLabel(artifact.bytes),
    artifactId: artifact.artifactId,
    url: artifact.url,
    mediaType: artifact.mediaType,
    path: artifact.kind === "file" ? artifact.summary : undefined,
  };
}

function todoStatus(status: TodoItem["status"]): ActivityProgressStatus {
  if (status === "completed") return "completed";
  if (status === "in_progress") return "running";
  if (status === "blocked") return "failed";
  return "pending";
}

function planStepStatus(status: PlanState["steps"][number]["status"]): ActivityProgressStatus {
  if (status === "done") return "completed";
  if (status === "running") return "running";
  if (status === "failed") return "failed";
  return "pending";
}

function progressStatus(status: AgentProgressEntry["status"]): ActivityProgressStatus {
  if (status === "completed" || status === "info") return "completed";
  if (status === "running") return "running";
  return "failed";
}

function commandLabel(command: unknown, fallback: string): string {
  const text = typeof command === "string" ? command.trim() : "";
  if (!text) return fallback;
  return text.length > 90 ? `${text.slice(0, 87)}...` : text;
}

function firstLine(value: string): string {
  const line = value.split(/\r?\n/).find((item) => item.trim()) ?? value;
  return line.length > 120 ? `${line.slice(0, 117)}...` : line;
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
