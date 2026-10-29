import { useI18n, type Translate } from "../i18n";
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { elicitationChoices, elicitationContent, elicitationDefaults, type ElicitationField } from "./elicitationForm";
import {
  Check,
  ExternalLink,
  File,
  Folder,
  Hand,
  LoaderCircle,
  MessageSquare,
  ShieldAlert,
  ShieldCheck,
  X,
} from "lucide-react";
import { useAppStore } from "../stores";
import { buildApprovalResponseCommand, buildNetworkApprovalResponseCommand, buildAskUserResponseCommand, buildElicitationResponseCommand, buildPermissionsResponseCommand } from "../protocol/prompt-responses";
import type { NetworkApprovalDecision } from "../protocol/common-types";
import { openPreferredWebLink } from "../lib/open-preferred-target";
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
import { pendingPromptKey, pendingPromptMatchesOwner, pendingPromptTargetsConversation } from "../lib/pending-prompts";
import { promptDraftKey } from "../stores/prompt-drafts";
import { summarizeArgs, humanizeKey } from "./toolUtils";
import { readableToolLabel } from "./toolDisplayName";
import { SelectMenu } from "../components/SelectMenu";
import { ContextMenu } from "../components/ContextMenu";
import { PromptSourceIcon } from "../lib/prompt-source-icons";
import { openWorkspaceFilePreview } from "./openAttachmentPreview";
import { pushToast } from "../overlays/ToastContainer";
import { MarkdownRenderer } from "./messages/MarkdownRenderer";
import { Button } from "../components/Button";
import { parseUnifiedDiffLines, type UnifiedDiffLine } from "../lib/unified-diff";
import { safeJsonParse } from "../lib/safe-parse";
import "./InlineAgentPrompt.css";
import { RequestUserInputCard } from "./RequestUserInputCard";
import { McpUrlElicitationCard } from "./McpUrlElicitationCard";
import { ComputerUseApprovalCard, isComputerUseApproval } from "./ComputerUseApprovalCard";
import { permissionRequestDetails } from "./permissionRequest";
import { useApprovalShortcuts } from "./useApprovalShortcuts";

export const InlineAgentPrompt = ({ conversationId }: { conversationId?: string } = {}) => {
  const { t: tr } = useI18n();
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
    <div className="inline-agent-prompt" aria-label={tr("Agent 正在等待输入")}>
      {visibleDiffReview && <DiffApprovalCard key={pendingPromptKey(visibleDiffReview)} request={visibleDiffReview} />}
      {visiblePlanApproval && <PlanApprovalCard key={pendingPromptKey(visiblePlanApproval)} request={visiblePlanApproval} />}
      {visibleGenericApproval && (visibleGenericApproval.permissionRequest
        ? <PermissionsApprovalCard key={pendingPromptKey(visibleGenericApproval)} request={visibleGenericApproval} />
        : visibleGenericApproval.networkApprovalContext ? <NetworkApprovalCard key={pendingPromptKey(visibleGenericApproval)} request={visibleGenericApproval} />
        : <ToolApprovalCard key={pendingPromptKey(visibleGenericApproval)} request={visibleGenericApproval} queue={queuedGenericApprovals} />)}
      {visibleAskUser && (visibleAskUser.planReview
        ? <SubagentPlanReviewCard key={pendingPromptKey(visibleAskUser)} request={visibleAskUser} review={visibleAskUser.planReview} />
        : visibleAskUser.requestUserInput ? <RequestUserInputCard key={JSON.stringify([visibleAskUser.conversationId, visibleAskUser.requestId,
            visibleAskUser.turnId, visibleAskUser.messageId, visibleAskUser.taskId])}
            request={{ ...visibleAskUser, requestUserInput: visibleAskUser.requestUserInput }} />
        : isComputerUseApproval(visibleAskUser) ? <ComputerUseApprovalCard key={JSON.stringify([visibleAskUser.conversationId,
            visibleAskUser.requestId, visibleAskUser.turnId, visibleAskUser.messageId, visibleAskUser.taskId])} request={visibleAskUser} />
        : visibleAskUser.elicitation?.mode === "url" ? <McpUrlElicitationCard key={pendingPromptKey(visibleAskUser)}
            request={{ ...visibleAskUser, elicitation: visibleAskUser.elicitation }} />
        : <AskUserCard key={pendingPromptKey(visibleAskUser)} request={visibleAskUser} />)}
    </div>
  );
};

