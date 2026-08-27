import { useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  ExternalLink,
  FileDiff,
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
} from "../stores/types";
import { pendingPromptTargetsConversation } from "../lib/pending-prompts";
import { ToolGlyph, summarizeArgs, humanizeKey } from "./toolUtils";
import { readableToolLabel } from "./toolDisplayName";
import { deriveCommandPrefix } from "./commandPrefix";
import { pushToast } from "../overlays/ToastContainer";
import { MarkdownRenderer } from "./messages/MarkdownRenderer";
import { Button } from "../components/Button";

export const InlineAgentPrompt = () => {
  const pendingApproval = useAppStore((s) => s.pendingApproval);
  const approvalQueue = useAppStore((s) => s.approvalQueue);
  const pendingDiffReview = useAppStore((s) => s.pendingDiffReview);
  const diffReviewQueue = useAppStore((s) => s.diffReviewQueue);
  const pendingAskUser = useAppStore((s) => s.pendingAskUser);
  const askUserQueue = useAppStore((s) => s.askUserQueue);
  const activeConversationId = useAppStore((s) => s.conversationId);
  const primaryVisibleApproval = pendingPromptTargetsConversation(pendingApproval, activeConversationId, activeConversationId)
    ? pendingApproval
    : null;
  const visibleDiffReview = [pendingDiffReview, ...diffReviewQueue].find((item) =>
    pendingPromptTargetsConversation(item, activeConversationId, activeConversationId),
  ) ?? null;
  const visibleAskUser = [pendingAskUser, ...askUserQueue].find((item) =>
    pendingPromptTargetsConversation(item, activeConversationId, activeConversationId),
  ) ?? null;
  const visibleApprovalQueue = approvalQueue.filter((item) =>
    pendingPromptTargetsConversation(item, activeConversationId, activeConversationId),
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
    <div className="inline-agent-prompt" style={shellStyle} aria-label="Agent 正在等待输入">
      {visibleDiffReview && <DiffApprovalCard request={visibleDiffReview} />}
      {visiblePlanApproval && <PlanApprovalCard request={visiblePlanApproval} />}
      {visibleGenericApproval && <ToolApprovalCard request={visibleGenericApproval} queue={queuedGenericApprovals} />}
      {visibleAskUser && (visibleAskUser.planReview
        ? <SubagentPlanReviewCard request={visibleAskUser} review={visibleAskUser.planReview} />
        : <AskUserCard request={visibleAskUser} />)}
    </div>
  );
};

