import { effectiveSubagentStatus } from "../lib/collaborationDisplay";
import { pendingPromptTargetsConversation } from "../lib/pending-prompts";
import type {
  AgentProgressEntry,
  ChatMessage,
  ContentBlock,
  PendingApproval,
  PendingAskUser,
  PendingDiffReview,
  SubagentState,
  TodoItem,
} from "../stores/types";

export type RuntimePhase =
  | "thinking"
  | "searching"
  | "executing"
  | "waiting_user"
  | "finalizing"
  | "recovering"
  | "failed"
  | "done";

export interface RuntimeSummary {
  phase: RuntimePhase;
  headline: string;
  detail?: string;
  blockingLabel?: string;
  collaborationLabel?: string;
  toolLabel?: string;
  recoveryLabel?: string;
  attentionLabel?: string;
}

export interface RuntimeSummaryInput {
  conversationId: string | null;
  isStreaming: boolean;
  messages: ChatMessage[];
  todos: TodoItem[];
  agentProgress: AgentProgressEntry[];
  subagents: SubagentState[];
  pendingApproval: PendingApproval | null;
  approvalQueue: PendingApproval[];
  pendingDiffReview: PendingDiffReview | null;
  pendingAskUser: PendingAskUser | null;
}

interface PendingPromptState {
  approval: PendingApproval | null;
  approvalQueue: PendingApproval[];
  diffReview: PendingDiffReview | null;
  askUser: PendingAskUser | null;
}

interface CollaborationSummary {
  label: string;
  blockingLabel?: string;
}

function isFinalTextBlock(block: NonNullable<ChatMessage["blocks"]>[number]): boolean {
  if (block.type !== "text") return false;
  if (!block.content.trim()) return false;
  return (
    block.visibility === "final" ||
    block.phase === "final" ||
    block.source === "model_final" ||
    block.source === "reply" ||
    block.source === "fallback" ||
    block.source === "partial"
  );
}

function isUnsealedTextBlock(block: NonNullable<ChatMessage["blocks"]>[number]): boolean {
  return (
    block.type === "text" &&
    (block.visibility === "unsealed" || block.visibility === "draft") &&
    Boolean(block.content.trim())
  );
}

function isActivityBlock(block: NonNullable<ChatMessage["blocks"]>[number]): boolean {
  return block.type === "tool_call" || block.type === "process" || block.type === "progress";
}

function isRunningToolBlock(block: ContentBlock): block is Extract<ContentBlock, { type: "tool_call" }> {
  return block.type === "tool_call" && (block.record.status === "running" || block.record.status === "pending");
}

function isFileMutationToolName(name: string): boolean {
  return /(?:write_file|edit_file|apply_patch|patch_file|delete_file|rename_file|move_file|create_file|save_file)/i.test(name);
}

function isFilePrepareStatus(label: string): boolean {
  return /(?:正在|准备|生成|已准备).*(?:写入|修改|编辑|文件内容)|(?:write|edit).+file/i.test(label);
}

function isQuietFileMutationActivity(block: ContentBlock): boolean {
  if (block.type === "tool_call") return isFileMutationToolName(block.record.name);
  if (block.type === "process") {
    return isFilePrepareStatus(String(block.content || block.summary || block.title || ""));
  }
  return false;
}

function latestRunningTool(messages: ChatMessage[]): { toolName: string } | null {
  for (let messageIndex = messages.length - 1; messageIndex >= 0; messageIndex -= 1) {
    const blocks = messages[messageIndex]?.blocks || [];
    for (let blockIndex = blocks.length - 1; blockIndex >= 0; blockIndex -= 1) {
      const block = blocks[blockIndex];
      if (block && isRunningToolBlock(block)) {
        if (isFileMutationToolName(block.record.name)) continue;
        return { toolName: block.record.name };
      }
    }
  }
  return null;
}

function latestRunningProcess(messages: ChatMessage[]): { kind: "writing" | "processing"; label: string } | null {
  for (let messageIndex = messages.length - 1; messageIndex >= 0; messageIndex -= 1) {
    const blocks = messages[messageIndex]?.blocks || [];
    for (let blockIndex = blocks.length - 1; blockIndex >= 0; blockIndex -= 1) {
      const block = blocks[blockIndex];
      if (block?.type !== "process" || block.status !== "running") continue;
      const label = String(block.content || block.summary || block.title || "").trim();
      if (!label) continue;
      if (isFilePrepareStatus(label)) continue;
      if (/写入|修改|编辑|文件|writing|editing/i.test(label)) {
        return { kind: "writing", label };
      }
      return { kind: "processing", label };
    }
  }
  return null;
}