const PermissionsApprovalCard = ({ request }: { request: PendingApproval }) => {
  const { t: tr, locale } = useI18n();
  const [localResponding, setResponding] = useState(false);
  const responding = localResponding || request.status === "submitted";
  const subagents = useAppStore((s) => s.subagents);
  const profile = request.permissionRequest!.permissions;
  const actor = approvalSourceLabel(tr, request, subagents) || "MiniCode";
  const actions: React.ReactNode[] = [];
  const conjunction = (nodes: React.ReactNode[]) => new Intl.ListFormat(locale, { type: "conjunction" })
    .formatToParts(nodes.map((_, index) => String(index)))
    .map((part, index) => <span key={index}>{part.type === "element" ? nodes[Number(part.value)] : part.value}</span>);
  const fileSystem = profile.file_system;
  for (const detail of permissionRequestDetails(profile)) {
    if (detail.kind === "network") { actions.push(tr("连接互联网")); continue; }
    const { access, paths } = detail;
    actions.push(<span key={access}>{access === "readWrite" ? tr("查看和编辑内容") : access === "read" ? tr("查看内容") : tr("编辑内容")} {conjunction(paths.map(path => <span key={path}>
      <span className="inline-permission-path" title={path}>
        {(path.replaceAll("\\", "/").split("/").filter(Boolean).pop() || path).includes(".")
          ? <File size={15} aria-hidden="true" /> : <Folder size={15} aria-hidden="true" />}
        {path.replaceAll("\\", "/").split("/").filter(Boolean).pop() || path}
      </span>
    </span>))}{tr("的内容")}</span>);
  }
  const respond = async (allowed: boolean, scope: "turn" | "session" = "turn") => {
    setResponding(true);
    useAppStore.getState().markApprovalSubmitted(request.requestId, request);
    try {
      const result = await sendPromptResponseCommand(buildPermissionsResponseCommand(request.requestId,
        allowed ? profile : {}, allowed ? scope : "turn",
        request));
      if (!commandResultSucceeded(result)) throw new Error(result.message || tr("审批未被后端接受"));
      useAppStore.getState().clearApproval(request.requestId, request);
    } catch (error) {
      useAppStore.getState().markApprovalError(request.requestId, error instanceof Error ? error.message : tr("审批提交失败"), request);
      setResponding(false);
    }
  };
  return <section className="inline-approval-bar" aria-label="Agent is waiting for input" aria-busy={responding}>
    <div className="inline-approval-heading"><Hand size={18} aria-hidden="true" /><span>{fileSystem ? tr("权限") : tr("互联网访问")}</span></div>
    <div className="inline-approval-main">
      <div className="inline-prompt-title">{tr("允许 {v0}", { v0: actor })} {conjunction(actions)}{locale === "en-US" ? "?" : "？"}</div>
      {request.permissionRequest!.reason && <div className="inline-prompt-subtitle">{request.permissionRequest!.reason}</div>}
      {request.status === "error" && request.error && <div role="alert" className="inline-prompt-error">{request.error}</div>}
    </div>
    <ApprovalActions responding={responding} onDeny={() => void respond(false)} onAllow={() => void respond(true)}
      onAllowConversation={() => void respond(true, "session")} />
  </section>;
};

const NetworkApprovalCard = ({ request }: { request: PendingApproval }) => {
  const { t: tr } = useI18n();
  const [localResponding, setResponding] = useState(false);
  const responding = localResponding || request.status === "submitted";
  const subagents = useAppStore((s) => s.subagents);
  const context = request.networkApprovalContext!;
  const choices = request.availableDecisions!;
  const amendment = choices.find((choice): choice is Extract<NetworkApprovalDecision, object> => typeof choice === "object"
    && "network_policy_amendment" in choice && choice.network_policy_amendment.network_policy_amendment.action === "allow");
  const actor = approvalSourceLabel(tr, request, subagents) || "MiniCode";
  const url = context.protocol === "http" || context.protocol === "https" ? `${context.protocol}://${context.host}` : null;
  const [titleBefore, titleAfter] = tr("允许 {actor} 与 {destination} 建立连接？", { actor, destination: "{destination}" }).split("{destination}");
  const respond = async (decision: NetworkApprovalDecision) => {
    setResponding(true);
    useAppStore.getState().markApprovalSubmitted(request.requestId, request);
    try {
      const result = await sendPromptResponseCommand(buildNetworkApprovalResponseCommand(request.requestId, decision,
        { requestDigest: request.requestDigest, owner: request }));
      if (!commandResultSucceeded(result)) throw new Error(result.message || tr("审批未被后端接受"));
      useAppStore.getState().clearApproval(request.requestId, request);
    } catch (error) {
      useAppStore.getState().markApprovalError(request.requestId, error instanceof Error ? error.message : tr("审批提交失败"), request);
      setResponding(false);
    }
  };
  return <section className="inline-approval-bar inline-native-network-approval" aria-label="Agent is waiting for input" aria-busy={responding}>
    <div className="inline-approval-heading"><NativeApprovalSourceIcon kind="hand" /><span>{tr("互联网访问")}</span></div>
    <div className="inline-approval-main" role="alert" aria-atomic="true">
      <div className="inline-prompt-title">{titleBefore}{url
        ? <a href={url} onClick={event => { event.preventDefault(); void openPreferredWebLink(url, request.conversationId); }}>{url}</a>
        : context.host}{titleAfter}</div>
      <div className="inline-prompt-subtitle">{tr("{host} 不在当前网络允许列表中", { host: context.host })}</div>
      {request.status === "error" && request.error && <div role="alert" className="inline-prompt-error">{request.error}</div>}
    </div>
    <ApprovalActions responding={responding} onDeny={() => void respond("abort")}
      onAllow={choices.includes("approved") ? () => void respond("approved") : undefined}
      nativeScopeApproval={choices.includes("approved_for_session") ? { label: tr("允许此对话"), onAllow: () => void respond("approved_for_session") } : undefined}
      leadingApproval={amendment ? { label: tr("始终允许"), onAllow: () => void respond(amendment) } : undefined} />
  </section>;
};