const ToolApprovalCard = ({ request, queue }: { request: PendingApproval; queue: PendingApproval[] }) => {
  const [responding, setResponding] = useState(false);
  const [amending, setAmending] = useState(false);
  const [feedback, setFeedback] = useState("");
  const summary = useMemo(() => summarizeArgs(request.args), [request.args]);
  const total = 1 + queue.length;
  const displayName = displayToolName(request.toolName);
  // MiniCode escalate-on-failure: a command retried with escalated permissions
  // carries with_escalated_permissions + a justification in its args. Surface it
  // prominently so the user understands they are approving full (unsandboxed)
  // access, not an ordinary command.
  const escalated = isEscalatedApproval(request);
  const escalationJustification = String(request.args?.justification ?? "").trim();
  const sourceLabel = approvalSourceLabel(request);
  const expiry = useApprovalExpiry(request.expiresAt);

  useEffect(() => {
    setResponding(false);
    setAmending(false);
    setFeedback("");
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
      if (result && !commandResultSucceeded(result)) throw new Error(result.message || "审批未被后端接受");
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
    const store = useAppStore.getState();
    // "Allow all" is a bulk convenience — it must NOT silently approve elevated
    // (unsandboxed / escalated) requests. Those stay queued for an explicit,
    // individually-reviewed decision so the user always sees the sandbox warning.
    const all = [request, ...queue].filter((item) =>
      !isEscalatedApproval(item) && !isExitPlanModeApproval(item),
    );
    const accepted: string[] = [];
    for (const item of all) {
      try {
        const command = buildApprovalResponseCommand(
          item.requestId,
          "approve",
          { owner: { conversationId: item.conversationId, turnId: item.turnId, messageId: item.messageId } },
        );
        const result = await sendPromptResponseCommand(command);
        if (result && !commandResultSucceeded(result)) throw new Error(result.message || "审批未被后端接受");
        accepted.push(item.requestId);
      } catch (error) {
        store.markApprovalError(
          item.requestId,
          error instanceof Error ? error.message : "审批提交失败",
        );
      }
    }
    if (accepted.length > 0) store.clearApprovals(accepted);
  };
  // Escalated requests and ExitPlanMode are always reviewed individually.
  const individuallyReviewedQueueCount = [request, ...queue].filter((item) =>
    isEscalatedApproval(item) || isExitPlanModeApproval(item),
  ).length;

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
      if (approvalResult && !commandResultSucceeded(approvalResult)) {
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
      style={approvalBarStyle}
    >
      <div style={approvalIconStyle}>
        <ToolGlyph />
      </div>

      <div className="inline-approval-main" style={approvalMainStyle}>
        <div style={approvalTitleRowStyle}>
          <span style={titleStyle}>允许使用 {displayName}？</span>
          {total > 1 && <span style={pendingPillStyle}>{total} 项待处理</span>}
        </div>
        <div style={subtitleStyle}>
          {escalated
            ? "请求提升权限，将在沙箱外运行并访问完整文件系统和网络。"
            : "运行此工具前需要你的授权。"}
          {sourceLabel ? ` 来源：${sourceLabel}。` : ""}
          {expiry.label && (
            <span style={{ color: expiry.urgent ? "var(--state-warning)" : "inherit" }}>
              {` ${expiry.label}`}
            </span>
          )}
        </div>

        {escalated && (
          <div style={escalationBannerStyle}>
            <ShieldAlert size={14} style={{ flexShrink: 0, marginTop: 1 }} />
            <span>
              <strong>将在沙箱外运行。</strong>
              {escalationJustification ? ` ${escalationJustification}` : " Agent 表示沙箱内运行失败，需要完整访问权限。"}
            </span>
          </div>
        )}

        <div style={compactSummaryRowStyle}>
          {summary.slice(0, 2).map((item) => (
            <span key={item.label} style={approvalArgStyle} title={`${item.label}: ${item.value}`}>
              <span style={approvalArgLabelStyle}>{item.label}</span>
              <span style={approvalArgValueStyle}>{item.value}</span>
            </span>
          ))}
        </div>

        {/* Show the full command verbatim — never let the exact text the user is
            approving get lost behind a single-line ellipsis (approved ≠ shown). */}
        {commandText && (
          <pre style={fullCommandStyle} aria-label="即将运行的命令">{commandText}</pre>
        )}

        {amending && (
          <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 6 }}>
            <textarea
              value={feedback}
              onChange={(e) => setFeedback(e.target.value)}
              placeholder="给 Agent 补充说明，例如拒绝原因或需要调整的内容…"
              aria-label="给 Agent 补充说明"
              rows={2}
              style={{
                width: "100%",
                padding: "6px 8px",
                borderRadius: 6,
                border: "1px solid var(--border-subtle)",
                background: "var(--surface-base)",
                color: "var(--text-primary)",
                fontSize: "var(--text-xxs)",
                fontFamily: "inherit",
                resize: "vertical",
              }}
            />
            <div style={{ display: "flex", gap: 6 }}>
              <Button variant="primary" size="sm" onClick={() => respond(false, feedback)} disabled={responding || !feedback.trim()}>
                拒绝并发送说明
              </Button>
              <Button variant="secondary" size="sm" onClick={() => setAmending(false)} disabled={responding}>
                取消
              </Button>
            </div>
          </div>
        )}

        {queue.length > 0 && (
          <div style={queueStyle}>
            接下来：{queue.map((item) => displayToolName(item.toolName)).join("、")}
          </div>
        )}
        {request.status === "error" && request.error && (
          <div style={errorStyle}>{request.error}</div>
        )}
      </div>

      <div className="inline-approval-actions" style={compactButtonRowStyle}>
        <Button variant="secondary" size="sm" onClick={() => respond(false)} disabled={responding} aria-label="拒绝使用工具">
          <X size={14} />
          拒绝
        </Button>
        <Button variant="primary" size="sm" onClick={() => respond(true)} disabled={responding} aria-label="允许使用工具">
          <Check size={14} />
          允许
        </Button>
        <Button variant="secondary" size="sm" onClick={() => setAmending((v) => !v)} disabled={responding} aria-label="补充说明" title="为本次决定补充说明">
          <MessageSquare size={14} />
          说明
        </Button>
        {alwaysPrefix && (
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
        {queue.length > 0 && (
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
  const [plan, setPlan] = useState(initialPlan);
  const [editing, setEditing] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [rejectionFeedback, setRejectionFeedback] = useState("");
  const [responding, setResponding] = useState(false);
  const planWasEdited = plan !== initialPlan;

  useEffect(() => {
    setPlan(initialPlan);
    setEditing(false);
    setRejecting(false);
    setRejectionFeedback("");
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
      if (result && !commandResultSucceeded(result)) throw new Error(result.message || "计划审批未被后端接受");
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
    <section style={planApprovalCardStyle} aria-label="计划审批">
      <div style={headerStyle}>
        <ShieldCheck size={16} color="var(--accent-primary)" />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={titleStyle}>准备开始实现？</div>
          <div style={subtitleStyle}>Agent 已完成计划，批准后将退出 Plan mode 并恢复进入前的权限模式。</div>
        </div>
      </div>

      <div style={planDocumentStyle}>
        {editing ? (
          <textarea
            value={plan}
            onChange={(event) => setPlan(event.target.value)}
            aria-label="编辑计划"
            rows={14}
            style={planEditorStyle}
          />
        ) : plan.trim() ? (
          <MarkdownRenderer content={plan} />
        ) : (
          <div style={errorStyle}>没有可审批的计划内容。请拒绝并让 Agent 先写入 Plan 文件。</div>
        )}
      </div>

      {planFilePath && <div style={planFilePathStyle}>Plan 文件：{planFilePath}</div>}
      {commandPrompts.length > 0 && (
        <div style={requestedPermissionsStyle}>
          <strong>请求的实现权限</strong>
          {commandPrompts.map((item, index) => (
            <span key={`${index}-${item.tool}-${item.prompt}`}>{item.tool}：{item.prompt}</span>
          ))}
        </div>
      )}
      {request.status === "error" && request.error && <div style={errorStyle}>{request.error}</div>}

      {rejecting && (
        <textarea
          value={rejectionFeedback}
          onChange={(event) => setRejectionFeedback(event.target.value)}
          placeholder="说明需要调整的内容…"
          aria-label="计划拒绝反馈"
          rows={3}
          autoFocus
          style={planEditorStyle}
        />
      )}

      <div style={buttonRowStyle}>
        <Button variant="secondary" size="sm" onClick={() => setEditing((value) => !value)} disabled={responding}>
          {editing ? "预览计划" : "编辑计划"}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => rejecting ? void respond(false) : setRejecting(true)}
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

const SubagentPlanReviewCard = (
  { request, review }: { request: PendingAskUser; review: PendingSubagentPlanReview },
) => {
  const [responding, setResponding] = useState(false);
  const [error, setError] = useState("");
  const plan = review.planContent?.trim() ?? "";

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
      useAppStore.getState().clearAskUser(request.requestId);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "计划审批提交失败，请重试。";
      setError(message);
      pushToast(message, "error", 4500);
      setResponding(false);
    }
  };

  return (
    <section style={planApprovalCardStyle} aria-label="子智能体计划审批">
      <div style={headerStyle}>
        <ShieldCheck size={16} color="var(--accent-primary)" />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={titleStyle}>批准子智能体的计划？</div>
          <div style={askSubtitleStyle}>{request.question}</div>
          {review.teamName && <div style={subtitleStyle}>团队：{review.teamName}</div>}
          {error && <div role="alert" style={askErrorStyle}>{error}</div>}
        </div>
      </div>

      <div style={planDocumentStyle}>
        {plan
          ? <MarkdownRenderer content={plan} />
          : <div style={errorStyle}>子智能体没有提交计划内容。请拒绝，让它先写入 Plan 文件。</div>}
      </div>

      {review.plan_file_path && <div style={planFilePathStyle}>Plan 文件：{review.plan_file_path}</div>}

      <div style={buttonRowStyle}>
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
          disabled={responding}
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

function approvalSourceLabel(request: PendingApproval): string {
  const agent = String(request.sourceAgent || "").trim();
  const thread = String(request.sourceThread || "").trim();
  if (agent && thread) return `${agent}（${thread}）`;
  return agent || thread;
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

  const respond = async (allowed: boolean) => {
    if (responding) return;
    setResponding(true);
    try {
      const command = buildApprovalResponseCommand(
        request.requestId,
        allowed ? "approve" : "reject",
        { owner: { conversationId: request.conversationId, turnId: request.turnId, messageId: request.messageId } },
      );
      const result = await sendPromptResponseCommand(command);
      if (result && !commandResultSucceeded(result)) throw new Error(result.message || "审批未被后端接受");
      const current = useAppStore.getState().diffReview;
      if (current?.requestId === request.requestId) {
        useAppStore.getState().setDiffReviewState({
          ...current,
          status: allowed ? "approved" : "rejected",
        });
      }
      useAppStore.getState().clearDiffReview(request.requestId);
    } catch (error) {
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
    <section style={cardStyle}>
      <div style={headerStyle}>
        <FileDiff size={16} color="var(--accent-primary)" />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={titleStyle}>审阅文件更改</div>
          <div style={subtitleStyle}>
            {request.filePath
              || request.reviewState?.toolName
              || (diffReview?.requestId === request.requestId ? diffReview.toolName : "")
              || "工具编辑"} · <span style={{ color: "var(--state-success)" }}>+{stats.plus}</span>{" "}
            <span style={{ color: "var(--state-danger)" }}>-{stats.minus}</span>
            {expiry.label && (
              <span style={{ color: expiry.urgent ? "var(--state-warning)" : "var(--text-muted)" }}>
                {` · ${expiry.label}`}
              </span>
            )}
          </div>
        </div>
      </div>

      <div style={diffPreviewStyle}>
        {stats.preview.length > 0 ? stats.preview.map((line, index) => (
          <div key={`${index}-${line}`} style={diffLineStyle(line)}>
            {line}
          </div>
        )) : <span style={{ color: "var(--text-muted)" }}>打开差异面板检查拟议更改。</span>}
      </div>

      <div className="inline-prompt-actions" style={buttonRowStyle}>
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
  const [answer, setAnswer] = useState("");
  const [responding, setResponding] = useState(false);
  const [error, setError] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const hasOptions = Boolean(request.options && request.options.length > 0);
  const hasCustomInput = request.allowCustom !== false;
  const expiry = useApprovalExpiry(request.expiresAt);
  const canSubmit = request.allowEmpty === true || answer.length > 0;

  useEffect(() => {
    setAnswer("");
    setResponding(false);
    setError("");
    if (hasCustomInput && !hasOptions) window.setTimeout(() => inputRef.current?.focus(), 40);
  }, [request.requestId]);

  const respond = async (text: string) => {
    if (responding) return;
    setResponding(true);
    try {
      const command = buildAskUserResponseCommand(
        request.requestId,
        text,
        { conversationId: request.conversationId, turnId: request.turnId, messageId: request.messageId },
      );
      const result = await sendPromptResponseCommand(command);
      if (result && !commandResultSucceeded(result)) throw new Error(result.message || "回答未被后端接受");
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
    try {
      const command = {
        type: "control_cancel_request" as const,
        request_id: request.requestId,
        ...(request.conversationId ? { conversation_id: request.conversationId } : {}),
        ...(request.turnId ? { turn_id: request.turnId } : {}),
        ...(request.messageId ? { message_id: request.messageId } : {}),
      };
      const result = await sendPromptResponseCommand(command);
      if (result && !commandResultSucceeded(result)) throw new Error(result.message || "取消请求未被后端接受");
      useAppStore.getState().clearAskUser(request.requestId);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "取消发送失败，请重试。";
      setError(message);
      pushToast(message, "error", 4500);
      setResponding(false);
    }
  };

  return (
    <section style={{ ...cardStyle, ...askUserCardStyle }}>
      <div style={headerStyle}>
        <MessageSquare size={16} color="var(--accent-primary)" />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={titleStyle}>{request.provider ? "提供商需要认证输入" : "Agent 需要你的输入"}</div>
          {request.provider && <div style={subtitleStyle}>认证提供商：{request.provider}</div>}
          {request.prompt && request.prompt.trim() !== request.question.trim() && (
            <div style={askContextStyle}>{request.prompt}</div>
          )}
          <div style={askSubtitleStyle}>{request.question}</div>
          {expiry.label && (
            <div style={{ ...subtitleStyle, color: expiry.urgent ? "var(--state-warning)" : "var(--text-muted)" }}>
              {expiry.label}
            </div>
          )}
          {error && <div role="alert" style={askErrorStyle}>{error}</div>}
        </div>
      </div>

      {hasOptions && (
        <div style={choiceGridStyle}>
          {request.options?.map((option, index) => (
            <button
              key={`${option.value}:${index}`}
              type="button"
              disabled={responding}
              onClick={() => void respond(option.value)}
              style={choiceCardStyle}
            >
              <span style={choiceLetterStyle}>{optionLetter(index)}</span>
              <span style={choiceCardBodyStyle}>
                <span style={choiceCardTitleStyle}>{option.label}</span>
                {option.description && <span style={choiceCardDescriptionStyle}>{option.description}</span>}
              </span>
            </button>
          ))}
        </div>
      )}

      {hasCustomInput && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (canSubmit) void respond(answer);
          }}
          style={askInputRowStyle}
        >
          <div style={askInputWrapStyle}>
            {hasOptions && (
              <div style={askInputLabelStyle}>
                <span style={choiceLetterStyle}>{optionLetter(request.options?.length ?? 0)}</span>
                自定义回答
              </div>
            )}
            <input
              ref={inputRef}
              type={request.secret ? "password" : "text"}
              value={answer}
              onChange={(event) => setAnswer(event.target.value)}
              placeholder={request.placeholder || (hasOptions ? "输入自定义回答…" : "输入你的回答…")}
              aria-label={request.secret ? "输入认证密钥" : "回答 Agent 的问题"}
              autoComplete={request.secret ? "new-password" : "off"}
              spellCheck={false}
              style={inputStyle}
            />
          </div>
          <Button type="submit" variant="primary" size="sm" disabled={!canSubmit || responding}>
            发送
          </Button>
        </form>
      )}

      {(
        <div style={buttonRowStyle}>
          <Button variant="secondary" size="sm" disabled={responding} onClick={() => void cancel()}>
            <X size={14} />
            取消
          </Button>
        </div>
      )}
    </section>
  );
};

const diffStats = (diff: string) => {
  const lines = diff.split("\n");
  let plus = 0;
  let minus = 0;
  const preview: string[] = [];
  for (const line of lines) {
    if (line.startsWith("+") && !line.startsWith("+++")) plus++;
    else if (line.startsWith("-") && !line.startsWith("---")) minus++;
    if (preview.length < 8 && (line.startsWith("@@") || line.startsWith("+") || line.startsWith("-"))) {
      preview.push(line);
    }
  }
  return { plus, minus, preview };
};

const shellStyle: React.CSSProperties = {
  display: "grid",
  gap: 6,
  width: "100%",
  margin: "0 0 8px",
  flexShrink: 0,
};

const approvalBarStyle: React.CSSProperties = {
  border: "1px solid var(--border-subtle)",
  background: "color-mix(in oklch, var(--surface-page) 92%, var(--accent-primary) 8%)",
  borderRadius: "var(--radius-sm, 6px)",
  padding: "8px 9px",
  display: "grid",
  alignItems: "center",
  gap: 9,
  overflow: "hidden",
};

const approvalIconStyle: React.CSSProperties = {
  width: 24,
  height: 24,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  borderRadius: "var(--radius-sm, 5px)",
  background: "color-mix(in oklch, var(--state-warning) 10%, var(--surface-soft))",
  border: "1px solid color-mix(in oklch, var(--state-warning) 28%, var(--border-subtle))",
};

const approvalMainStyle: React.CSSProperties = {
  minWidth: 0,
  display: "grid",
  gap: 3,
};

const approvalTitleRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 7,
  minWidth: 0,
};

const pendingPillStyle: React.CSSProperties = {
  flexShrink: 0,
  padding: "1px 6px",
  borderRadius: 999,
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontWeight: "var(--fw-semibold)",
};

const compactSummaryRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  minWidth: 0,
  overflow: "hidden",
  flex: 1,
};

const approvalArgStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 4,
  minWidth: 0,
  maxWidth: "min(320px, 45vw)",
  fontSize: "var(--text-xs)",
};

const approvalArgLabelStyle: React.CSSProperties = {
  flexShrink: 0,
  color: "var(--text-muted)",
};

const approvalArgValueStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-secondary)",
  fontFamily: "var(--font-mono)",
};

const compactButtonRowStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "flex-end",
  gap: 6,
  flexShrink: 0,
};

