import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  Check,
  ExternalLink,
  File,
  Hand,
  LoaderCircle,
  MessageSquare,
  ShieldAlert,
  ShieldCheck,
  X,
} from "lucide-react";
import { useAppStore } from "../stores";
import { buildApprovalResponseCommand, buildAskUserResponseCommand, buildElicitationResponseCommand } from "../protocol/prompt-responses";
import {
  commandResultSucceeded,
  sendClientCommandAwaitResult,
  sendPromptResponseCommand,
} from "../protocol/ws-outbox";
import type {
  PendingApproval,
  PendingAskUser,
  PendingDiffReview,
  PendingSubagentPlanReview,
  SubagentState,
} from "../stores/types";
import { pendingPromptTargetsConversation } from "../lib/pending-prompts";
import { promptDraftKey } from "../stores/prompt-drafts";
import { summarizeArgs, humanizeKey } from "./toolUtils";
import { readableToolLabel } from "./toolDisplayName";
import { SelectMenu } from "../components/SelectMenu";
import { openWorkspaceFilePreview } from "./openAttachmentPreview";
import { pushToast } from "../overlays/ToastContainer";
import { MarkdownRenderer } from "./messages/MarkdownRenderer";
import { Button } from "../components/Button";
import { parseUnifiedDiffLines, type UnifiedDiffLine } from "../lib/unified-diff";
import { safeJsonParse } from "../lib/safe-parse";
import "./InlineAgentPrompt.css";

export const InlineAgentPrompt = ({ conversationId }: { conversationId?: string } = {}) => {
  const pendingApproval = useAppStore((s) => s.pendingApproval);
  const approvalQueue = useAppStore((s) => s.approvalQueue);
  const pendingDiffReview = useAppStore((s) => s.pendingDiffReview);
  const diffReviewQueue = useAppStore((s) => s.diffReviewQueue);
  const pendingAskUser = useAppStore((s) => s.pendingAskUser);
  const askUserQueue = useAppStore((s) => s.askUserQueue);
  const selectedConversationId = useAppStore((s) => s.conversationId);
  const activeConversationId = conversationId ?? selectedConversationId;
  const primaryVisibleApproval = pendingPromptTargetsConversation(pendingApproval, activeConversationId)
    ? pendingApproval
    : null;
  const visibleDiffReview = [pendingDiffReview, ...diffReviewQueue].find((item) =>
    pendingPromptTargetsConversation(item, activeConversationId),
  ) ?? null;
  const visibleAskUser = [pendingAskUser, ...askUserQueue].find((item) =>
    pendingPromptTargetsConversation(item, activeConversationId),
  ) ?? null;
  const visibleApprovalQueue = approvalQueue.filter((item) =>
    pendingPromptTargetsConversation(item, activeConversationId),
  );
  const visibleApproval = primaryVisibleApproval ?? visibleApprovalQueue[0] ?? null;
  const queuedApprovals = visibleApproval
    ? visibleApprovalQueue.filter((item) => item.requestId !== visibleApproval.requestId)
    : [];
  const visiblePlanApproval = visibleApproval && isExitPlanModeApproval(visibleApproval)
    ? visibleApproval
    : queuedApprovals.find(isExitPlanModeApproval) ?? null;
  const visibleGenericApproval = visiblePlanApproval?.requestId === visibleApproval?.requestId
    ? null
    : visibleApproval;
  const queuedGenericApprovals = queuedApprovals.filter((item) =>
    item.requestId !== visiblePlanApproval?.requestId && !isExitPlanModeApproval(item),
  );

  if (!visibleApproval && !visibleDiffReview && !visibleAskUser) return null;

  return (
    <div className="inline-agent-prompt" aria-label="Agent 正在等待输入">
      {visibleDiffReview && <DiffApprovalCard key={visibleDiffReview.requestId} request={visibleDiffReview} />}
      {visiblePlanApproval && <PlanApprovalCard key={visiblePlanApproval.requestId} request={visiblePlanApproval} />}
      {visibleGenericApproval && <ToolApprovalCard key={visibleGenericApproval.requestId} request={visibleGenericApproval} queue={queuedGenericApprovals} />}
      {visibleAskUser && (visibleAskUser.planReview
        ? <SubagentPlanReviewCard key={visibleAskUser.requestId} request={visibleAskUser} review={visibleAskUser.planReview} />
        : <AskUserCard key={visibleAskUser.requestId} request={visibleAskUser} />)}
    </div>
  );
};

