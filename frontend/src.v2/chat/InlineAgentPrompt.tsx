import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  Check,
  ExternalLink,
  FileDiff,
  LoaderCircle,
  MessageSquare,
  ShieldAlert,
  ShieldCheck,
  X,
} from "lucide-react";
import { useAppStore } from "../stores";
import { buildApprovalResponseCommand, buildAskUserResponseCommand } from "../protocol/prompt-responses";
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
import { ToolGlyph, summarizeArgs, humanizeKey } from "./toolUtils";
import { readableToolLabel } from "./toolDisplayName";
import { deriveCommandPrefix } from "./commandPrefix";
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
  const draft = useAppStore((s) => s.promptDrafts[promptDraftKey(request)]);
  const updatePromptDraft = useAppStore((s) => s.updatePromptDraft);
  const amending = draft?.amending ?? false;
  const feedback = draft?.feedback ?? "";
  const subagents = useAppStore((s) => s.subagents);
  const collaborationTool = ["task", "task_status", "task_stop", "send_message"].includes(request.toolName);
  const collaborationArgs = request.toolName === "task" && Array.isArray(request.args.parallel_tasks)
    ? request.args.parallel_tasks as Record<string, unknown>[]
    : [request.args];
  const summary = useMemo(() => collaborationTool ? [] : summarizeArgs(Object.fromEntries(
    Object.entries(request.args).filter(([key]) =>
      !/(?:^|_)(?:id|ids|epoch|session|thread|sender|recipient|source)(?:_|$)|^(?:agent|call|run)$/i.test(key)
      && !/^(?:agent|subagent|thread|call|toolCall|session|run|request|conversation|turn|message)Ids?$|^source(?:Agent|Thread|Tool)$/.test(key)
      && !["command", "cmd", "justification", "with_escalated_permissions"].includes(key)),
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
  const requiresIndividualReview = (item: PendingApproval) =>
    isEscalatedApproval(item) || isExitPlanModeApproval(item)
    || (item.toolName === "run_command"
      && (item.networkUnisolated === true || networkBoundaryUnavailable));
  const escalationJustification = String(request.args?.justification ?? "").trim();
  const sourceLabel = approvalSourceLabel(request, subagents);
  const expiry = useApprovalExpiry(request.expiresAt);

  useEffect(() => {
    setResponding(false);
  }, [request.requestId]);

  const respond = async (allowed: boolean, fb?: string) => {
    if (responding) return;
    setResponding(true);
    try {
      const command = buildApprovalResponseCommand(
        request.requestId,
        allowed ? "approve" : "reject",
        {
          feedback: fb,
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

  const allowAll = async () => {
    if (responding) return;
    setResponding(true);
    const store = useAppStore.getState();
    // "Allow all" is a bulk convenience — it must NOT silently approve elevated
    // (unsandboxed / escalated) requests. Those stay queued for an explicit,
    // individually-reviewed decision so the user always sees the sandbox warning.
    const all = [request, ...queue].filter((item) => !requiresIndividualReview(item));
    const accepted: string[] = [];
    for (const item of all) {
      try {
        const command = buildApprovalResponseCommand(
          item.requestId,
          "approve",
          { owner: { conversationId: item.conversationId, turnId: item.turnId, messageId: item.messageId } },
        );
        const result = await sendPromptResponseCommand(command);
        if (!commandResultSucceeded(result)) throw new Error(result.message || "审批未被后端接受");
        accepted.push(item.requestId);
      } catch (error) {
        store.markApprovalError(
          item.requestId,
          error instanceof Error ? error.message : "审批提交失败",
        );
      }
    }
    if (accepted.length > 0) store.clearApprovals(accepted);
    setResponding(false);
  };
  // Escalated requests and ExitPlanMode are always reviewed individually.
  const individuallyReviewedQueueCount = [request, ...queue].filter(requiresIndividualReview).length;
  const bulkReviewCount = total - individuallyReviewedQueueCount;

  // "Always allow <prefix>": persist a run_command(prefix:*) content rule so future
  // commands with the same prefix skip prompting, then approve this one.
  const commandText = String(request.args?.command ?? request.args?.cmd ?? "");
  const alwaysPrefix = deriveCommandPrefix(commandText);
  const alwaysAllowPrefix = async () => {
    if (responding) return;
    if (!alwaysPrefix) {
      respond(true);
      return;
    }
    setResponding(true);
    const rule = `run_command(${alwaysPrefix}:*)`;
    try {
      const result = await sendClientCommandAwaitResult({
        type: "permissions.content_rule.add",
        rule,
        deny: false,
        scope: "global",
        source: "approval.always_allow_prefix",
      }, "permissions.content_rule.add");
      const failed = ["error", "failed", "warning"].includes(String(result.level || "").toLowerCase());
      if (failed || result.data?.rule !== rule || result.data?.deny === true) {
        throw new Error(result.message || "权限规则未保存。");
      }
      const command = buildApprovalResponseCommand(
        request.requestId,
        "approve",
        { owner: { conversationId: request.conversationId, turnId: request.turnId, messageId: request.messageId } },
      );
      const approvalResult = await sendPromptResponseCommand(command);
      if (!commandResultSucceeded(approvalResult)) {
        throw new Error(approvalResult.message || "审批未被后端接受");
      }
      useAppStore.getState().clearApproval(request.requestId);
    } catch (error) {
      const message = error instanceof Error ? error.message : "权限规则保存失败";
      useAppStore.getState().markApprovalError(request.requestId, message);
      setResponding(false);
    }
  };

  return (
    <section
      className="inline-approval-bar"
      aria-label="Agent is waiting for input"
    >
      <div className="inline-approval-icon">
        <ToolGlyph />
      </div>

      <div className="inline-approval-main">
        <div className="inline-prompt-title-row">
          <span className="inline-prompt-title">允许使用 {displayName}？</span>
          {total > 1 && <span className="inline-prompt-pending">{total} 项待处理</span>}
        </div>
        <div className="inline-prompt-subtitle">
          {escalated
            ? "请求提升权限，将在沙箱外运行并访问完整文件系统和网络。"
            : "运行此工具前需要你的授权。"}
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
              {escalationJustification ? ` ${escalationJustification}` : " Agent 表示沙箱内运行失败，需要完整访问权限。"}
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
          {summary.slice(0, 2).map((item) => (
            <span key={item.label} className="inline-approval-argument" title={`${item.label}: ${item.value}`}>
              <span className="inline-approval-argument-label">{item.label}</span>
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

        {amending && (
          <div className="inline-prompt-feedback">
            <textarea
              value={feedback}
              onChange={(e) => updatePromptDraft(request, { feedback: e.target.value })}
              placeholder="给 Agent 补充说明，例如拒绝原因或需要调整的内容…"
              aria-label="给 Agent 补充说明"
              rows={2}
              className="inline-prompt-feedback-input"
            />
            <div className="inline-prompt-feedback-actions">
              <Button variant="primary" size="sm" onClick={() => respond(false, feedback)} disabled={responding || !feedback.trim()}>
                拒绝并发送说明
              </Button>
              <Button variant="secondary" size="sm" onClick={() => updatePromptDraft(request, { amending: false })} disabled={responding}>
                取消
              </Button>
            </div>
          </div>
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

      <div className="inline-approval-actions">
        <Button variant="secondary" size="sm" onClick={() => respond(false)} disabled={responding} aria-label="拒绝使用工具">
          <X size={14} />
          拒绝
        </Button>
        <Button variant="primary" size="sm" onClick={() => respond(true)} disabled={responding} aria-label="允许使用工具">
          <Check size={14} />
          允许
        </Button>
        <Button variant="secondary" size="sm" onClick={() => updatePromptDraft(request, { amending: !amending })} disabled={responding} aria-label="补充说明" title="为本次决定补充说明">
          <MessageSquare size={14} />
          说明
        </Button>
        {alwaysPrefix && !networkUnisolated && (
          <Button
            variant="accent"
            size="sm"
            onClick={alwaysAllowPrefix}
            disabled={responding}
            aria-label={`全局始终允许 ${alwaysPrefix} 命令`}
            title={`在所有工作区全局允许“${alwaysPrefix}”命令`}
          >
            <ShieldCheck size={14} />
            全局允许 {alwaysPrefix}
          </Button>
        )}
        {queue.length > 0 && bulkReviewCount > 0 && (
          <Button
            variant="accent"
            size="sm"
            onClick={allowAll}
            disabled={responding}
            aria-label="允许所有未提升权限的待处理工具请求"
            title={individuallyReviewedQueueCount > 0
              ? `允许队列中的普通请求；仍有 ${individuallyReviewedQueueCount} 项请求需要单独审阅`
              : "允许所有待处理工具请求"}
          >
            {individuallyReviewedQueueCount > 0 ? "允许普通请求" : "全部允许"}
          </Button>
        )}
      </div>
    </section>
  );
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
    <section className="inline-prompt-card">
      <div className="inline-prompt-header">
        <FileDiff size={16} color="var(--accent-primary)" />
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
        <Button variant="secondary" size="sm" onClick={openDiff}>
          <ExternalLink size={14} />
          打开差异
        </Button>
        <Button variant="secondary" size="sm" disabled={responding} onClick={() => void respond(false)} aria-label="拒绝文件更改">
          <X size={14} />
          拒绝
        </Button>
        <Button variant="primary" size="sm" disabled={responding} onClick={() => void respond(true)} aria-label="允许文件更改">
          <Check size={14} />
          允许
        </Button>
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

const AskUserCard = ({ request }: { request: PendingAskUser }) => {
  const draft = useAppStore((s) => s.promptDrafts[promptDraftKey(request)]);
  const updatePromptDraft = useAppStore((s) => s.updatePromptDraft);
  const persistDraft = !request.secret && !request.provider && request.promptType !== "secret" && request.promptType !== "manual_code";
  const [transientAnswer, setTransientAnswer] = useState("");
  const [transientOption, setTransientOption] = useState<number | null>(null);
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
  const hasOptions = Boolean(request.options && request.options.length > 0);
  const hasCustomInput = request.allowCustom !== false;
  const expiry = useApprovalExpiry(request.expiresAt);
  const canSubmit = selectedOption !== null || request.allowEmpty === true || answer.length > 0;

  useEffect(() => {
    setTransientAnswer("");
    setTransientOption(null);
    setResponding(false);
    setError("");
    if (hasCustomInput && !hasOptions) window.setTimeout(() => inputRef.current?.focus(), 40);
  }, [request.requestId]);

  const respond = async (text: string) => {
    if (responding) return;
    setResponding(true);
    setError("");
    try {
      const command = buildAskUserResponseCommand(
        request.requestId,
        text,
        { conversationId: request.conversationId, turnId: request.turnId, messageId: request.messageId },
      );
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
      const command = {
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
