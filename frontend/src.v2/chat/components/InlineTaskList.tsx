import { ArrowRight, Copy, ListChecks, LoaderCircle, OctagonAlert } from "lucide-react";
import { useId, useMemo, useState, type CSSProperties } from "react";
import { useAppStore } from "../../stores";
import { planStepTodoStatus, shouldSurfacePlanProgress } from "../../lib/planVisibility";
import { coordinatorNoticeKindForSubagent, effectiveSubagentStatus, type CoordinatorNoticeKind } from "../../lib/collaborationDisplay";
import { projectAgentViews, type AgentDisplayStatus } from "../../lib/agent-view-model";
import type { ConversationAgentState, ConversationMeta, GitChangesState, PlanState, SubagentState, TodoItem } from "../../stores/types";
import { RollingNumber } from "../../components/RollingNumber";
import { initialDiffReviewPatch } from "../diffReviewState";
import "./inline-task-list.css";

interface InlineTaskListProps {
  wide?: boolean;
}

export function InlineTaskList({ wide = false }: InlineTaskListProps = {}) {
  const todos = useAppStore((s) => s.todos);
  const plan = useAppStore((s) => s.plan);
  const subagents = useAppStore((s) => s.subagents);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const gitChanges = useAppStore((s) => s.gitChanges);
  const conversationId = useAppStore((s) => s.conversationId);
  const conversations = useAppStore((s) => s.conversations);
  const conversationStreaming = useAppStore((s) => s.conversationStreaming);
  const conversationAgentStates = useAppStore((s) => s.conversationAgentStates);
  const requestConversationSwitch = useAppStore((s) => s.requestConversationSwitch);
  const [collaborationPreviewVisible, setCollaborationPreviewVisible] = useState(false);
  const collaborationPreviewId = useId();

  const currentSummary = useMemo(() => summarizeWork(todos, plan), [todos, plan]);
  const collaborationSummary = useMemo(() => summarizeCollaboration(subagents), [subagents]);
  const backgroundSummary = useMemo(
    () => summarizeBackgroundRun({
      activeConversationId: conversationId,
      conversations,
      conversationStreaming,
      conversationAgentStates,
    }),
    [conversationId, conversations, conversationStreaming, conversationAgentStates],
  );
  const currentVisible =
    currentSummary &&
    (isStreaming || Boolean(currentSummary?.hasBlocked)) &&
    (!currentSummary.allCompleted || Boolean(currentSummary?.hasBlocked));
  const summary = currentVisible ? currentSummary : collaborationSummary ?? backgroundSummary;
  if (!summary) return null;

  const {
    kind,
    total,
    completed,
    activeTask,
    activeIndex,
    allCompleted,
    hasBlocked,
    backgroundConversationId,
    backgroundCount,
    conversationTitle,
  } = summary;
  const isBackground = kind === "background";
  const isCollaboration = kind === "collaboration";
  const collaborationPreviewItems = isCollaboration ? (summary.previewItems ?? []) : [];
  const visibleCollaborationPreviewItems = collaborationPreviewItems.slice(0, 3);
  const hiddenCollaborationPreviewCount = Math.max(0, collaborationPreviewItems.length - visibleCollaborationPreviewItems.length);
  const canPreviewCollaboration = isCollaboration && visibleCollaborationPreviewItems.length > 0;
  if (!isBackground && !isCollaboration && !isStreaming && !hasBlocked) return null;
  if (allCompleted && !hasBlocked) return null;

  const gitStats = isBackground ? null : summarizeLiveGitChanges(gitChanges);
  const progress = total > 0 ? Math.round((completed / total) * 100) : 0;
  const isEditingFiles = Boolean(
    gitStats &&
    stateCanShowFileEditing(activeTask?.status, isStreaming),
  );
  const isPreparingFiles = false;
  const isFileActive = isEditingFiles || isPreparingFiles;
  const visibleGitStats = isEditingFiles ? gitStats : null;
  const stateLabel = isBackground
    ? "后台运行"
    : isCollaboration
      ? hasBlocked ? "需要处理" : activeTask?.status === "pending" ? "等待中" : "运行中"
    : activeTask?.status === "blocked"
      ? "受阻"
      : isEditingFiles
        ? "正在编辑"
        : isPreparingFiles
          ? "正在生成"
      : activeTask?.status === "in_progress" || isStreaming
        ? "进行中"
        : "待处理";
  const stateTone = activeTask?.status === "blocked"
    ? "blocked"
    : isCollaboration
      ? hasBlocked ? "blocked" : "running"
    : activeTask?.status === "in_progress" || isStreaming || isBackground
      ? "running"
      : "pending";
  const baseActiveLabel = activeTask?.status === "blocked" && !isCollaboration
    ? `受阻：${activeTask.activeForm || activeTask.content}`
    : activeTask?.activeForm || activeTask?.content || (isStreaming || isBackground ? "等待任务更新" : "任务待处理");
  const activeLabel = isBackground && conversationTitle
    ? `${conversationTitle}：${baseActiveLabel}`
    : isEditingFiles && gitStats?.primaryLabel
      ? gitStats.primaryLabel
    : baseActiveLabel;
  const gitAriaLabel = visibleGitStats && visibleGitStats.label !== activeLabel ? `，${visibleGitStats.label}` : "";
  const countLabel = allCompleted ? `${completed}/${total}` : `${activeIndex}/${total}`;
  const visibleCountLabel = isBackground
      ? backgroundCount && backgroundCount > 1 ? `${backgroundCount} 个任务` : "查看"
      : isCollaboration ? stateLabel
      : kind === "plan" ? `第 ${countLabel} 步` : `任务 ${countLabel}`;
  const ariaLabel = `${visibleCountLabel}：${activeLabel}${gitAriaLabel}`;
  const openBackgroundConversation = () => {
    if (backgroundConversationId) requestConversationSwitch(backgroundConversationId);
  };
  const items = summary.items;
  const canExpandCurrent = !isBackground && items.length > 0;
  const handlePillClick = () => {
    if (isBackground) {
      openBackgroundConversation();
      return;
    }
    if (isCollaboration) {
      useAppStore.getState().setRightStackTab("subagents");
      return;
    }
    if (canExpandCurrent) useAppStore.getState().setRightStackTab("tasks");
  };
  const pillAriaLabel = isBackground
    ? `查看运行中的对话：${conversationTitle || backgroundConversationId || activeLabel}`
    : isCollaboration
      ? `打开子智能体面板：${activeLabel}`
    : `打开${kind === "plan" ? "计划" : "任务"}进度面板：${activeLabel}`;
  const openGitDiffReview = () => {
    if (!visibleGitStats?.files.length) return;
    const reviewableFiles = visibleGitStats.files.filter((file) => file.patch);
    if (!reviewableFiles.length) return;
    const store = useAppStore.getState();
    const reviewFiles = reviewableFiles.map((file) => ({
      path: file.path,
      patch: file.patch ?? "",
      additions: file.additions,
      deletions: file.deletions,
    }));
    const selectedPath = reviewFiles[0]?.path;
    store.setDiffReviewState({
      requestId: `inline-task-diff-${Date.now()}`,
      toolName: "本轮文件变更",
      diff: initialDiffReviewPatch(reviewFiles, selectedPath),
      files: reviewFiles,
      selectedPath,
      status: "viewing",
      mode: "view",
      fileDecisions: {},
      lineComments: [],
    });
    store.setRightStackTab("diff");
  };

  return (
    <div
      className="inline-task-list"
      data-state={stateTone}
      data-kind={kind}
      data-editing-files={isFileActive ? "true" : "false"}
      style={{ maxWidth: wide ? "min(560px, calc(100% - 32px))" : "min(480px, calc(100% - 32px))" }}
    >
      <span
        className="sr-only"
        role="status"
        aria-label={ariaLabel}
        aria-live="polite"
        aria-atomic="true"
      >
        {ariaLabel}
      </span>
      <button
        type="button"
        className="inline-task-pill inline-task-pill-button"
        onClick={handlePillClick}
        aria-label={pillAriaLabel}
        aria-describedby={collaborationPreviewVisible && canPreviewCollaboration ? collaborationPreviewId : undefined}
        onMouseEnter={() => canPreviewCollaboration && setCollaborationPreviewVisible(true)}
        onMouseLeave={() => canPreviewCollaboration && setCollaborationPreviewVisible(false)}
        onFocus={() => canPreviewCollaboration && setCollaborationPreviewVisible(true)}
        onBlur={() => canPreviewCollaboration && setCollaborationPreviewVisible(false)}
      >
        <span className="inline-task-pill-icon" aria-hidden="true">
          {stateTone === "blocked"
              ? <OctagonAlert size={14} />
              : isCollaboration
                ? <LoaderCircle size={14} />
              : <ListChecks size={14} />}
        </span>
        <span className="inline-task-pill-state">{stateLabel}</span>
        <span className="inline-task-pill-title">{activeLabel}</span>
        {backgroundConversationId ? (
          <span
            className="inline-task-pill-action"
            title="查看运行中的对话"
          >
            <span>{visibleCountLabel}</span>
            <ArrowRight size={12} />
          </span>
        ) : !isCollaboration ? (
          <span key={visibleCountLabel} className="inline-task-pill-count">
            {visibleCountLabel}
          </span>
        ) : null}
        {visibleGitStats && (
          <span className="inline-task-pill-change">
            {!visibleGitStats.primaryLabel && <span>{visibleGitStats.label}</span>}
            {visibleGitStats.additions > 0 && <RollingNumber value={visibleGitStats.additions} prefix="+" className="inline-task-pill-add" animateOnMount />}
            {visibleGitStats.deletions > 0 && <RollingNumber value={visibleGitStats.deletions} prefix="-" className="inline-task-pill-del" animateOnMount />}
          </span>
        )}
        {!isCollaboration && (
          <span className="inline-task-progress-track" aria-hidden="true">
            <span
              className="inline-task-progress-fill"
              style={{ "--inline-task-progress": String(Math.max(0, Math.min(100, progress)) / 100) } as CSSProperties}
            />
          </span>
        )}
      </button>
      {collaborationPreviewVisible && canPreviewCollaboration && (
        <div
          id={collaborationPreviewId}
          className="inline-task-collaboration-preview"
          role="tooltip"
          aria-label="子智能体任务预览"
        >
          <div className="inline-task-collaboration-preview-header">
            <span>子 Agent</span>
            <span>{activeIndex}/{total}</span>
          </div>
          <div className="inline-task-collaboration-preview-list">
            {visibleCollaborationPreviewItems.map((item) => (
              <div key={item.id} className="inline-task-collaboration-preview-row">
                <span className="inline-task-collaboration-preview-dot" data-tone={item.tone} aria-hidden="true" />
                <span className="inline-task-collaboration-preview-title">{item.title}</span>
                <span className="inline-task-collaboration-preview-state">{item.statusLabel}</span>
              </div>
            ))}
          </div>
          {hiddenCollaborationPreviewCount > 0 && (
            <div className="inline-task-collaboration-preview-more">
              +{hiddenCollaborationPreviewCount} 个任务
            </div>
          )}
        </div>
      )}
      {isEditingFiles && visibleGitStats && (
        <button
          type="button"
          className="inline-task-file-strip"
          onClick={openGitDiffReview}
          aria-label={`查看 ${visibleGitStats.label} diff`}
          disabled={!visibleGitStats.files.some((file) => file.patch)}
        >
          <span className="inline-task-file-strip-path">{visibleGitStats.primaryLabel ?? visibleGitStats.label}</span>
          <span className="inline-task-file-strip-stats">
            {visibleGitStats.additions > 0 && <RollingNumber value={visibleGitStats.additions} prefix="+" className="inline-task-pill-add" animateOnMount />}
            {visibleGitStats.deletions > 0 && <RollingNumber value={visibleGitStats.deletions} prefix="-" className="inline-task-pill-del" animateOnMount />}
          </span>
          <Copy size={14} />
        </button>
      )}
    </div>
  );
}