const ToolApprovalCard = ({ request, queue }: { request: PendingApproval; queue: PendingApproval[] }) => {
  const { t: tr } = useI18n();
  const [localResponding, setResponding] = useState(false);
  const responding = localResponding || request.status === "submitted";
  const subagents = useAppStore((s) => s.subagents);
  const collaborationTool = ["task", "spawn_agent", "task_status", "task_stop", "interrupt_agent", "send_message", "followup_task"].includes(request.toolName);
  const collaborationArgs = request.toolName === "task" && Array.isArray(request.args.parallel_tasks)
    ? request.args.parallel_tasks as Record<string, unknown>[]
    : [request.args];
  const summary = useMemo(() => request.stdinAction ? [{ label: "cwd", value: String(request.stdinAction.cwd) }] : collaborationTool ? [] : summarizeArgs(Object.fromEntries(
    Object.entries(request.args).filter(([key]) =>
      !/(?:^|_)(?:id|ids|epoch|session|thread|sender|recipient|source)(?:_|$)|^(?:agent|call|run)$/i.test(key)
      && !/^(?:agent|subagent|thread|call|toolCall|session|run|request|conversation|turn|message)Ids?$|^source(?:Agent|Thread|Tool)$/.test(key)
      && !["command", "cmd", "reason", "justification", "sandbox_permissions", "prefix_rule"].includes(key)),
  )).filter((item) => item.label !== "request"), [request.args, request.stdinAction, collaborationTool]);
  const total = 1 + queue.length;
  const displayName = displayToolName(request.toolName);
  // The Source sandbox intent and justification belong to this frozen request.
  const escalated = isEscalatedApproval(request);
  const sandboxStatus = useAppStore((s) => s.runtimeSession?.sandbox_status
    ?? s.runtimeCapabilities?.permission?.sandbox_status);
  const networkBoundaryUnavailable = sandboxStatus?.requested?.network === true
    && sandboxStatus.network_isolated === false;
  const networkUnisolated = request.toolName === "run_command"
    && (request.networkUnisolated === true || networkBoundaryUnavailable);
  const escalationJustification = String(request.stdinAction?.reason ?? request.args?.justification ?? "").trim();
  const sourceLabel = approvalSourceLabel(tr, request, subagents);
  const expiry = useApprovalExpiry(request.expiresAt);

  useEffect(() => {
    setResponding(false);
  }, [request.requestId]);

  const respond = async (allowed: boolean, rememberForSession = false, rememberForFuture = false, execPolicyAmendment?: string[]) => {
    if (responding) return;
    setResponding(true);
    useAppStore.getState().markApprovalSubmitted(request.requestId, request);
    try {
      const command = buildApprovalResponseCommand(
        request.requestId,
        allowed ? "approve" : "reject",
        {
          rememberForSession,
          rememberForFuture,
          requestDigest: request.requestDigest,
          sourceCommandDecision: request.availableDecisions ? allowed ? "approved" : "abort" : undefined,
          execPolicyAmendment,
          owner: request,
        },
      );
      const result = await sendPromptResponseCommand(command);
      if (!commandResultSucceeded(result)) throw new Error(result.message || tr("审批未被后端接受"));
      useAppStore.getState().clearApproval(request.requestId, request);
    } catch (error) {
      useAppStore.getState().markApprovalError(
        request.requestId,
        error instanceof Error ? error.message : tr("审批提交失败"), request,
      );
      setResponding(false);
    }
  };

  const commandText = request.stdinAction ? String(request.stdinAction.display_command ?? "") : String(request.args.command ?? request.args.cmd ?? "");
  const filePath = typeof request.args.path === "string" ? request.args.path
    : typeof request.args.file_path === "string" ? request.args.file_path : "";
  const fileAction = ["write_file", "edit_file", "apply_patch"].includes(request.toolName) ? tr("编辑")
    : request.toolName === "read_file" ? tr("读取") : "";
  const fileName = filePath.replaceAll("\\", "/").split("/").filter(Boolean).pop() || filePath;

  if (request.toolName === "run_command" && request.availableDecisions) {
    // Source's actual default conversationDetailMode is STEPS_COMMANDS.
    // MiniCode's legacy ViewMode is not the Source prose/commands setting.
    const actor = subagents.find(agent => agent.id === request.sourceAgent || agent.role === request.sourceAgent)?.role;
    const title = request.approvalReason?.trim() || (actor
      ? tr("允许 {v0} 运行此命令？", { v0: actor }) : tr("允许 MiniCode 运行此命令？"));
    const prefixDecision = request.availableDecisions.find((choice): choice is {
      approved_execpolicy_amendment: { proposed_execpolicy_amendment: string[] };
    } => typeof choice === "object" && "approved_execpolicy_amendment" in choice);
    const prefixLabel = prefixDecision
      ? sourceApprovalPrefix(prefixDecision.approved_execpolicy_amendment.proposed_execpolicy_amendment) : null;
    return <section className="inline-approval-bar inline-native-command-approval" aria-label="Agent is waiting for input" aria-busy={responding}>
      <div className="inline-approval-heading"><NativeApprovalSourceIcon kind="terminal" /><span>{tr("终端")}</span></div>
      <div className="inline-approval-main" role="alert" aria-atomic="true">
        <div className="inline-prompt-title">{title}</div>
        {request.status === "error" && request.error && <div role="alert" className="inline-prompt-error">{request.error}</div>}
      </div>
      <NativeCommandApprovalPreview command={request.displayCommand!} />
      <ApprovalActions responding={responding} onDeny={() => void respond(false)} onAllow={() => void respond(true)}
        prefixApproval={prefixDecision && prefixLabel !== null ? {
          onAllow: () => void respond(true, false, false, prefixDecision.approved_execpolicy_amendment.proposed_execpolicy_amendment),
          prefixLabel,
        } : undefined} />
    </section>;
  }

  return (
    <section
      className="inline-approval-bar"
      aria-label="Agent is waiting for input"
      aria-busy={responding}
    >
      <div className="inline-approval-heading">
        <Hand size={18} aria-hidden="true" />
        <span>{tr("权限")}</span>
        {total > 1 && <span className="inline-prompt-pending">{total} {tr("项待处理")}</span>}
      </div>

      <div className="inline-approval-main">
        <div className="inline-prompt-title-row">
          <span className="inline-prompt-title">{filePath && fileAction ? <>
            {tr("允许 MiniCode")}{fileAction}{" "}
            <button type="button" className="inline-approval-target" title={filePath}
              onClick={() => openWorkspaceFilePreview({ path: filePath, name: fileName, conversationId: request.conversationId })}>
              <File size={15} aria-hidden="true" />{fileName}
            </button> {tr("的内容？")}</> : request.stdinAction || request.toolName === "run_command" ? tr("允许 MiniCode 运行此命令？") : tr("允许使用 {v0}？", { v0: displayName })}</span>
        </div>
        <div className="inline-prompt-subtitle">
          {escalationJustification || (typeof request.args.reason === "string" ? request.args.reason : "")}
          {sourceLabel ? tr(" 来源：{v0}。", { v0: sourceLabel }) : ""}
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
              <strong>{tr("将在沙箱外运行。")}</strong>

            </span>
          </div>
        )}

        {networkUnisolated && !escalated && (
          <div className="inline-approval-escalation" role="alert">
            <ShieldAlert size={14} />
            <span>{tr("当前沙箱限制文件写入，但无法隔离网络；此命令仍可直接建立网络连接。请核对完整命令后逐项决定。")}</span>
          </div>
        )}

        <div className="inline-approval-summary">
          {summary.filter((item) => !fileAction || item.value !== filePath).slice(0, 2).map((item) => (
            <span key={item.label} className="inline-approval-argument" title={`${item.label}: ${item.value}`}>
              <span className="inline-approval-argument-label">{item.label === "cwd" ? tr("工作目录") : item.label}</span>
              <span className="inline-approval-argument-value">{item.value}</span>
            </span>
          ))}
        </div>

        {collaborationTool && collaborationArgs.map((args, index) => {
          const targets = Array.isArray(args.subagent_ids)
            ? args.subagent_ids
            : [args.target || args.recipient || args.subagent_id].filter(Boolean);
          const targetLabel = targets.map((target) => approvalSourceLabel(tr, {
            ...request, sourceAgent: String(target), sourceThread: undefined,
          }, subagents)).join("、");
          const name = String(args.name || args.agent_type || targetLabel || tr("任务 {v0}", { v0: index + 1 }));
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
              {args.read_only === true && <div>{tr("只读任务")}</div>}
              {Array.isArray(args.write_scope) && args.write_scope.length > 0 && <div>{tr("写入范围：")}{args.write_scope.join("、")}</div>}
              {cwd && <div>{tr("工作目录：")}{cwd}</div>}
            </div>
          );
        })}
        {collaborationTool && request.args.run_in_background === true && <div className="inline-prompt-subtitle">{tr("后台执行")}</div>}

        {/* Show the full command verbatim — never let the exact text the user is
            approving get lost behind a single-line ellipsis (approved ≠ shown). */}
        {commandText && (
          <pre className="inline-approval-command" aria-label={tr("即将运行的命令")}>{commandText}</pre>
        )}

        {queue.length > 0 && (
          <div className="inline-approval-queue">
            {tr("接下来：")}{queue.map((item) => displayToolName(item.toolName)).join("、")}
          </div>
        )}
        {request.status === "error" && request.error && (
          <div role="alert" className="inline-prompt-error">{request.error}</div>
        )}
      </div>

      <ApprovalActions responding={responding} onDeny={() => void respond(false)} onAllow={() => void respond(true)}
        onAllowConversation={request.availableDecisions || request.stdinAction || request.mcpApproval?.allow_session_remember === false ? undefined : () => void respond(true, true)}
        onAllowFuture={request.mcpApproval?.allow_persistent_approval ? () => void respond(true, false, true) : undefined}
        />
    </section>
  );
};