const ToolApprovalCard = ({ request, queue }: { request: PendingApproval; queue: PendingApproval[] }) => {
  const [responding, setResponding] = useState(false);
  const subagents = useAppStore((s) => s.subagents);
  const collaborationTool = ["task", "task_status", "task_stop", "send_message"].includes(request.toolName);
  const collaborationArgs = request.toolName === "task" && Array.isArray(request.args.parallel_tasks)
    ? request.args.parallel_tasks as Record<string, unknown>[]
    : [request.args];
  const summary = useMemo(() => collaborationTool ? [] : summarizeArgs(Object.fromEntries(
    Object.entries(request.args).filter(([key]) =>
      !/(?:^|_)(?:id|ids|epoch|session|thread|sender|recipient|source)(?:_|$)|^(?:agent|call|run)$/i.test(key)
      && !/^(?:agent|subagent|thread|call|toolCall|session|run|request|conversation|turn|message)Ids?$|^source(?:Agent|Thread|Tool)$/.test(key)
      && !["command", "cmd", "reason", "justification", "with_escalated_permissions"].includes(key)),
  )).filter((item) => item.label !== "request"), [request.args, collaborationTool]);
  const total = 1 + queue.length;
  const displayName = displayToolName(request.toolName);
  // MiniCode escalate-on-failure: a command retried with escalated permissions
  // carries with_escalated_permissions + a justification in its args. Surface it
  // prominently so the user understands they are approving full (unsandboxed)
  // access, not an ordinary command.
  const escalated = isEscalatedApproval(request);
  const sandboxStatus = useAppStore((s) => s.runtimeSession?.sandbox_status
    ?? s.runtimeCapabilities?.permission?.sandbox_status);
  const networkBoundaryUnavailable = sandboxStatus?.requested?.network === true
    && sandboxStatus.network_isolated === false;
  const networkUnisolated = request.toolName === "run_command"
    && (request.networkUnisolated === true || networkBoundaryUnavailable);
  const escalationJustification = String(request.args?.justification ?? "").trim();
  const sourceLabel = approvalSourceLabel(request, subagents);
  const expiry = useApprovalExpiry(request.expiresAt);

  useEffect(() => {
    setResponding(false);
  }, [request.requestId]);

  const respond = async (allowed: boolean, rememberForSession = false) => {
    if (responding) return;
    setResponding(true);
    try {
      const command = buildApprovalResponseCommand(
        request.requestId,
        allowed ? "approve" : "reject",
        {
          rememberForSession,
          owner: { conversationId: request.conversationId, turnId: request.turnId, messageId: request.messageId },
        },
      );
      const result = await sendPromptResponseCommand(command);
      if (!commandResultSucceeded(result)) throw new Error(result.message || "审批未被后端接受");
      useAppStore.getState().clearApproval(request.requestId);
    } catch (error) {
      useAppStore.getState().markApprovalError(
        request.requestId,
        error instanceof Error ? error.message : "审批提交失败",
      );
      setResponding(false);
    }
  };

  const commandText = String(request.args.command ?? request.args.cmd ?? "");
  const filePath = typeof request.args.path === "string" ? request.args.path
    : typeof request.args.file_path === "string" ? request.args.file_path : "";
  const fileAction = ["write_file", "edit_file", "apply_patch"].includes(request.toolName) ? "编辑"
    : request.toolName === "read_file" ? "读取" : "";
  const fileName = filePath.replaceAll("\\", "/").split("/").filter(Boolean).pop() || filePath;

  return (
    <section
      className="inline-approval-bar"
      aria-label="Agent is waiting for input"
      aria-busy={responding}
    >
      <div className="inline-approval-heading">
        <Hand size={18} aria-hidden="true" />
        <span>权限</span>
        {total > 1 && <span className="inline-prompt-pending">{total} 项待处理</span>}
      </div>

      <div className="inline-approval-main">
        <div className="inline-prompt-title-row">
          <span className="inline-prompt-title">{filePath && fileAction ? <>
            允许 MiniCode {fileAction}{" "}
            <button type="button" className="inline-approval-target" title={filePath}
              onClick={() => openWorkspaceFilePreview({ path: filePath, name: fileName, conversationId: request.conversationId })}>
              <File size={15} aria-hidden="true" />{fileName}
            </button> 的内容？
          </> : request.toolName === "run_command" ? "允许 MiniCode 运行此命令？" : `允许使用 ${displayName}？`}</span>
        </div>
        <div className="inline-prompt-subtitle">
          {escalationJustification || (typeof request.args.reason === "string" ? request.args.reason : "")}
          {sourceLabel ? ` 来源：${sourceLabel}。` : ""}
          {expiry.label && (
            <span className="inline-prompt-expiry" data-urgent={expiry.urgent}>
              {` ${expiry.label}`}
            </span>
          )}
        </div>

        {escalated && (
          <div className="inline-approval-escalation">
            <ShieldAlert size={14} />
            <span>
              <strong>将在沙箱外运行。</strong>

            </span>
          </div>
        )}

        {networkUnisolated && !escalated && (
          <div className="inline-approval-escalation" role="alert">
            <ShieldAlert size={14} />
            <span>当前沙箱限制文件写入，但无法隔离网络；此命令仍可直接建立网络连接。请核对完整命令后逐项决定。</span>
          </div>
        )}

        <div className="inline-approval-summary">
          {summary.filter((item) => !fileAction || item.value !== filePath).slice(0, 2).map((item) => (
            <span key={item.label} className="inline-approval-argument" title={`${item.label}: ${item.value}`}>
              <span className="inline-approval-argument-label">{item.label === "cwd" ? "工作目录" : item.label}</span>
              <span className="inline-approval-argument-value">{item.value}</span>
            </span>
          ))}
        </div>

        {collaborationTool && collaborationArgs.map((args, index) => {
          const targets = Array.isArray(args.subagent_ids)
            ? args.subagent_ids
            : [args.recipient || args.subagent_id].filter(Boolean);
          const targetLabel = targets.map((target) => approvalSourceLabel({
            ...request, sourceAgent: String(target), sourceThread: undefined,
          }, subagents)).join("、");
          const name = String(args.name || args.agent_type || targetLabel || `任务 ${index + 1}`);
          const description = String(args.description || "");
          const cwd = args.cwd as string | undefined;
          let content = String(args.prompt || args.message || args.reason || "");
          // send_message can carry a lifecycle envelope. Its routing slots
          // belong in Inspector; ordinary messages and task prompts stay exact.
          if (request.toolName === "send_message" && content.trim().startsWith("{")) {
            const protocol = safeJsonParse<{ type?: string; reason?: string; summary?: string } | null>(content, null);
            if (["idle_notification", "shutdown_request", "shutdown_response", "plan_approval_request", "plan_approval_response", "permission_request", "permission_response"].includes(protocol?.type ?? "")) {
              content = protocol?.reason || protocol?.summary || "";
            }
          }
          return (
            <div key={index} className="inline-prompt-context">
              <strong>{name}</strong>
              {description && description !== name && <div>{description}</div>}
              {content && content !== description && <div className="inline-prompt-context-body">{content}</div>}
              {args.read_only === true && <div>只读任务</div>}
              {Array.isArray(args.write_scope) && args.write_scope.length > 0 && <div>写入范围：{args.write_scope.join("、")}</div>}
              {cwd && <div>工作目录：{cwd}</div>}
            </div>
          );
        })}
        {collaborationTool && request.args.run_in_background === true && <div className="inline-prompt-subtitle">后台执行</div>}

        {/* Show the full command verbatim — never let the exact text the user is
            approving get lost behind a single-line ellipsis (approved ≠ shown). */}
        {commandText && (
          <pre className="inline-approval-command" aria-label="即将运行的命令">{commandText}</pre>
        )}

        {queue.length > 0 && (
          <div className="inline-approval-queue">
            接下来：{queue.map((item) => displayToolName(item.toolName)).join("、")}
          </div>
        )}
        {request.status === "error" && request.error && (
          <div role="alert" className="inline-prompt-error">{request.error}</div>
        )}
      </div>

      <ApprovalActions responding={responding} onDeny={() => void respond(false)} onAllow={() => void respond(true)}
        onAllowConversation={() => void respond(true, true)} />
    </section>
  );
};