type InlineWorkItem = Pick<TodoItem, "id" | "content" | "activeForm" | "status">;
interface InlineSummary {
  kind: "todo" | "plan" | "background" | "collaboration";
  total: number;
  completed: number;
  items: InlineWorkItem[];
  activeTask?: InlineWorkItem;
  activeIndex: number;
  allCompleted: boolean;
  hasBlocked: boolean;
  backgroundConversationId?: string;
  backgroundCount?: number;
  conversationTitle?: string;
  previewItems?: InlinePreviewItem[];
}

interface InlinePreviewItem {
  id: string;
  title: string;
  statusLabel: string;
  tone: AgentDisplayStatus;
}

function summarizeCollaboration(subagents: SubagentState[]): InlineSummary | null {
  const userVisibleSubagents = subagents.filter((subagent) => subagent.role !== "message");
  const visible = userVisibleSubagents.filter((subagent) => subagent.role !== "workflow" && !subagent.id.startsWith("workflow-"));
  const candidates = visible.length > 0 ? visible : userVisibleSubagents;
  const previewItems = projectAgentViews(candidates).map((view) => ({
    id: view.id,
    title: view.title,
    statusLabel: view.statusLabel,
    tone: view.status,
  }));
  const active = candidates.filter((subagent) => ["running", "pending", "blocked"].includes(effectiveSubagentStatus(subagent)));
  const errors = candidates.filter((subagent) => effectiveSubagentStatus(subagent) === "error");
  if (active.length === 0) {
    if (errors.length > 0) {
      const label = `${errors.length} 个任务需要处理`;
      return {
        kind: "collaboration",
        total: errors.length,
        completed: 0,
        items: [],
        previewItems,
        activeTask: {
          id: "collaboration-error",
          content: label,
          activeForm: label,
          status: "blocked",
        },
        activeIndex: errors.length,
        allCompleted: false,
        hasBlocked: true,
      };
    }
    return null;
  }

  const running = active.filter((subagent) => effectiveSubagentStatus(subagent) === "running").length;
  const blocked = active.filter((subagent) => effectiveSubagentStatus(subagent) === "blocked").length;
  const notices = active
    .map(coordinatorNoticeKindForSubagent)
    .filter((kind): kind is CoordinatorNoticeKind => Boolean(kind));
  const collectOnly = blocked > 0 && notices.length === blocked && notices.every((kind) => kind === "collect_results");
  const completed = candidates.filter((subagent) =>
    ["done", "partial", "cancelled", "error"].includes(effectiveSubagentStatus(subagent)),
  ).length;
  // Keep the pill as a compact stage indicator: once collaboration starts,
  // show one active slot and advance the numerator as workers finish. Counting
  // every concurrently running worker makes a three-agent batch jump straight
  // to 3/3 before any result is ready.
  const started = Math.min(candidates.length, completed + (running > 0 ? 1 : 0) + blocked);
  let label = `${started}/${candidates.length}`;
  if (blocked > 0 && notices.includes("collect_results")) {
    label = "结果正在整理";
  } else if (blocked > 0 && notices.includes("duplicate_delegation")) {
    label = "相同任务已在处理";
  } else if (blocked > 0 && notices.includes("capacity")) {
    label = "任务较多，正在依次处理";
  } else if (blocked > 0) {
    label = `${blocked} 个任务需要处理`;
  }
  const status: InlineWorkItem["status"] = running > 0 || collectOnly
    ? "in_progress"
    : blocked > 0
      ? "blocked"
      : "pending";

  return {
    kind: "collaboration",
    total: candidates.length,
    completed,
    items: [],
    previewItems,
    activeTask: {
      id: "collaboration-active",
      content: label,
      activeForm: label,
      status,
    },
    activeIndex: started,
    allCompleted: false,
    hasBlocked: blocked > 0 && !collectOnly,
  };
}