const NativeApprovalSourceIcon = ({ kind }: { kind: "terminal" | "info" | "hand" }) => {
  const size = kind === "info" ? 14 : 18;
  const url = `url('${import.meta.env.BASE_URL}apps/approval-${kind}.svg')`;
  return <span aria-hidden="true" data-native-approval-source-icon={kind} style={{ display: "inline-flex", width: size, height: size, flexShrink: 0,
    background: "currentColor", maskImage: url, WebkitMaskImage: url, maskPosition: "center", maskRepeat: "no-repeat", maskSize: "contain" }} />;
};

// Source qr -> Yr and shared V6/WSr -> GSr/xWn; this is display formatting,
// and never changes the frozen amendment sent back to the execution owner.
function sourceApprovalPrefix(prefix: string[]): string | null {
  if (/[\n\r]/.test(prefix.join(" "))) return null;
  return prefix.map(token => {
    if (/^[A-Za-z0-9_@+=:,./-]+$/.test(token)) return token;
    if (!/[`$\\!]/.test(token) && !token.includes('"')) return `"${token}"`;
    return token === "" ? "''" : /[^\w@%\-+=:,./]/.test(token)
      ? (`'${token.replace(/('+)/g, `'"$1"'`)}'`).replace(/^''|''$/g, "") : token;
  }).join(" ");
}