const ApprovalActions = ({ responding, onDeny, onAllow, onAllowConversation,
  denyLabel = "拒绝使用工具", allowLabel = "允许使用工具",
}: {
  responding: boolean;
  onDeny: () => void;
  onAllow: () => void;
  onAllowConversation?: () => void;
  denyLabel?: string;
  allowLabel?: string;
}) => {
  const actionsRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (document.querySelector(".inline-approval-actions") !== actionsRef.current) return;
      if (responding || event.defaultPrevented || event.isComposing || event.repeat || event.altKey || event.ctrlKey || event.metaKey) return;
      const target = event.target as HTMLElement;
      if (target.closest('input, textarea, select, button, [contenteditable="true"], [role="menu"], [role="listbox"]')) return;
      if (event.key === "Escape" || event.key === "Enter") {
        event.preventDefault();
        (event.key === "Escape" ? onDeny : onAllow)();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [responding, onDeny, onAllow]);
  return <div ref={actionsRef} className="inline-approval-actions">
    <Button variant="secondary" size="sm" onClick={onDeny} disabled={responding} aria-label={denyLabel}
      className="inline-approval-deny">拒绝<kbd>Esc</kbd></Button>
    <div className="inline-approval-allow-group">
      <Button variant="primary" size="sm" onClick={onAllow} disabled={responding} aria-label={allowLabel}
        className="inline-approval-allow">{responding ? "正在提交…" : "允许一次"}<kbd>↵</kbd></Button>
      {onAllowConversation && <SelectMenu value="once" ariaLabel="选择允许范围" align="end" disabled={responding}
        className="inline-approval-scope" onValueChange={(scope) => scope === "conversation" ? onAllowConversation() : onAllow()}>
        <option value="once">允许一次</option>
        <option value="conversation">允许此次对话</option>
      </SelectMenu>}
    </div>
  </div>;
};