function summarizeBackgroundRun({
  activeConversationId,
  conversations,
  conversationStreaming,
  conversationAgentStates,
}: {
  activeConversationId: string | null;
  conversations: ConversationMeta[];
  conversationStreaming: Record<string, boolean>;
  conversationAgentStates: Record<string, ConversationAgentState>;
}): InlineSummary | null {
  const running = conversations.filter((conversation) =>
    conversation.id !== activeConversationId &&
    !conversation.archived &&
    Boolean(conversationStreaming[conversation.id]),
  );
  const target = running[0];
  if (!target) return null;

  const stored = conversationAgentStates[target.id];
  const storedSummary = stored ? summarizeWork(stored.todos, stored.plan) : null;
  if (storedSummary && !storedSummary.allCompleted) {
    return {
      ...storedSummary,
      kind: "background",
      backgroundConversationId: target.id,
      backgroundCount: running.length,
      conversationTitle: target.title || "运行中的对话",
    };
  }

  return {
    kind: "background",
    total: 1,
    completed: 0,
    items: [
      {
        id: `background-${target.id}`,
        content: "任务仍在运行",
        activeForm: "任务仍在运行",
        status: "in_progress",
      },
    ],
    activeTask: {
      id: `background-${target.id}`,
      content: "任务仍在运行",
      activeForm: "任务仍在运行",
      status: "in_progress",
    },
    activeIndex: 1,
    allCompleted: false,
    hasBlocked: false,
    backgroundConversationId: target.id,
    backgroundCount: running.length,
    conversationTitle: target.title || "运行中的对话",
  };
}
function summarizeLiveGitChanges(gitChanges: GitChangesState): {
  label: string;
  primaryLabel?: string;
  additions: number;
  deletions: number;
  files: { path: string; patch?: string; additions: number; deletions: number }[];
} | null {
  const paths = new Set<string>();
  const files = gitChanges.live?.files ?? [];
  let additions = 0;
  let deletions = 0;
  for (const file of files) {
    paths.add(file.path);
    additions += file.additions;
    deletions += file.deletions;
  }
  if (paths.size === 0) return null;
  const primaryPath = files[0]?.path ?? [...paths][0];
  const primaryLabel = paths.size === 1 && primaryPath ? shortTaskPath(primaryPath) : undefined;
  return {
    label: primaryLabel ?? `${paths.size} 个文件已更改`,
    primaryLabel,
    additions,
    deletions,
    files,
  };
}