function scopedProgressEntries(conversationId: string | null, entries: AgentProgressEntry[]): AgentProgressEntry[] {
  const conversationKey = conversationId || "__active__";
  return entries.filter((entry) =>
    (entry.conversationId === conversationKey || entry.conversationId === "__active__" || !entry.conversationId) &&
    entry.visibility !== "debug",
  );
}

function latestProgressBy(
  entries: AgentProgressEntry[],
  predicate: (entry: AgentProgressEntry) => boolean,
): AgentProgressEntry | null {
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (predicate(entry)) return entry;
  }
  return null;
}

function currentTodo(todos: TodoItem[]): TodoItem | null {
  return todos.find((todo) => todo.status === "in_progress")
    || todos.find((todo) => todo.status === "blocked")
    || null;
}

function visiblePendingPrompts(input: RuntimeSummaryInput): PendingPromptState {
  const approval = pendingPromptTargetsConversation(input.pendingApproval, input.conversationId, input.conversationId)
    ? input.pendingApproval
    : null;
  const approvalQueue = input.approvalQueue.filter((item) =>
    pendingPromptTargetsConversation(item, input.conversationId, input.conversationId),
  );
  return {
    approval: approval ?? approvalQueue[0] ?? null,
    approvalQueue: approval ? approvalQueue.filter((item) => item.requestId !== approval.requestId) : approvalQueue.slice(1),
    diffReview: pendingPromptTargetsConversation(input.pendingDiffReview, input.conversationId, input.conversationId)
      ? input.pendingDiffReview
      : null,
    askUser: pendingPromptTargetsConversation(input.pendingAskUser, input.conversationId, input.conversationId)
      ? input.pendingAskUser
      : null,
  };
}

function visibleSubagents(subagents: SubagentState[]): SubagentState[] {
  const nonMessage = subagents.filter((subagent) => subagent.role !== "message");
  const filtered = nonMessage.filter((subagent) => subagent.role !== "workflow" && !subagent.id.startsWith("workflow-"));
  return filtered.length > 0 ? filtered : nonMessage;
}

function summarizeCollaboration(subagents: SubagentState[]): CollaborationSummary | null {
  const active = visibleSubagents(subagents).filter((subagent) => {
    const status = effectiveSubagentStatus(subagent);
    return status === "running" || status === "pending" || status === "blocked" || status === "error";
  });
  if (active.length === 0) return null;

  const running = active.filter((subagent) => effectiveSubagentStatus(subagent) === "running");
  const pending = active.filter((subagent) => effectiveSubagentStatus(subagent) === "pending");
  const blocked = active.filter((subagent) => {
    const status = effectiveSubagentStatus(subagent);
    return status === "blocked" || status === "error";
  });
  const requiredBlocked = blocked.filter((subagent) => subagent.requiredForFinal !== false);
  const countParts: string[] = [];
  if (running.length > 0) countParts.push(`${running.length} 个 agent 运行中`);
  if (requiredBlocked.length > 0) countParts.push(`${requiredBlocked.length} 个阻塞答复`);
  else if (blocked.length > 0) countParts.push(`${blocked.length} 个待处理`);
  else if (running.length === 0 && pending.length > 0) countParts.push(`${pending.length} 个待启动`);

  const leadItems = active.slice(0, 2).map(formatSubagentActivity).filter(Boolean);
  const extra = active.length - leadItems.length;
  const labelBase = countParts.join("，") || `${active.length} 个 agent 活跃`;
  const label = leadItems.length > 0
    ? `${labelBase} · ${leadItems.join(" · ")}${extra > 0 ? ` +${extra}` : ""}`
    : labelBase;

  return {
    label,
    blockingLabel: requiredBlocked.length > 0
      ? "等待协作结果"
      : blocked.length > 0
        ? "协作需要处理"
        : running.length > 0
          ? "等待协作完成"
          : undefined,
  };
}

function formatSubagentActivity(subagent: SubagentState): string {
  const role = humanizeRole(subagent.role || "agent");
  const activity = cleanLabel(subagent.currentActivity)
    || cleanLabel(subagent.summary)
    || cleanLabel(subagent.detail)
    || defaultSubagentActivity(effectiveSubagentStatus(subagent));
  return `${role}：${truncate(activity, 28)}`;
}

function defaultSubagentActivity(status: SubagentState["status"]): string {
  switch (status) {
    case "running":
      return "处理中";
    case "pending":
      return "等待开始";
    case "blocked":
      return "等待处理";
    case "error":
      return "执行失败";
    case "done":
      return "已完成";
    default:
      return "处理中";
  }
}

function humanizeRole(role: string): string {
  const normalized = String(role || "agent").trim();
  if (!normalized) return "agent";
  if (/verification/i.test(normalized)) return "reviewer";
  if (/explore|research/i.test(normalized)) return "researcher";
  if (/plan/i.test(normalized)) return "planner";
  return normalized.replace(/^subagent[-_:]?/i, "agent ").replace(/[_-]+/g, " ");
}