const NativeCommandApprovalPreview = ({ command }: { command: string }) => {
  const { t: tr } = useI18n();
  const textRef = useRef<HTMLSpanElement>(null);
  const [collapsible, setCollapsible] = useState(false);
  const [expanded, setExpanded] = useState(false);
  useLayoutEffect(() => {
    const text = textRef.current!;
    const measure = () => setCollapsible(text.scrollHeight > parseFloat(getComputedStyle(text).lineHeight) * 3 + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(text);
    return () => observer.disconnect();
  }, [command]);
  return <div className="inline-approval-native-command">
    <div className="inline-approval-native-command-text" aria-label={tr("即将运行的命令")}>
      <span ref={textRef} style={expanded ? undefined : { display: "-webkit-box", maxHeight: "3lh", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{command}</span>
    </div>
    {collapsible && <div className="inline-approval-native-command-toggle"><Button variant="ghost" size="sm" onClick={() => setExpanded(value => !value)} aria-expanded={expanded}>
      {expanded ? tr("收起") : tr("展开")}
    </Button></div>}
  </div>;
};

const NativeApprovalScopeMenu = ({ responding, onAllow, scopedAction }: {
  responding: boolean; onAllow?: () => void; scopedAction: { label: string; onAllow: () => void; tooltip?: string };
}) => {
  const { t: tr } = useI18n();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const menuId = useId();
  return <>
    <Button ref={triggerRef} variant="primary" size="sm" className="inline-approval-scope-action" aria-label={tr("选择允许范围")}
      aria-haspopup="menu" aria-expanded={open} aria-controls={open ? menuId : undefined} disabled={responding}
      onKeyDown={event => { if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); setOpen(true); } }}
      onClick={() => setOpen(value => !value)}><PromptSourceIcon icon="chevron-down" size={14} /></Button>
    {open && <ContextMenu id={menuId} ariaLabel={tr("选择允许范围")} className="inline-approval-native-scope-menu" position={{ x: 0, y: 0 }}
      anchor={triggerRef.current!} side="top" returnFocusTo={triggerRef.current!} onClose={() => setOpen(false)} items={[
        ...(onAllow ? [{ label: tr("允许一次"), onClick: onAllow, disabled: responding }] : []),
        { label: scopedAction.label, onClick: scopedAction.onAllow, disabled: responding,
          ...(scopedAction.tooltip ? { rightIcon: <NativeApprovalSourceIcon kind="info" />, rightIconTooltip: scopedAction.tooltip } : {}) },
      ]} />}
  </>;
};

const ApprovalActions = ({ responding, onDeny, onAllow, onAllowConversation, onAllowFuture,
  denyLabel, allowLabel, prefixApproval, nativeScopeApproval, leadingApproval,
}: {
  responding: boolean;
  onDeny: () => void;
  onAllow?: () => void;
  onAllowConversation?: () => void;
  onAllowFuture?: () => void;
  prefixApproval?: { onAllow: () => void; prefixLabel: string };
  nativeScopeApproval?: { label: string; onAllow: () => void };
  leadingApproval?: { label: string; onAllow: () => void };
  denyLabel?: string;
  allowLabel?: string;
}) => {
  const { t: tr } = useI18n();
  const actionsRef = useApprovalShortcuts({ responding, onDeny, onAllow });
  return <div ref={actionsRef} className="inline-approval-actions">
    {leadingApproval && <Button variant="secondary" size="sm" className="inline-approval-leading" disabled={responding}
      onClick={leadingApproval.onAllow}>{leadingApproval.label}</Button>}
    <Button variant="secondary" size="sm" onClick={onDeny} disabled={responding} aria-label={denyLabel ?? tr("拒绝使用工具")}
      className="inline-approval-deny">{tr("拒绝")}{!responding && <kbd>Esc</kbd>}</Button>
    {(onAllow || nativeScopeApproval) && <div className="inline-approval-allow-group">
      {onAllow && <Button variant="primary" size="sm" onClick={onAllow} disabled={responding} loading={responding} aria-label={allowLabel ?? tr("允许使用工具")}
        className="inline-approval-allow">{tr("允许一次")}{!responding && <kbd>↵</kbd>}</Button>}
      {prefixApproval || nativeScopeApproval ? <NativeApprovalScopeMenu responding={responding} onAllow={onAllow}
        scopedAction={prefixApproval ? { label: tr("允许类似命令"), onAllow: prefixApproval.onAllow,
          tooltip: tr("允许在此对话中使用以 {command} 开头的命令", { command: prefixApproval.prefixLabel }) } : nativeScopeApproval!} />
        : (onAllowConversation || onAllowFuture) && <SelectMenu value="once" ariaLabel={tr("选择允许范围")} align="end" disabled={responding}
        className="inline-approval-scope" onValueChange={(scope) => scope === "future" ? onAllowFuture?.() : scope === "conversation" ? onAllowConversation?.() : onAllow?.()}>
        <option value="once">{tr("允许一次")}</option>
        {onAllowConversation && <option value="conversation">{tr("允许此次对话")}</option>}
        {onAllowFuture && <option value="future">{tr("始终允许此工具")}</option>}
      </SelectMenu>}
    </div>}
  </div>;
};

const PlanApprovalCard = ({ request }: { request: PendingApproval }) => {
  const { t: tr } = useI18n();
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
  const [localResponding, setResponding] = useState(false);
  const responding = localResponding || request.status === "submitted";
  const planWasEdited = plan !== initialPlan;
  const isLongPlan = plan.length > 1200 || plan.split("\n").length > 14;
  const planDocumentId = useId();

  useEffect(() => {
    setResponding(false);
  }, [initialPlan, request.requestId]);

  const respond = async (allowed: boolean) => {
    if (responding) return;
    setResponding(true);
    useAppStore.getState().markApprovalSubmitted(request.requestId, request);
    try {
      const command = buildApprovalResponseCommand(
        request.requestId,
        allowed ? "approve" : "reject",
        {
          ...(allowed && planWasEdited ? { plan } : {}),
          ...(allowed && commandPrompts.length > 0 ? { commandPrompts } : {}),
          ...(!allowed && rejectionFeedback.trim() ? { feedback: rejectionFeedback } : {}),
          owner: request,
        },
      );
      const result = await sendPromptResponseCommand(command);
      if (!commandResultSucceeded(result)) throw new Error(result.message || tr("计划审批未被后端接受"));
      useAppStore.getState().clearApproval(request.requestId, request);
    } catch (error) {
      useAppStore.getState().markApprovalError(
        request.requestId,
        error instanceof Error ? error.message : tr("计划审批提交失败"), request,
      );
      setResponding(false);
    }
  };

  return (
    <section className="inline-prompt-card inline-prompt-plan-card" aria-label={tr("计划审批")} aria-busy={responding}>
      <div className="inline-prompt-header">
        <ShieldCheck size={16} color="var(--accent-primary)" />
        <div className="inline-prompt-header-copy">
          <div className="inline-prompt-title">{tr("准备开始实现？")}</div>
          <div className="inline-prompt-subtitle inline-prompt-subtitle-wrap">{tr("计划已准备好。批准后开始实现，并恢复进入计划模式前的权限设置。")}</div>
        </div>
      </div>

      <div
        id={planDocumentId}
        className="inline-prompt-plan-document"
        role="region"
        aria-label={tr("计划内容")}
        tabIndex={editing || (isLongPlan && !planExpanded) ? undefined : 0}
        data-collapsed={!editing && isLongPlan && !planExpanded ? "true" : "false"}>
        {editing ? (
          <textarea
            value={plan}
            onChange={(event) => updatePlanDraft({ plan: event.target.value })}
            aria-label={tr("编辑计划")}
            rows={14}
            disabled={responding}
            className="inline-prompt-plan-editor"
          />
        ) : plan.trim() ? (
          <MarkdownRenderer content={plan} />
        ) : (
          <div className="inline-prompt-error">{tr("没有可审批的计划内容。请拒绝并让 Agent 先写入计划文件。")}</div>
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
          {planExpanded ? tr("收起计划") : tr("展开计划")}
        </Button>
      )}

      {planFilePath && <div className="inline-prompt-plan-path">{tr("计划文件：")}{planFilePath}</div>}
      {commandPrompts.length > 0 && (
        <div className="inline-prompt-permissions">
          <strong>{tr("请求的实现权限")}</strong>
          {commandPrompts.map((item, index) => (
            <span key={`${index}-${item.tool}-${item.prompt}`}>{item.tool}：{item.prompt}</span>
          ))}
        </div>
      )}
      {request.status === "error" && request.error && <div role="alert" className="inline-prompt-error">{request.error}</div>}

      {responding && <div className="inline-prompt-response-status" role="status"><LoaderCircle size={13} className="animate-spin" aria-hidden="true" />{tr("正在提交计划…")}</div>}

      {rejecting && (
        <textarea
          value={rejectionFeedback}
          onChange={(event) => updatePlanDraft({ rejectionFeedback: event.target.value })}
          placeholder={tr("说明需要调整的内容…")}
          aria-label={tr("计划拒绝反馈")}
          rows={3}
          disabled={responding}
          autoFocus
          className="inline-prompt-plan-editor"
        />
      )}

      <div className="inline-prompt-button-row">
        <Button variant="secondary" size="sm" onClick={() => updatePlanDraft({ editing: !editing })} disabled={responding}>
          {editing ? tr("预览计划") : tr("编辑计划")}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => rejecting ? void respond(false) : updatePlanDraft({ rejecting: true })}
          disabled={responding}
          aria-label={rejecting ? tr("提交计划拒绝反馈") : tr("拒绝计划")}
        >
          <X size={14} />
          {rejecting ? tr("提交拒绝") : tr("拒绝")}
        </Button>
        <Button
          variant="primary"
          size="sm"
          onClick={() => void respond(true)}
          disabled={responding || !plan.trim()}
          aria-label={tr("批准计划并开始实现")}
        >
          <Check size={14} />
          {tr("批准并开始实现")}</Button>
      </div>
    </section>
  );
};