function stateCanShowFileEditing(status: InlineWorkItem["status"] | undefined, isStreaming: boolean): boolean {
  return isStreaming && status !== "blocked" && status !== "completed";
}

function shortTaskPath(path: string): string {
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  const value = parts.at(-1) ?? path;
  return value.length > 38 ? `${value.slice(0, 35)}...` : value;
}

function summarizeWork(todos: TodoItem[], plan: PlanState | null): InlineSummary | null {
  if (todos.length > 0) return summarizeItems("todo", todos);
  if (!shouldSurfacePlanProgress(plan)) return null;
  const planItems: InlineWorkItem[] = plan.steps
    .filter((step) => step.title.trim())
    .map((step) => ({
      id: step.id,
      content: step.title,
      activeForm: plan.status === "executing" && step.status === "running" ? step.title : "",
      status: planStepTodoStatus(plan, step),
    }));
  return summarizeItems("plan", planItems);
}

function summarizeItems(kind: "todo" | "plan", items: InlineWorkItem[]): InlineSummary | null {
  items = dedupeWorkItems(items);
  if (items.length === 0) return null;

  const completed = items.filter((todo) => todo.status === "completed").length;
  const allCompleted = completed === items.length;
  const activeIndexRaw = items.findIndex((todo) => todo.status === "in_progress");
  const blockedIndexRaw = items.findIndex((todo) => todo.status === "blocked");
  const pendingIndexRaw = items.findIndex((todo) => todo.status === "pending");
  const currentIndexRaw = activeIndexRaw >= 0
    ? activeIndexRaw
    : blockedIndexRaw >= 0
      ? blockedIndexRaw
      : pendingIndexRaw;
  const activeTask = currentIndexRaw >= 0 ? items[currentIndexRaw] : items.at(-1);
  const activeIndex = allCompleted ? items.length : Math.max(1, currentIndexRaw + 1);

  return {
    kind,
    total: items.length,
    completed,
    items,
    activeTask,
    activeIndex,
    allCompleted,
    hasBlocked: items.some((todo) => todo.status === "blocked"),
  };
}