const PlanApprovalCard = ({ request }: { request: PendingApproval }) => {
  const initialPlan = typeof request.args.plan === "string" ? request.args.plan : "";
  const planFilePath = typeof request.args.plan_file_path === "string" ? request.args.plan_file_path : "";
  const commandPrompts = normalizeCommandPrompts(request.args.command_prompts);
  const savedDraft = useAppStore((s) => s.promptDrafts[promptDraftKey(request)]);
  const updatePromptDraft = useAppStore((s) => s.updatePromptDraft);
  const draft = savedDraft?.initialPlan === initialPlan ? savedDraft : undefined;
  const plan = draft?.plan ?? initialPlan;
  const editing = draft?.editing ?? false;
  const planExpanded = draft?.planExpanded ?? false;
  const rejecting = draft?.rejecting ?? false;
  const rejectionFeedback = draft?.rejectionFeedback ?? "";
  const updatePlanDraft = (patch: Partial<NonNullable<typeof draft>>) => updatePromptDraft(request, {
    initialPlan, plan, editing, planExpanded, rejecting, rejectionFeedback, ...patch,
  });
  const [responding, setResponding] = useState(false);
  const planWasEdited = plan !== initialPlan;
  const isLongPlan = plan.length > 1200 || plan.split("\n").length > 14;
  const planDocumentId = useId();

  useEffect(() => {
    setResponding(false);
  }, [initialPlan, request.requestId]);

  const respond = async (allowed: boolean) => {
    if (responding) return;
    setResponding(true);
    try {
      const command = buildApprovalResponseCommand(
        request.requestId,
        allowed ? "approve" : "reject",
        {
          ...(allowed && planWasEdited ? { plan } : {}),
          ...(allowed && commandPrompts.length > 0 ? { commandPrompts } : {}),
          ...(!allowed && rejectionFeedback.trim() ? { feedback: rejectionFeedback } : {}),
          owner: { conversationId: request.conversationId, turnId: request.turnId, messageId: request.messageId },
        },
      );
      const result = await sendPromptResponseCommand(command);
      if (!commandResultSucceeded(result)) throw new Error(result.message || "计划审批未被后端接受");
      useAppStore.getState().clearApproval(request.requestId);
    } catch (error) {
      useAppStore.getState().markApprovalError(
        request.requestId,
        error instanceof Error ? error.message : "计划审批提交失败",
      );
      setResponding(false);
    }
  };

  return (
    <section className="inline-prompt-card inline-prompt-plan-card" aria-label="计划审批" aria-busy={responding}>
      <div className="inline-prompt-header">
        <ShieldCheck size={16} color="var(--accent-primary)" />
        <div className="inline-prompt-header-copy">
          <div className="inline-prompt-title">准备开始实现？</div>
          <div className="inline-prompt-subtitle inline-prompt-subtitle-wrap">计划已准备好。批准后开始实现，并恢复进入计划模式前的权限设置。</div>
        </div>
      </div>

      <div
        id={planDocumentId}
        className="inline-prompt-plan-document"
        role="region"
        aria-label="计划内容"
        tabIndex={editing || (isLongPlan && !planExpanded) ? undefined : 0}
        data-collapsed={!editing && isLongPlan && !planExpanded ? "true" : "false"}>
        {editing ? (
          <textarea
            value={plan}
            onChange={(event) => updatePlanDraft({ plan: event.target.value })}
            aria-label="编辑计划"
            rows={14}
            disabled={responding}
            className="inline-prompt-plan-editor"
          />
        ) : plan.trim() ? (
          <MarkdownRenderer content={plan} />
        ) : (
          <div className="inline-prompt-error">没有可审批的计划内容。请拒绝并让 Agent 先写入计划文件。</div>
        )}
      </div>

      {!editing && isLongPlan && (
        <Button
          variant="secondary"
          size="sm"
          className="inline-prompt-plan-expand"
          aria-expanded={planExpanded}
          aria-controls={planDocumentId}
          onClick={() => updatePlanDraft({ planExpanded: !planExpanded })}
        >
          {planExpanded ? "收起计划" : "展开计划"}
        </Button>
      )}

      {planFilePath && <div className="inline-prompt-plan-path">计划文件：{planFilePath}</div>}
      {commandPrompts.length > 0 && (
        <div className="inline-prompt-permissions">
          <strong>请求的实现权限</strong>
          {commandPrompts.map((item, index) => (
            <span key={`${index}-${item.tool}-${item.prompt}`}>{item.tool}：{item.prompt}</span>
          ))}
        </div>
      )}
      {request.status === "error" && request.error && <div role="alert" className="inline-prompt-error">{request.error}</div>}

      {responding && <div className="inline-prompt-response-status" role="status"><LoaderCircle size={13} className="animate-spin" aria-hidden="true" />正在提交计划…</div>}

      {rejecting && (
        <textarea
          value={rejectionFeedback}
          onChange={(event) => updatePlanDraft({ rejectionFeedback: event.target.value })}
          placeholder="说明需要调整的内容…"
          aria-label="计划拒绝反馈"
          rows={3}
          disabled={responding}
          autoFocus
          className="inline-prompt-plan-editor"
        />
      )}

      <div className="inline-prompt-button-row">
        <Button variant="secondary" size="sm" onClick={() => updatePlanDraft({ editing: !editing })} disabled={responding}>
          {editing ? "预览计划" : "编辑计划"}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => rejecting ? void respond(false) : updatePlanDraft({ rejecting: true })}
          disabled={responding}
          aria-label={rejecting ? "提交计划拒绝反馈" : "拒绝计划"}
        >
          <X size={14} />
          {rejecting ? "提交拒绝" : "拒绝"}
        </Button>
        <Button
          variant="primary"
          size="sm"
          onClick={() => void respond(true)}
          disabled={responding || !plan.trim()}
          aria-label="批准计划并开始实现"
        >
          <Check size={14} />
          批准并开始实现
        </Button>
      </div>
    </section>
  );
};