const fullCommandStyle: React.CSSProperties = {
  margin: "2px 0 0",
  padding: "6px 8px",
  gridColumn: "1 / -1",
  maxHeight: 132,
  overflow: "auto",
  borderRadius: "var(--radius-sm, 6px)",
  border: "1px solid var(--border-subtle)",
  background: "var(--surface-base)",
  color: "var(--text-secondary)",
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.5,
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
};

const cardStyle: React.CSSProperties = {
  border: "1px solid color-mix(in oklch, var(--state-warning) 45%, var(--border-subtle))",
  background: "color-mix(in oklch, var(--state-warning) 8%, var(--surface-page))",
  borderRadius: "var(--radius-sm, 6px)",
  padding: 10,
  display: "grid",
  gap: 9,
};

const planApprovalCardStyle: React.CSSProperties = {
  ...cardStyle,
  maxHeight: "min(74vh, 720px)",
  overflow: "auto",
};

const planDocumentStyle: React.CSSProperties = {
  padding: "10px 12px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  background: "var(--surface-base)",
  color: "var(--text-primary)",
  maxHeight: "min(46vh, 460px)",
  overflow: "auto",
};

const planEditorStyle: React.CSSProperties = {
  width: "100%",
  minHeight: 260,
  padding: 0,
  border: 0,
  outline: 0,
  resize: "vertical",
  background: "transparent",
  color: "var(--text-primary)",
  font: "inherit",
  fontFamily: "var(--font-mono)",
  lineHeight: 1.55,
};

const planFilePathStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs)",
  overflowWrap: "anywhere",
};

const requestedPermissionsStyle: React.CSSProperties = {
  display: "grid",
  gap: 4,
  padding: "8px 10px",
  borderRadius: "var(--radius-sm, 6px)",
  background: "var(--surface-soft)",
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
};

const headerStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  gap: 9,
  minWidth: 0,
};

const titleStyle: React.CSSProperties = {
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  fontWeight: "var(--fw-bold)",
};

const subtitleStyle: React.CSSProperties = {
  marginTop: 2,
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.45,
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const escalationBannerStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  gap: 6,
  marginTop: 6,
  padding: "6px 7px",
  borderRadius: "var(--radius-sm, 6px)",
  border: "1px solid color-mix(in oklch, var(--state-danger) 34%, var(--border-subtle))",
  background: "color-mix(in oklch, var(--state-danger) 9%, var(--surface-page))",
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.45,
};

const queueStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const errorStyle: React.CSSProperties = {
  color: "var(--state-danger)",
  fontSize: "var(--text-xs)",
};

const buttonRowStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "flex-end",
  gap: 7,
  flexWrap: "wrap",
};

const askUserCardStyle: React.CSSProperties = {
  gap: 12,
  padding: 12,
};

const askSubtitleStyle: React.CSSProperties = {
  marginTop: 4,
  color: "var(--text-secondary)",
  fontSize: "var(--text-sm)",
  lineHeight: 1.5,
};