const STATUS_PRIORITY: Record<InlineWorkItem["status"], number> = {
  blocked: 4,
  in_progress: 3,
  pending: 2,
  completed: 1,
};

function dedupeWorkItems(items: InlineWorkItem[]): InlineWorkItem[] {
  const result: InlineWorkItem[] = [];
  const seen = new Map<string, number>();
  for (const item of items) {
    const key = normalizedWorkItemKey(item);
    const existingIndex = seen.get(key);
    if (existingIndex == null) {
      seen.set(key, result.length);
      result.push(item);
      continue;
    }
    result[existingIndex] = preferWorkItem(result[existingIndex], item);
  }
  return result;
}

function preferWorkItem(current: InlineWorkItem, next: InlineWorkItem): InlineWorkItem {
  if (STATUS_PRIORITY[next.status] > STATUS_PRIORITY[current.status]) {
    return {
      ...next,
      activeForm: next.activeForm || current.activeForm,
    };
  }
  if (
    STATUS_PRIORITY[next.status] === STATUS_PRIORITY[current.status] &&
    (next.activeForm || "").length > (current.activeForm || "").length
  ) {
    return {
      ...current,
      activeForm: next.activeForm,
    };
  }
  return current;
}

function normalizedWorkItemKey(item: InlineWorkItem): string {
  const text = normalizeTaskText(item.content || item.activeForm || item.id || "");
  return text || item.id || "";
}

function normalizeTaskText(value: string): string {
  return value
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[`*_~"'“”‘’]/g, "")
    .replace(/[()[\]{}<>:;,.!?/\\|，。！？、；：（）《》【】]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}