function classifyToolPhase(toolName: string): RuntimePhase {
  if (/(?:web|browser|search|grep|glob|find|read_file|list|fetch|crawl|scrape)/i.test(toolName)) {
    return "searching";
  }
  return "executing";
}

function formatToolName(name: string): string {
  const normalized = name.replace(/^mcp__\w+__/, "").toLowerCase();
  if (normalized.includes("write_file") || normalized.includes("edit_file")) return "写入文件";
  if (normalized.includes("read_file") || normalized.includes("list_dir") || normalized.includes("glob") || normalized.includes("grep")) return "读取文件";
  if (normalized.includes("run_command") || normalized.includes("shell") || normalized.includes("terminal")) return "运行命令";
  if (normalized.includes("web") || normalized.includes("browser") || normalized.includes("fetch") || normalized.includes("search")) return "检索资料";
  return normalized.replace(/_/g, " ");
}

function cleanLabel(value: string | null | undefined): string {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function truncate(value: string, maxLength: number): string {
  if (value.length <= maxLength) return value;
  return `${value.slice(0, Math.max(0, maxLength - 1))}…`;
}

function buildApprovalSummary(promptState: PendingPromptState, collaboration: CollaborationSummary | null): RuntimeSummary | null {
  if (promptState.askUser) {
    return {
      phase: "waiting_user",
      headline: "等待你的回答",
      detail: truncate(cleanLabel(promptState.askUser.question), 84),
      blockingLabel: "需要你的输入",
      collaborationLabel: collaboration?.label,
    };
  }
  if (promptState.diffReview) {
    return {
      phase: "waiting_user",
      headline: "等待你审核变更",
      detail: cleanLabel(promptState.diffReview.filePath) || "Diff review",
      blockingLabel: "需要你的确认",
      collaborationLabel: collaboration?.label,
    };
  }
  if (promptState.approval) {
    const toolName = formatToolName(promptState.approval.toolName || "tool");
    const total = 1 + promptState.approvalQueue.length;
    const source = approvalSourceLabel(promptState.approval);
    return {
      phase: "waiting_user",
      headline: `等待你批准${toolName}`,
      detail: [source ? `来源：${source}` : "", total > 1 ? `另有 ${total - 1} 项待审批` : ""].filter(Boolean).join(" · ") || undefined,
      blockingLabel: "需要你的确认",
      collaborationLabel: collaboration?.label,
      toolLabel: `工具：${toolName}`,
      attentionLabel: total > 1 ? `${total} 项待审批` : undefined,
    };
  }
  return null;
}

function approvalSourceLabel(approval: PendingApproval): string {
  const agent = cleanLabel(approval.sourceAgent);
  const thread = cleanLabel(approval.sourceThread);
  if (agent && thread) return `${agent} / ${thread}`;
  return agent || thread;
}

export function deriveRuntimeSummary(input: RuntimeSummaryInput): RuntimeSummary | null {
  const lastMessage = input.messages[input.messages.length - 1];
  const todo = currentTodo(input.todos);
  const collaboration = summarizeCollaboration(input.subagents);
  const promptState = visiblePendingPrompts(input);
  const approvalSummary = buildApprovalSummary(promptState, collaboration);
  if (approvalSummary) return approvalSummary;

  const scopedProgress = scopedProgressEntries(input.conversationId, input.agentProgress);
  const runningRecovery = latestProgressBy(scopedProgress, (entry) => entry.status === "running" && entry.phase === "recover");
  const latestRecovery = latestProgressBy(scopedProgress, (entry) => entry.phase === "recover");
  const runningApproval = latestProgressBy(scopedProgress, (entry) => entry.status === "running" && entry.stage === "approval");
  if (runningApproval) {
    const toolName = runningApproval.toolName ? formatToolName(runningApproval.toolName) : "工具";
    return {
      phase: "waiting_user",
      headline: runningApproval.message || `等待你批准${toolName}`,
      blockingLabel: "等待你的确认",
      collaborationLabel: collaboration?.label,
      toolLabel: runningApproval.toolName ? `工具：${toolName}` : undefined,
    };
  }

  if (runningRecovery) {
    return {
      phase: "recovering",
      headline: runningRecovery.summary || runningRecovery.message || "正在自动恢复",
      blockingLabel: "正在恢复执行",
      collaborationLabel: collaboration?.label,
      recoveryLabel: latestRecovery && latestRecovery.status !== "running"
        ? truncate(latestRecovery.summary || latestRecovery.message, 60)
        : undefined,
    };
  }

  const runningToolProgress = latestProgressBy(scopedProgress, (entry) => entry.status === "running" && entry.stage === "tool");
  const runningToolName = runningToolProgress?.toolName || latestRunningTool(input.messages)?.toolName || "";
  if (runningToolName) {
    const phase = classifyToolPhase(runningToolName);
    return {
      phase,
      headline: todo?.activeForm || runningToolProgress?.summary || runningToolProgress?.message || defaultHeadline(phase),
      blockingLabel: phase === "searching" ? "等待资料返回" : "等待工具完成",
      collaborationLabel: collaboration?.label,
      toolLabel: `工具：${formatToolName(runningToolName)}`,
      recoveryLabel: latestRecovery && latestRecovery.status === "completed"
        ? truncate(latestRecovery.summary || latestRecovery.message, 60)
        : undefined,
    };
  }

  const runningProcess = latestRunningProcess(input.messages);
  if (runningProcess) {
    return {
      phase: runningProcess.kind === "writing" ? "finalizing" : "executing",
      headline: todo?.activeForm || runningProcess.label || defaultHeadline(runningProcess.kind === "writing" ? "finalizing" : "executing"),
      blockingLabel: runningProcess.kind === "writing" ? "正在整理答案" : "等待处理中间结果",
      collaborationLabel: collaboration?.label,
      recoveryLabel: latestRecovery && latestRecovery.status === "completed"
        ? truncate(latestRecovery.summary || latestRecovery.message, 60)
        : undefined,
    };
  }

  if (lastMessage?.isStreaming && !lastMessage.isThinkingStreaming) {
    const blocks = lastMessage.blocks || [];
    const hasVisibleText = String(lastMessage.content || "").trim().length > 0 || blocks.some(isFinalTextBlock);
    const hasUnsealedText = blocks.some(isUnsealedTextBlock);
    const hasActivityBlocks = blocks.some((block) => isActivityBlock(block) && !isQuietFileMutationActivity(block));
    if (hasVisibleText) {
      return {
        phase: hasActivityBlocks ? "finalizing" : "thinking",
        headline: todo?.activeForm || defaultHeadline(hasActivityBlocks ? "finalizing" : "thinking"),
        blockingLabel: hasActivityBlocks ? "正在整理答案" : undefined,
        collaborationLabel: collaboration?.label,
        recoveryLabel: latestRecovery && latestRecovery.status === "completed"
          ? truncate(latestRecovery.summary || latestRecovery.message, 60)
          : undefined,
      };
    }
    if (hasActivityBlocks) {
      return {
        phase: "executing",
        headline: todo?.activeForm || defaultHeadline("executing"),
        blockingLabel: "等待处理中间结果",
        collaborationLabel: collaboration?.label,
      };
    }
    if (hasUnsealedText || input.isStreaming) {
      return {
        phase: "thinking",
        headline: todo?.activeForm || defaultHeadline("thinking"),
        collaborationLabel: collaboration?.label,
        recoveryLabel: latestRecovery && latestRecovery.status === "completed"
          ? truncate(latestRecovery.summary || latestRecovery.message, 60)
          : undefined,
      };
    }
  }

  if (collaboration) {
    return {
      phase: "executing",
      headline: todo?.activeForm || "正在协同处理",
      blockingLabel: collaboration.blockingLabel,
      collaborationLabel: collaboration.label,
      recoveryLabel: latestRecovery && latestRecovery.status === "completed"
        ? truncate(latestRecovery.summary || latestRecovery.message, 60)
        : undefined,
    };
  }

  if (input.isStreaming) {
    return {
      phase: "thinking",
      headline: todo?.activeForm || defaultHeadline("thinking"),
      recoveryLabel: latestRecovery && latestRecovery.status === "completed"
        ? truncate(latestRecovery.summary || latestRecovery.message, 60)
        : undefined,
    };
  }

  if (lastMessage?.terminalStatus === "failed") {
    return {
      phase: "failed",
      headline: lastMessage.failureMessage || "本轮执行失败",
      detail: cleanLabel(lastMessage.content) || undefined,
    };
  }

  return null;
}

function defaultHeadline(phase: RuntimePhase): string {
  switch (phase) {
    case "thinking":
      return "正在思考";
    case "searching":
      return "正在查找上下文";
    case "executing":
      return "正在执行任务";
    case "waiting_user":
      return "等待你的决定";
    case "finalizing":
      return "正在整理答案";
    case "recovering":
      return "正在自动恢复";
    case "failed":
      return "本轮执行失败";
    case "done":
      return "执行完成";
    default:
      return "正在处理";
  }
}

export function runtimePhaseLabel(phase: RuntimePhase): string {
  switch (phase) {
    case "thinking":
      return "理解中";
    case "searching":
      return "查找中";
    case "executing":
      return "执行中";
    case "waiting_user":
      return "等待你";
    case "finalizing":
      return "整理答案中";
    case "recovering":
      return "恢复中";
    case "failed":
      return "失败";
    case "done":
      return "完成";
    default:
      return "进行中";
  }
}