const askErrorStyle: React.CSSProperties = {
  marginTop: 6,
  color: "var(--state-danger)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.35,
};

const choiceGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
  gap: 8,
};

const choiceCardStyle: React.CSSProperties = {
  minHeight: 42,
  display: "grid",
  gridTemplateColumns: "24px minmax(0, 1fr)",
  alignItems: "center",
  gap: 9,
  textAlign: "left",
  padding: "8px 10px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  background: "var(--surface-base)",
  color: "var(--text-primary)",
  cursor: "pointer",
};

const choiceLetterStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  width: 22,
  height: 22,
  borderRadius: "var(--radius-sm, 5px)",
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontWeight: "var(--fw-bold)",
  lineHeight: 1,
  flexShrink: 0,
};

const choiceCardTitleStyle: React.CSSProperties = {
  fontSize: "var(--text-sm)",
  fontWeight: "var(--fw-semibold)",
  lineHeight: 1.35,
};

const choiceCardBodyStyle: React.CSSProperties = {
  display: "grid",
  gap: 2,
  minWidth: 0,
};

const choiceCardDescriptionStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontWeight: "var(--fw-normal)",
  lineHeight: 1.35,
};

const askContextStyle: React.CSSProperties = {
  marginTop: 4,
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.45,
};

const askInputRowStyle: React.CSSProperties = {
  display: "flex",
  gap: 8,
  alignItems: "end",
  flexWrap: "wrap",
};

const askInputWrapStyle: React.CSSProperties = {
  flex: 1,
  minWidth: "min(280px, 100%)",
  display: "grid",
  gap: 6,
};

const askInputLabelStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 7,
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontWeight: "var(--fw-bold)",
};

const inputStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  padding: "0 9px",
  height: 30,
  background: "var(--surface-base)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  outline: "none",
};

const diffPreviewStyle: React.CSSProperties = {
  display: "grid",
  gap: 1,
  maxHeight: 140,
  overflow: "auto",
  padding: 8,
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-base)",
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs)",
};

const diffLineStyle = (line: string): React.CSSProperties => ({
  color: line.startsWith("+") && !line.startsWith("+++")
    ? "var(--state-success)"
    : line.startsWith("-") && !line.startsWith("---")
      ? "var(--state-danger)"
      : line.startsWith("@@")
        ? "var(--accent-primary)"
        : "var(--text-secondary)",
  whiteSpace: "pre",
});

function optionLetter(index: number): string {
  return String.fromCharCode(65 + Math.max(0, index));
}