export const SubagentPlanReviewCard = (
  { request, review }: { request: PendingAskUser; review: PendingSubagentPlanReview },
) => {
  const [responding, setResponding] = useState(false);
  const [error, setError] = useState("");
  const plan = review.planContent ?? "";
  const subagents = useAppStore((s) => s.subagents);
  const teammate = approvalSourceLabel({
    requestId: request.requestId,
    conversationId: request.conversationId,
    toolName: "subagent.plan_review",
    args: {},
    sourceAgent: review.teammateName || review.subagentId,
  }, subagents, review.teammateName);

  useEffect(() => {
    setResponding(false);
    setError("");
  }, [request.requestId]);

  const respond = async (approved: boolean) => {
    if (responding) return;
    setResponding(true);
    try {
      const result = await sendClientCommandAwaitResult({
        type: "subagent.plan_review",
        subagent_id: review.subagentId,
        request_id: request.requestId,
        approved,
        ...(request.conversationId ? { conversation_id: request.conversationId } : {}),
      }, "subagent.plan_review");
      if (!commandResultSucceeded(result)) throw new Error(result.message || "计划审批未被后端接受");
      const store = useAppStore.getState();
      const agents = request.conversationId && request.conversationId !== store.conversationId
        ? store.conversationAgentStates[request.conversationId]?.subagents ?? [] : store.subagents;
      const agent = agents.find((item) => item.id === review.subagentId);
      if (agent?.activePlanRequestId === request.requestId) {
        store.updateSubagent(agent.id, { awaitingPlanApproval: false, activePlanRequestId: "" }, request.conversationId);
      }
      store.clearAskUser(request.requestId);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "计划审批提交失败，请重试。";
      setError(message);
      pushToast(message, "error", 4500);
      setResponding(false);
    }
  };

  return (
    <section className="inline-prompt-card inline-prompt-plan-card" aria-label="子智能体计划审批" aria-busy={responding}>
      <div className="inline-prompt-header">
        <ShieldCheck size={16} color="var(--accent-primary)" />
        <div className="inline-prompt-header-copy">
          <div className="inline-prompt-title">批准子智能体的计划？</div>
          <div className="inline-prompt-question">{teammate} 提交了计划，需要你批准后才能开始实现。</div>
          {review.teamName && <div className="inline-prompt-subtitle">团队：{review.teamName}</div>}
          {error && <div role="alert" className="inline-prompt-error inline-prompt-ask-error">{error}</div>}
        </div>
      </div>

      <div className="inline-prompt-plan-document" role="region" aria-label="计划内容" tabIndex={0}>
        {plan.trim()
          ? <MarkdownRenderer content={plan} />
          : <div className="inline-prompt-error">子智能体没有提交计划内容。请拒绝，让它先写入计划文件。</div>}
      </div>

      {review.plan_file_path && <div className="inline-prompt-plan-path">计划文件：{review.plan_file_path}</div>}

      {responding && <div className="inline-prompt-response-status" role="status"><LoaderCircle size={13} className="animate-spin" aria-hidden="true" />正在提交计划…</div>}

      <div className="inline-prompt-button-row">
        <Button
          variant="secondary"
          size="sm"
          disabled={responding}
          onClick={() => void respond(false)}
          aria-label="拒绝子智能体的计划"
        >
          <X size={14} />
          拒绝
        </Button>
        <Button
          variant="primary"
          size="sm"
          disabled={responding || !plan.trim()}
          onClick={() => void respond(true)}
          aria-label="批准子智能体的计划"
        >
          <Check size={14} />
          批准
        </Button>
      </div>
    </section>
  );
};

const displayToolName = (name: string): string => {
  const readable = readableToolLabel(name);
  return readable === name ? humanizeKey(name) : readable;
};

function approvalSourceLabel(request: PendingApproval, subagents: SubagentState[], teammateName?: string): string {
  const source = String(request.sourceAgent || "").trim();
  if (!source) return request.sourceThread ? "智能体" : "";
  if (source === "parent") return "主智能体";
  if (source === "*" || source === "all") return "所有智能体";
  const agent = subagents.find((item) => item.id === source || item.role === source);
  const label = teammateName || agent?.role || (source.includes("@") ? source.split("@")[0] : "");
  return !label || /^(?:(?:subagent|agent|thread|call|session|run)[-_:][\w-]+|[a-f0-9]{8,}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$/i.test(label)
    ? "智能体"
    : label;
}

// A request retried with escalated permissions runs OUTSIDE the sandbox
// (full filesystem + network). These must always be reviewed individually.
function isEscalatedApproval(request: PendingApproval): boolean {
  return request.args?.with_escalated_permissions === true
    || request.args?.with_escalated_permissions === "true";
}

function isExitPlanModeApproval(request: PendingApproval): boolean {
  return request.toolName === "exit_plan_mode" || request.sourceTool === "exit_plan_mode";
}

function normalizeCommandPrompts(value: unknown): Array<{ tool: "run_command"; prompt: string }> {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const record = item as Record<string, unknown>;
    const prompt = String(record.prompt || "").trim();
    // The control/approval wire contract only accepts MiniCode's command
    // permission prompts. Narrow here, at the untrusted request boundary,
    // instead of leaking a generic string into ApprovalResponseOptions.
    return record.tool === "run_command" && prompt ? [{ tool: "run_command" as const, prompt }] : [];
  });
}