export const SubagentPlanReviewCard = (
  { request, review }: { request: PendingAskUser; review: PendingSubagentPlanReview },
) => {
  const { t: tr } = useI18n();
  const [responding, setResponding] = useState(false);
  const [error, setError] = useState("");
  const plan = review.planContent ?? "";
  const subagents = useAppStore((s) => s.subagents);
  const teammate = approvalSourceLabel(tr, {
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
      if (!commandResultSucceeded(result)) throw new Error(result.message || tr("计划审批未被后端接受"));
      const store = useAppStore.getState();
      const agents = request.conversationId && request.conversationId !== store.conversationId
        ? store.conversationAgentStates[request.conversationId]?.subagents ?? [] : store.subagents;
      const agent = agents.find((item) => item.id === review.subagentId);
      if (agent?.activePlanRequestId === request.requestId) {
        store.updateSubagent(agent.id, { awaitingPlanApproval: false, activePlanRequestId: "" }, request.conversationId);
      }
      store.clearAskUser(request.requestId, request.conversationId, request);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : tr("计划审批提交失败，请重试。");
      setError(message);
      pushToast(message, "error", 4500);
      setResponding(false);
    }
  };

  return (
    <section className="inline-prompt-card inline-prompt-plan-card" aria-label={tr("子智能体计划审批")} aria-busy={responding}>
      <div className="inline-prompt-header">
        <ShieldCheck size={16} color="var(--accent-primary)" />
        <div className="inline-prompt-header-copy">
          <div className="inline-prompt-title">{tr("批准子智能体的计划？")}</div>
          <div className="inline-prompt-question">{teammate} {tr("提交了计划，需要你批准后才能开始实现。")}</div>
          {review.teamName && <div className="inline-prompt-subtitle">{tr("团队：")}{review.teamName}</div>}
          {error && <div role="alert" className="inline-prompt-error inline-prompt-ask-error">{error}</div>}
        </div>
      </div>

      <div className="inline-prompt-plan-document" role="region" aria-label={tr("计划内容")} tabIndex={0}>
        {plan.trim()
          ? <MarkdownRenderer content={plan} />
          : <div className="inline-prompt-error">{tr("子智能体没有提交计划内容。请拒绝，让它先写入计划文件。")}</div>}
      </div>

      {review.plan_file_path && <div className="inline-prompt-plan-path">{tr("计划文件：")}{review.plan_file_path}</div>}

      {responding && <div className="inline-prompt-response-status" role="status"><LoaderCircle size={13} className="animate-spin" aria-hidden="true" />{tr("正在提交计划…")}</div>}

      <div className="inline-prompt-button-row">
        <Button
          variant="secondary"
          size="sm"
          disabled={responding}
          onClick={() => void respond(false)}
          aria-label={tr("拒绝子智能体的计划")}
        >
          <X size={14} />
          {tr("拒绝")}</Button>
        <Button
          variant="primary"
          size="sm"
          disabled={responding || !plan.trim()}
          onClick={() => void respond(true)}
          aria-label={tr("批准子智能体的计划")}
        >
          <Check size={14} />
          {tr("批准")}</Button>
      </div>
    </section>
  );
};