const DiffApprovalCard = ({ request }: { request: PendingDiffReview }) => {
  const diffReview = useAppStore((s) => s.diffReview);
  const stats = useMemo(() => diffStats(request.diff), [request.diff]);
  const expiry = useApprovalExpiry(request.expiresAt);
  const [responding, setResponding] = useState(false);
  const [error, setError] = useState("");

  const respond = async (allowed: boolean) => {
    if (responding) return;
    setResponding(true);
    setError("");
    try {
      const command = buildApprovalResponseCommand(
        request.requestId,
        allowed ? "approve" : "reject",
        { owner: { conversationId: request.conversationId, turnId: request.turnId, messageId: request.messageId } },
      );
      const result = await sendPromptResponseCommand(command);
      if (!commandResultSucceeded(result)) throw new Error(result.message || "审批未被后端接受");
      const current = useAppStore.getState().diffReview;
      if (current?.requestId === request.requestId) {
        useAppStore.getState().setDiffReviewState({
          ...current,
          status: allowed ? "approved" : "rejected",
        });
      }
      useAppStore.getState().clearDiffReview(request.requestId);
    } catch (error) {
      setError(error instanceof Error ? error.message : "审批提交失败");
      const current = useAppStore.getState().diffReview;
      if (current?.requestId === request.requestId) {
        useAppStore.getState().setDiffReviewState({
          ...current,
          status: "error",
          error: error instanceof Error ? error.message : "审批提交失败",
        });
      }
      setResponding(false);
    }
  };

  const openDiff = () => {
    const store = useAppStore.getState();
    if (request.reviewState) store.setDiffReviewState(request.reviewState);
    store.setRightStackTab("inspector");
    store.addPanel({
      id: "approval-diff",
      kind: "diff",
      label: "差异审阅",
    });
  };

  return (
    <section className="inline-prompt-card inline-approval-bar" aria-busy={responding}>
      <div className="inline-approval-heading"><Hand size={18} aria-hidden="true" /><span>权限</span></div>
      <div className="inline-prompt-header">
        <div className="inline-prompt-header-copy">
          <div className="inline-prompt-title">审阅文件更改</div>
          <div className="inline-prompt-subtitle">
            {request.filePath
              || request.reviewState?.toolName
              || (diffReview?.requestId === request.requestId ? diffReview.toolName : "")
              || "工具编辑"} · <span className="chat-change-added">+{stats.plus}</span>{" "}
            <span className="chat-change-deleted">-{stats.minus}</span>
            {expiry.label && (
              <span className="inline-prompt-expiry" data-urgent={expiry.urgent}>
                {` · ${expiry.label}`}
              </span>
            )}
          </div>
        </div>
      </div>

      <div className="inline-prompt-diff-preview">
        {stats.preview.length > 0 ? stats.preview.map((line, index) => (
          <div key={`${index}-${line.text}`} className="inline-prompt-diff-line" data-kind={line.kind}>
            {line.text}
          </div>
        )) : <span className="inline-prompt-context">打开差异面板检查拟议更改。</span>}
      </div>

      {error && <div role="alert" className="inline-prompt-error">{error}</div>}
      <div className="inline-prompt-actions inline-prompt-button-row">
        <Button variant="ghost" size="sm" onClick={openDiff}>
          <ExternalLink size={14} />打开差异
        </Button>
        <ApprovalActions responding={responding} onDeny={() => void respond(false)} onAllow={() => void respond(true)}
          denyLabel="拒绝文件更改" allowLabel="允许文件更改" />
      </div>
    </section>
  );
};

const useApprovalExpiry = (expiresAt?: number): { label: string; urgent: boolean } => {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!expiresAt) return undefined;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [expiresAt]);
  if (!expiresAt) return { label: "", urgent: false };
  const remaining = Math.max(0, Math.ceil((expiresAt - now) / 1000));
  const minutes = Math.floor(remaining / 60);
  const seconds = String(remaining % 60).padStart(2, "0");
  return {
    label: remaining > 0 ? `将在 ${minutes}:${seconds} 后过期` : "授权已过期",
    urgent: remaining <= 60,
  };
};

type ElicitationField = {
  type: "string" | "number" | "integer" | "boolean" | "array";
  title?: string; description?: string; default?: string | number | boolean | string[];
  enum?: string[]; enumNames?: string[]; items?: { enum?: string[]; enumNames?: string[] };
  oneOf?: Array<{ const: string; title?: string }>;
  minimum?: number; maximum?: number; minLength?: number; maxLength?: number; pattern?: string;
  format?: string;
};