const displayToolName = (name: string): string => {
  const readable = readableToolLabel(name);
  return readable === name ? humanizeKey(name) : readable;
};

function approvalSourceLabel(tr: Translate, request: PendingApproval, subagents: SubagentState[], teammateName?: string): string {
  const source = String(request.sourceAgent || "").trim();
  if (!source) return request.sourceThread ? tr("智能体") : "";
  if (source === "parent") return tr("主智能体");
  if (source === "*" || source === "all") return tr("所有智能体");
  const agent = subagents.find((item) => item.id === source || item.role === source);
  const label = teammateName || agent?.role || (source.includes("@") ? source.split("@")[0] : "");
  return !label || /^(?:(?:subagent|agent|thread|call|session|run)[-_:][\w-]+|[a-f0-9]{8,}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$/i.test(label)
    ? tr("智能体")
    : label;
}

function isEscalatedApproval(request: PendingApproval): boolean {
  return request.args?.sandbox_permissions === "require_escalated";
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
  const { t: tr } = useI18n();
  const diffReview = useAppStore((s) => s.diffReview);
  const stats = useMemo(() => diffStats(request.diff), [request.diff]);
  const expiry = useApprovalExpiry(request.expiresAt);
  const [localResponding, setResponding] = useState(false);
  const responding = localResponding || request.reviewState?.status === "submitted";
  const [error, setError] = useState("");
  const displayedError = error || request.reviewState?.error;

  const respond = async (allowed: boolean) => {
    if (responding) return;
    setResponding(true);
    setError("");
    useAppStore.getState().updateDiffReviewStatus(request.requestId, { status: "submitted", error: undefined }, request);
    try {
      const command = buildApprovalResponseCommand(
        request.requestId,
        allowed ? "approve" : "reject",
        { owner: request },
      );
      const result = await sendPromptResponseCommand(command);
      if (!commandResultSucceeded(result)) throw new Error(result.message || tr("审批未被后端接受"));
      useAppStore.getState().updateDiffReviewStatus(request.requestId, { status: allowed ? "approved" : "rejected", error: undefined }, request);
      useAppStore.getState().clearDiffReview(request.requestId, request);
    } catch (error) {
      setError(error instanceof Error ? error.message : tr("审批提交失败"));
      useAppStore.getState().updateDiffReviewStatus(request.requestId, { status: "error", error: error instanceof Error ? error.message : tr("审批提交失败") }, request);
      setResponding(false);
    }
  };

  const openDiff = () => {
    const store = useAppStore.getState();
    if (request.reviewState) store.setDiffReviewState(request.reviewState);
    store.addPanel({
      id: "approval-diff",
      kind: "diff",
      label: tr("差异审阅"),
    });
  };

  return (
    <section className="inline-prompt-card inline-approval-bar" aria-busy={responding}>
      <div className="inline-approval-heading"><Hand size={18} aria-hidden="true" /><span>{tr("权限")}</span></div>
      <div className="inline-prompt-header">
        <div className="inline-prompt-header-copy">
          <div className="inline-prompt-title">{tr("审阅文件更改")}</div>
          <div className="inline-prompt-subtitle">
            {request.filePath
              || request.reviewState?.toolName
              || (diffReview?.requestId === request.requestId && pendingPromptMatchesOwner(diffReview, request) ? diffReview.toolName : "")
              || tr("工具编辑")} · <span className="chat-change-added">+{stats.plus}</span>{" "}
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
        )) : <span className="inline-prompt-context">{tr("打开差异面板检查拟议更改。")}</span>}
      </div>

      {displayedError && <div role="alert" className="inline-prompt-error">{displayedError}</div>}
      <div className="inline-prompt-actions inline-prompt-button-row">
        <Button variant="ghost" size="sm" onClick={openDiff}>
          <ExternalLink size={14} />{tr("打开差异")}</Button>
        <ApprovalActions responding={responding} onDeny={() => void respond(false)} onAllow={() => void respond(true)}
          denyLabel={tr("拒绝文件更改")} allowLabel={tr("允许文件更改")} />
      </div>
    </section>
  );
};

const useApprovalExpiry = (expiresAt?: number): { label: string; urgent: boolean } => {
  const { t: tr } = useI18n();
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
    label: remaining > 0 ? tr("将在 {v0}:{v1} 后过期", { v0: minutes, v1: seconds }) : tr("授权已过期"),
    urgent: remaining <= 60,
  };
};

const AskUserCard = ({ request }: { request: PendingAskUser }) => {
  const { t: tr } = useI18n();
  const draft = useAppStore((s) => s.promptDrafts[promptDraftKey(request)]);
  const updatePromptDraft = useAppStore((s) => s.updatePromptDraft);
  const persistDraft = !request.secret && !request.provider && request.promptType !== "secret" && request.promptType !== "manual_code";
  const [transientAnswer, setTransientAnswer] = useState("");
  const [transientOption, setTransientOption] = useState<number | null>(null);
  const [transientValues, setTransientValues] = useState<Record<string, string | boolean | string[] | undefined>>({});
  const fields = Object.entries((request.inputSchema?.properties ?? {}) as Record<string, ElicitationField>);
  const structured = request.inputSchema?.type === "object";
  const requiredFields = (request.inputSchema?.required ?? []) as string[];
  const defaultValues = elicitationDefaults(fields, requiredFields);
  const fieldValues = { ...defaultValues, ...(persistDraft ? draft?.elicitationValues : transientValues) };
  const updateField = (name: string, value: string | boolean | string[] | undefined) => {
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
  const formContent = elicitationContent(fields, requiredFields, fieldValues);
  const canSubmit = structured ? !formContent.error : selectedOption !== null || request.allowEmpty === true || answer.length > 0;

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
      const owner = request;
      if (structured && formContent.error) throw new Error(formContent.error);
      const command = structured ? buildElicitationResponseCommand(request.requestId, "accept", formContent.content, owner)
        : buildAskUserResponseCommand(request.requestId, text, owner);
      const result = await sendPromptResponseCommand(command);
      if (!commandResultSucceeded(result)) throw new Error(result.message || tr("回答未被后端接受"));
      useAppStore.getState().clearAskUser(request.requestId, request.conversationId, request);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : tr("回答发送失败，请重试。");
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
        request) : {
        type: "control_cancel_request" as const,
        request_id: request.requestId,
        ...(request.conversationId ? { conversation_id: request.conversationId } : {}),
        ...(request.turnId ? { turn_id: request.turnId } : {}),
        ...(request.messageId ? { message_id: request.messageId } : {}),
        ...(request.taskId ? { task_id: request.taskId } : {}),
      };
      const result = await sendPromptResponseCommand(command);
      if (!commandResultSucceeded(result)) throw new Error(result.message || tr("取消请求未被后端接受"));
      useAppStore.getState().clearAskUser(request.requestId, request.conversationId, request);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : tr("取消发送失败，请重试。");
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
          <div className="inline-prompt-title">{request.provider ? tr("提供商需要认证输入") : tr("Agent 需要你的输入")}</div>
          {request.provider && <div className="inline-prompt-subtitle">{tr("认证提供商：")}{request.provider}</div>}
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
        const choices = elicitationChoices(field);
        return <label className="inline-prompt-input-row" key={name}>
          <div className="inline-prompt-input-wrap">
            <div className="inline-prompt-input-label">{label}{requiredFields.includes(name) ? " *" : ""}</div>
            {field.description && <div className="inline-prompt-subtitle">{field.description}</div>}
            {field.type === "boolean" ? <input type="checkbox" aria-label={label} checked={value === true} disabled={responding}
              onChange={(event) => updateField(name, event.target.checked)} />
              : choices ? <select className="inline-prompt-input" aria-label={label} multiple={field.type === "array"}
                disabled={responding} value={field.type === "array" ? (value as string[] ?? []) : value === undefined ? "" : String(choices.findIndex(choice => choice.const === value))}
                onChange={(event) => updateField(name, field.type === "array" ? Array.from(event.target.selectedOptions).map((option) => option.value)
                  : event.target.value === "" ? undefined : choices[Number(event.target.value)].const)}>
                {field.type !== "array" && <option value="">{tr("请选择")}</option>}
                {choices.map((choice, index) => <option key={choice.const} value={field.type === "array" ? choice.const : String(index)}>{choice.title}</option>)}
              </select> : <input className="inline-prompt-input" aria-label={label}
                type={field.type === "number" || field.type === "integer" ? "number" : field.format === "email" ? "email" : "text"}
                value={String(value ?? "")} disabled={responding}
                min={field.minimum} max={field.maximum} step={field.type === "integer" ? 1 : "any"}
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
                {tr("自定义回答")}</div>
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
              placeholder={request.placeholder || (hasOptions ? tr("输入自定义回答…") : tr("输入你的回答…"))}
              aria-label={request.secret ? tr("输入认证密钥") : tr("回答 Agent 的问题")}
              autoComplete={request.secret ? "new-password" : "off"}
              spellCheck={false}
              className="inline-prompt-input"
            />
          </div>
        </div>
      )}

      <div className="inline-prompt-footer inline-prompt-button-row">
        <span className="inline-prompt-response-status" role="status">
          {responding ? <><LoaderCircle size={13} className="animate-spin" aria-hidden="true" />{tr("正在提交…")}</> : hasOptions ? tr("选好后点击继续") : null}
        </span>
        <Button variant="secondary" size="sm" disabled={responding} onClick={() => void cancel()}>
          <X size={14} />
          {tr("取消")}</Button>
        <Button type="submit" variant="primary" size="sm" disabled={!canSubmit || responding}>
          {hasOptions || !hasCustomInput ? tr("继续") : tr("发送")}
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