const AskUserCard = ({ request }: { request: PendingAskUser }) => {
  const draft = useAppStore((s) => s.promptDrafts[promptDraftKey(request)]);
  const updatePromptDraft = useAppStore((s) => s.updatePromptDraft);
  const persistDraft = !request.secret && !request.provider && request.promptType !== "secret" && request.promptType !== "manual_code";
  const [transientAnswer, setTransientAnswer] = useState("");
  const [transientOption, setTransientOption] = useState<number | null>(null);
  const [transientValues, setTransientValues] = useState<Record<string, string | boolean | string[]>>({});
  const fields = Object.entries((request.inputSchema?.properties ?? {}) as Record<string, ElicitationField>);
  const structured = request.inputSchema?.type === "object";
  const requiredFields = (request.inputSchema?.required ?? []) as string[];
  const defaultValues = Object.fromEntries(fields.flatMap(([name, field]) => field.default !== undefined
    ? [[name, typeof field.default === "number" ? String(field.default) : field.default]]
    : field.type === "boolean" && requiredFields.includes(name) ? [[name, false]] : [])) as Record<string, string | boolean | string[]>;
  const fieldValues = persistDraft ? draft?.elicitationValues ?? defaultValues : { ...defaultValues, ...transientValues };
  const updateField = (name: string, value: string | boolean | string[]) => {
    const elicitationValues = { ...fieldValues, [name]: value };
    if (persistDraft) updatePromptDraft(request, { elicitationValues });
    else setTransientValues(elicitationValues);
  };
  const answer = persistDraft ? draft?.answer ?? "" : transientAnswer;
  const selectedOption = persistDraft ? draft?.selectedOption ?? null : transientOption;
  const updateAnswer = (answer: string, selectedOption: number | null) => {
    if (persistDraft) updatePromptDraft(request, { answer, selectedOption });
    else { setTransientAnswer(answer); setTransientOption(selectedOption); }
  };
  const [responding, setResponding] = useState(false);
  const [error, setError] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const optionRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const questionId = useId();
  const hasOptions = !structured && Boolean(request.options && request.options.length > 0);
  const hasCustomInput = !structured && request.allowCustom !== false;
  const expiry = useApprovalExpiry(request.expiresAt);
  const canSubmit = structured ? requiredFields.every((name) => {
    const value = fieldValues[name];
    return value !== undefined && (typeof value === "string" || Array.isArray(value) ? value.length > 0 : true);
  }) : selectedOption !== null || request.allowEmpty === true || answer.length > 0;

  useEffect(() => {
    setTransientAnswer("");
    setTransientOption(null);
    setTransientValues({});
    setResponding(false);
    setError("");
    if (hasCustomInput && !hasOptions) window.setTimeout(() => inputRef.current?.focus(), 40);
  }, [request.requestId]);

  const respond = async (text: string) => {
    if (responding) return;
    setResponding(true);
    setError("");
    try {
      const owner = { conversationId: request.conversationId, turnId: request.turnId, messageId: request.messageId };
      const content = Object.fromEntries(fields.flatMap(([name, field]) => {
        const value = fieldValues[name];
        if (value === undefined || value === "") return [];
        return [[name, field.type === "number" || field.type === "integer" ? Number(value) : value]];
      }));
      const command = structured ? buildElicitationResponseCommand(request.requestId, "accept", content, owner)
        : buildAskUserResponseCommand(request.requestId, text, owner);
      const result = await sendPromptResponseCommand(command);
      if (!commandResultSucceeded(result)) throw new Error(result.message || "回答未被后端接受");
      useAppStore.getState().clearAskUser(request.requestId);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "回答发送失败，请重试。";
      setError(message);
      pushToast(message, "error", 4500);
      setResponding(false);
    }
  };

  const cancel = async () => {
    if (responding) return;
    setResponding(true);
    setError("");
    try {
      const command = structured ? buildElicitationResponseCommand(request.requestId, "cancel", undefined,
        { conversationId: request.conversationId, turnId: request.turnId, messageId: request.messageId }) : {
        type: "control_cancel_request" as const,
        request_id: request.requestId,
        ...(request.conversationId ? { conversation_id: request.conversationId } : {}),
        ...(request.turnId ? { turn_id: request.turnId } : {}),
        ...(request.messageId ? { message_id: request.messageId } : {}),
      };
      const result = await sendPromptResponseCommand(command);
      if (!commandResultSucceeded(result)) throw new Error(result.message || "取消请求未被后端接受");
      useAppStore.getState().clearAskUser(request.requestId);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "取消发送失败，请重试。";
      setError(message);
      pushToast(message, "error", 4500);
      setResponding(false);
    }
  };

  return (
    <form
      className="inline-prompt-card inline-prompt-question-card"
      aria-busy={responding}
      onSubmit={(event) => {
        event.preventDefault();
        if (canSubmit) void respond(selectedOption === null ? answer : request.options![selectedOption].value);
      }}
    >
      <div className="inline-prompt-header">
        <MessageSquare size={16} color="var(--accent-primary)" />
        <div className="inline-prompt-header-copy">
          <div className="inline-prompt-title">{request.provider ? "提供商需要认证输入" : "Agent 需要你的输入"}</div>
          {request.provider && <div className="inline-prompt-subtitle">认证提供商：{request.provider}</div>}
          {request.prompt && request.prompt.trim() !== request.question.trim() && (
            <div className="inline-prompt-context">{request.prompt}</div>
          )}
          <div id={questionId} className="inline-prompt-question">{request.question}</div>
          {expiry.label && (
            <div className="inline-prompt-subtitle inline-prompt-expiry" data-urgent={expiry.urgent}>
              {expiry.label}
            </div>
          )}
          {error && <div role="alert" className="inline-prompt-error inline-prompt-ask-error">{error}</div>}
        </div>
      </div>

      {hasOptions && (
        <div className="inline-prompt-choices" role="radiogroup" aria-labelledby={questionId}>
          {request.options?.map((option, index) => (
            <button
              key={`${option.value}:${index}`}
              ref={(element) => { optionRefs.current[index] = element; }}
              type="button"
              role="radio"
              aria-checked={selectedOption === index}
              tabIndex={index === (selectedOption ?? 0) ? 0 : -1}
              disabled={responding}
              onClick={() => {
                updateAnswer("", index);
              }}
              onKeyDown={(event) => {
                const count = request.options!.length;
                let next: number;
                if (event.key === "ArrowDown" || event.key === "ArrowRight") next = (index + 1) % count;
                else if (event.key === "ArrowUp" || event.key === "ArrowLeft") next = (index - 1 + count) % count;
                else if (event.key === "Home") next = 0;
                else if (event.key === "End") next = count - 1;
                else return;
                event.preventDefault();
                updateAnswer("", next);
                optionRefs.current[next]?.focus();
              }}
              className="inline-prompt-choice"
            >
              <span className="inline-prompt-choice-number">
                {selectedOption === index ? <Check size={13} /> : optionLetter(index)}
              </span>
              <span className="inline-prompt-choice-body">
                <span className="inline-prompt-choice-title">{option.label}</span>
                {option.description && <span className="inline-prompt-choice-description">{option.description}</span>}
              </span>
            </button>
          ))}
        </div>
      )}

      {structured && fields.map(([name, field]) => {
        const label = field.title || name;
        const value = fieldValues[name];
        const choices = field.enum ?? field.items?.enum ?? field.oneOf?.map((choice) => choice.const);
        const choiceLabels = field.enumNames ?? field.items?.enumNames ?? field.oneOf?.map((choice) => choice.title || choice.const);
        return <label className="inline-prompt-input-row" key={name}>
          <div className="inline-prompt-input-wrap">
            <div className="inline-prompt-input-label">{label}{requiredFields.includes(name) ? " *" : ""}</div>
            {field.description && <div className="inline-prompt-subtitle">{field.description}</div>}
            {field.type === "boolean" ? <input type="checkbox" aria-label={label} checked={value === true} disabled={responding}
              onChange={(event) => updateField(name, event.target.checked)} />
              : choices ? <select className="inline-prompt-input" aria-label={label} multiple={field.type === "array"}
                required={requiredFields.includes(name)} disabled={responding} value={field.type === "array" ? (value as string[] ?? []) : String(value ?? "")}
                onChange={(event) => updateField(name, field.type === "array" ? Array.from(event.target.selectedOptions).map((option) => option.value) : event.target.value)}>
                {field.type !== "array" && <option value="">请选择</option>}
                {choices.map((choice, index) => <option key={choice} value={choice}>{choiceLabels?.[index] || choice}</option>)}
              </select> : <input className="inline-prompt-input" aria-label={label}
                type={field.type === "number" || field.type === "integer" ? "number" : field.format === "email" ? "email" : "text"}
                value={String(value ?? "")} disabled={responding} required={requiredFields.includes(name)}
                min={field.minimum} max={field.maximum} step={field.type === "integer" ? 1 : "any"}
                minLength={field.minLength} maxLength={field.maxLength} pattern={field.pattern}
                onChange={(event) => updateField(name, event.target.value)} />}
          </div>
        </label>;
      })}

      {hasCustomInput && (
        <div className="inline-prompt-input-row">
          <div className="inline-prompt-input-wrap">
            {hasOptions && (
              <div className="inline-prompt-input-label">
                <span className="inline-prompt-choice-number">{optionLetter(request.options?.length ?? 0)}</span>
                自定义回答
              </div>
            )}
            <input
              ref={inputRef}
              type={request.secret ? "password" : "text"}
              value={answer}
              disabled={responding}
              onChange={(event) => {
                updateAnswer(event.target.value, null);
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter" && (event.nativeEvent.isComposing || event.keyCode === 229)) {
                  event.preventDefault();
                }
              }}
              placeholder={request.placeholder || (hasOptions ? "输入自定义回答…" : "输入你的回答…")}
              aria-label={request.secret ? "输入认证密钥" : "回答 Agent 的问题"}
              autoComplete={request.secret ? "new-password" : "off"}
              spellCheck={false}
              className="inline-prompt-input"
            />
          </div>
        </div>
      )}

      <div className="inline-prompt-footer inline-prompt-button-row">
        <span className="inline-prompt-response-status" role="status">
          {responding ? <><LoaderCircle size={13} className="animate-spin" aria-hidden="true" />正在提交…</> : hasOptions ? "选好后点击继续" : null}
        </span>
        <Button variant="secondary" size="sm" disabled={responding} onClick={() => void cancel()}>
          <X size={14} />
          取消
        </Button>
        <Button type="submit" variant="primary" size="sm" disabled={!canSubmit || responding}>
          {hasOptions || !hasCustomInput ? "继续" : "发送"}
        </Button>
      </div>
    </form>
  );
};

const diffStats = (diff: string) => {
  const lines = parseUnifiedDiffLines(diff);
  let plus = 0;
  let minus = 0;
  const preview: UnifiedDiffLine[] = [];
  for (const line of lines) {
    if (line.kind === "add") plus++;
    else if (line.kind === "del") minus++;
    if (preview.length < 8 && (line.kind === "hunk" || line.kind === "add" || line.kind === "del" || line.kind === "marker")) {
      preview.push(line);
    }
  }
  return { plus, minus, preview };
};

function optionLetter(index: number): string {
  return String.fromCharCode(65 + Math.max(0, index));
}
