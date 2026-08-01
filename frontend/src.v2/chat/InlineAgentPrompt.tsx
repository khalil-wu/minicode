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
import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import type { PendingApproval, PendingAskUser, PendingDiffReview } from "../stores/types";
import { pendingPromptTargetsConversation } from "../lib/pending-prompts";
import { ToolGlyph, summarizeArgs, humanizeKey } from "./toolUtils";
import { deriveCommandPrefix } from "./commandPrefix";

export const InlineAgentPrompt = () => {
  const pendingApproval = useAppStore((s) => s.pendingApproval);
  const approvalQueue = useAppStore((s) => s.approvalQueue);
  const pendingDiffReview = useAppStore((s) => s.pendingDiffReview);
  const pendingAskUser = useAppStore((s) => s.pendingAskUser);
  const activeConversationId = useAppStore((s) => s.conversationId);
  const primaryVisibleApproval = pendingPromptTargetsConversation(pendingApproval, activeConversationId, activeConversationId)
    ? pendingApproval
    : null;
  const visibleDiffReview = pendingPromptTargetsConversation(pendingDiffReview, activeConversationId, activeConversationId)
    ? pendingDiffReview
    : null;
  const visibleAskUser = pendingPromptTargetsConversation(pendingAskUser, activeConversationId, activeConversationId)
    ? pendingAskUser
    : null;
  const visibleApprovalQueue = approvalQueue.filter((item) =>
    pendingPromptTargetsConversation(item, activeConversationId, activeConversationId),
  );
  const visibleApproval = primaryVisibleApproval ?? visibleApprovalQueue[0] ?? null;
  const queuedApprovals = visibleApproval
    ? visibleApprovalQueue.filter((item) => item.requestId !== visibleApproval.requestId)
    : [];

  if (!visibleApproval && !visibleDiffReview && !visibleAskUser) return null;

  return (
    <div style={shellStyle} aria-label="Agent is waiting for input">
      {visibleDiffReview && <DiffApprovalCard request={visibleDiffReview} />}
      {visibleApproval && <ToolApprovalCard request={visibleApproval} queue={queuedApprovals} />}
      {visibleAskUser && <AskUserCard request={visibleAskUser} />}
    </div>
  );
};

const ToolApprovalCard = ({ request, queue }: { request: PendingApproval; queue: PendingApproval[] }) => {
  const [responding, setResponding] = useState(false);
  const [amending, setAmending] = useState(false);
  const [feedback, setFeedback] = useState("");
  const summary = useMemo(() => summarizeArgs(request.args), [request.args]);
  const total = 1 + queue.length;
  const displayName = humanizeKey(request.toolName);
  // Codex escalate-on-failure: a command retried with escalated permissions
  // carries with_escalated_permissions + a justification in its args. Surface it
  // prominently so the user understands they are approving full (unsandboxed)
  // access, not an ordinary command.
  const escalated = isEscalatedApproval(request);
  const escalationJustification = String(request.args?.justification ?? "").trim();
  const sourceLabel = approvalSourceLabel(request);
  const samplingPromptPreview = request.toolName.startsWith("mcp_sampling:")
    ? String(request.args?.prompt_preview ?? "").trim()
    : "";
  const samplingPreviewTruncated = request.args?.prompt_preview_truncated === true;

  useEffect(() => {
    setResponding(false);
    setAmending(false);
    setFeedback("");
  }, [request.requestId]);

  const respond = (allowed: boolean, fb?: string) => {
    if (responding) return;
    setResponding(true);
    const sent = sendClientCommand(buildApprovalResponseCommand(request.requestId, allowed ? "approve" : "reject", request.protocol, undefined, fb));
    if (sent) {
      useAppStore.getState().clearApproval(request.requestId);
    } else {
      useAppStore.getState().markApprovalError(request.requestId, "Connection is offline");
      setResponding(false);
    }
  };

  const allowAll = () => {
    const store = useAppStore.getState();
    // "Allow all" is a bulk convenience — it must NOT silently approve elevated
    // (unsandboxed / escalated) requests. Those stay queued for an explicit,
    // individually-reviewed decision so the user always sees the sandbox warning.
    const all = [request, ...queue].filter((item) => !isEscalatedApproval(item));
    const submitted: string[] = [];
    for (const item of all) {
      const sent = sendClientCommand(buildApprovalResponseCommand(item.requestId, "approve", item.protocol));
      if (sent) submitted.push(item.requestId);
      else store.markApprovalError(item.requestId, "Connection is offline");
    }
    if (submitted.length > 0) store.clearApprovals(submitted);
  };
  // Escalated items are excluded from bulk approval; surface that in the label.
  const escalatedQueueCount = [request, ...queue].filter(isEscalatedApproval).length;

  // "Always allow <prefix>": persist a Bash(prefix:*) content rule so future
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
    const rule = `Bash(${alwaysPrefix}:*)`;
    try {
      const result = await sendClientCommandAwaitResult({
        type: "permissions.content_rule.add",
        rule,
        deny: false,
        source: "approval.always_allow_prefix",
      }, "permissions.content_rule.add");
      const failed = ["error", "failed", "warning"].includes(String(result.level || "").toLowerCase());
      if (failed || result.data?.rule !== rule || result.data?.deny === true) {
        throw new Error(result.message || "The permission rule was not saved.");
      }
      const sent = sendClientCommand(
        buildApprovalResponseCommand(request.requestId, "approve", request.protocol),
      );
      if (!sent) throw new Error("Connection is offline");
      useAppStore.getState().clearApproval(request.requestId);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to save the permission rule";
      useAppStore.getState().markApprovalError(request.requestId, message);
      setResponding(false);
    }
  };

  return (
    <section style={approvalBarStyle}>
      <div style={approvalIconStyle}>
        <ToolGlyph />
      </div>

      <div style={approvalMainStyle}>
        <div style={approvalTitleRowStyle}>
          <span style={titleStyle}>Allow {displayName}?</span>
          {total > 1 && <span style={pendingPillStyle}>{total} pending</span>}
        </div>
        <div style={subtitleStyle}>
          {escalated
            ? "Elevated access requested — this will run outside the sandbox (full filesystem + network)."
            : "Permission required before this tool can run."}
          {sourceLabel ? ` Source: ${sourceLabel}.` : ""}
        </div>

        {escalated && (
          <div style={escalationBannerStyle}>
            <ShieldAlert size={14} style={{ flexShrink: 0, marginTop: 1 }} />
            <span>
              <strong>Run outside sandbox.</strong>
              {escalationJustification ? ` ${escalationJustification}` : " The agent says a sandboxed run failed and needs full access."}
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
          <pre style={fullCommandStyle} aria-label="Command to run">{commandText}</pre>
        )}

        {samplingPromptPreview && (
          <div style={samplingPreviewStyle}>
            <strong>Untrusted MCP prompt</strong>
            <pre style={samplingPreviewTextStyle} aria-label="MCP prompt sent to the model">
              {samplingPromptPreview}
            </pre>
            {samplingPreviewTruncated && (
              <span style={samplingPreviewHintStyle}>Preview truncated; deny this request to avoid sending omitted content.</span>
            )}
          </div>
        )}

        {amending && (
          <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 6 }}>
            <textarea
              value={feedback}
              onChange={(e) => setFeedback(e.target.value)}
              placeholder="Add guidance for the agent (e.g. why you're denying, or what to change)…"
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
              <button type="button" onClick={() => respond(false, feedback)} disabled={responding || !feedback.trim()} style={primaryButtonStyle}>
                Deny with note
              </button>
              <button type="button" onClick={() => setAmending(false)} disabled={responding} style={secondaryButtonStyle}>
                Cancel
              </button>
            </div>
          </div>
        )}

        {queue.length > 0 && (
          <div style={queueStyle}>
            Next: {queue.map((item) => humanizeKey(item.toolName)).join(", ")}
          </div>
        )}
        {request.status === "error" && request.error && (
          <div style={errorStyle}>{request.error}</div>
        )}
      </div>

      <div style={compactButtonRowStyle}>
        <button type="button" onClick={() => respond(false)} disabled={responding} style={secondaryButtonStyle} aria-label="Deny tool use">
          <X size={14} />
          Deny
        </button>
        <button type="button" onClick={() => respond(true)} disabled={responding} style={primaryButtonStyle} aria-label="Allow tool use">
          <Check size={14} />
          Allow
        </button>
        <button type="button" onClick={() => setAmending((v) => !v)} disabled={responding} style={secondaryButtonStyle} aria-label="Amend with feedback" title="Add guidance to your decision (cc Tab-to-amend)">
          <MessageSquare size={14} />
          Amend
        </button>
        {alwaysPrefix && (
          <button
            type="button"
            onClick={alwaysAllowPrefix}
            disabled={responding}
            style={accentButtonStyle}
            aria-label={`Always allow ${alwaysPrefix} commands without prompting`}
            title={`Always allow "${alwaysPrefix}" commands without prompting`}
          >
            <ShieldCheck size={14} />
            Always {alwaysPrefix}
          </button>
        )}
        {queue.length > 0 && (
          <button
            type="button"
            onClick={allowAll}
            disabled={responding}
            style={accentButtonStyle}
            aria-label="Allow all non-elevated pending tool requests"
            title={escalatedQueueCount > 0
              ? `Approves queued requests; ${escalatedQueueCount} elevated request(s) still need individual review`
              : "Allow all pending tool requests"}
          >
            {escalatedQueueCount > 0 ? "Allow non-elevated" : "Allow all"}
          </button>
        )}
      </div>
    </section>
  );
};

function approvalSourceLabel(request: PendingApproval): string {
  const agent = String(request.sourceAgent || "").trim();
  const thread = String(request.sourceThread || "").trim();
  if (agent && thread) return `${agent} in ${thread}`;
  return agent || thread;
}

// A request retried with escalated permissions runs OUTSIDE the sandbox
// (full filesystem + network). These must always be reviewed individually.
function isEscalatedApproval(request: PendingApproval): boolean {
  return request.args?.with_escalated_permissions === true
    || request.args?.with_escalated_permissions === "true";
}

const DiffApprovalCard = ({ request }: { request: PendingDiffReview }) => {
  const diffReview = useAppStore((s) => s.diffReview);
  const stats = useMemo(() => diffStats(request.diff), [request.diff]);

  const respond = (allowed: boolean) => {
    const sent = sendClientCommand(buildApprovalResponseCommand(request.requestId, allowed ? "approve" : "reject", request.protocol));
    if (!sent) return;
    const current = useAppStore.getState().diffReview;
    if (current?.requestId === request.requestId) {
      useAppStore.getState().setDiffReviewState({
        ...current,
        status: allowed ? "approved" : "rejected",
      });
    }
    useAppStore.getState().clearDiffReview();
  };

  const openDiff = () => {
    useAppStore.getState().setRightStackTab("inspector");
    useAppStore.getState().addPanel({
      id: "approval-diff",
      kind: "diff",
      label: "Diff Review",
    });
  };

  return (
    <section style={cardStyle}>
      <div style={headerStyle}>
        <FileDiff size={16} color="var(--accent-primary)" />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={titleStyle}>Review file changes</div>
          <div style={subtitleStyle}>
            {request.filePath || diffReview?.toolName || "Tool edit"} · <span style={{ color: "var(--state-success)" }}>+{stats.plus}</span>{" "}
            <span style={{ color: "var(--state-danger)" }}>-{stats.minus}</span>
          </div>
        </div>
      </div>

      <div style={diffPreviewStyle}>
        {stats.preview.length > 0 ? stats.preview.map((line, index) => (
          <div key={`${index}-${line}`} style={diffLineStyle(line)}>
            {line}
          </div>
        )) : <span style={{ color: "var(--text-muted)" }}>Open the diff panel to inspect the proposed changes.</span>}
      </div>

      <div style={buttonRowStyle}>
        <button type="button" onClick={openDiff} style={secondaryButtonStyle}>
          <ExternalLink size={14} />
          Open diff
        </button>
        <button type="button" onClick={() => respond(false)} style={secondaryButtonStyle} aria-label="Deny file changes">
          <X size={14} />
          Deny
        </button>
        <button type="button" onClick={() => respond(true)} style={primaryButtonStyle} aria-label="Allow file changes">
          <Check size={14} />
          Allow
        </button>
      </div>
    </section>
  );
};

const AskUserCard = ({ request }: { request: PendingAskUser }) => {
  const [answer, setAnswer] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const hasOptions = Boolean(request.options && request.options.length > 0);

  useEffect(() => {
    setAnswer("");
    if (!hasOptions) window.setTimeout(() => inputRef.current?.focus(), 40);
  }, [request.requestId]);

  const respond = (text: string) => {
    const sent = sendClientCommand(buildAskUserResponseCommand(request.requestId, text, request.protocol));
    if (sent) useAppStore.getState().clearAskUser();
  };

  return (
    <section style={{ ...cardStyle, ...askUserCardStyle }}>
      <div style={headerStyle}>
        <MessageSquare size={16} color="var(--accent-primary)" />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={titleStyle}>Agent needs input</div>
          <div style={askSubtitleStyle}>{request.question}</div>
        </div>
      </div>

      {hasOptions && (
        <div style={choiceGridStyle}>
          {request.options?.map((option, index) => (
            <button key={option} type="button" onClick={() => respond(option)} style={choiceCardStyle}>
              <span style={choiceLetterStyle}>{optionLetter(index)}</span>
              <span style={choiceCardTitleStyle}>{option}</span>
            </button>
          ))}
        </div>
      )}

      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (answer.trim()) respond(answer.trim());
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
            value={answer}
            onChange={(event) => setAnswer(event.target.value)}
            placeholder={hasOptions ? "Type a custom answer..." : "Type your answer..."}
            style={inputStyle}
          />
        </div>
        <button type="submit" disabled={!answer.trim()} style={{ ...primaryButtonStyle, opacity: answer.trim() ? 1 : 0.55 }}>
          Send
        </button>
      </form>
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
  gridTemplateColumns: "26px minmax(0, 1fr) auto",
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
  fontWeight: 650,
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
  minWidth: "max-content",
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

const samplingPreviewStyle: React.CSSProperties = {
  marginTop: 4,
  display: "grid",
  gap: 4,
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
};

const samplingPreviewTextStyle: React.CSSProperties = {
  ...fullCommandStyle,
  margin: 0,
  maxHeight: 180,
};

const samplingPreviewHintStyle: React.CSSProperties = {
  color: "var(--state-warning)",
};

const cardStyle: React.CSSProperties = {
  border: "1px solid color-mix(in oklch, var(--state-warning) 45%, var(--border-subtle))",
  background: "color-mix(in oklch, var(--state-warning) 8%, var(--surface-page))",
  borderRadius: "var(--radius-sm, 6px)",
  padding: 10,
  display: "grid",
  gap: 9,
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
  fontWeight: 700,
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

const baseButtonStyle: React.CSSProperties = {
  height: 28,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 5,
  borderRadius: "var(--radius-sm, 4px)",
  padding: "0 10px",
  fontSize: "var(--text-xs)",
  fontWeight: 650,
  cursor: "pointer",
};

const primaryButtonStyle: React.CSSProperties = {
  ...baseButtonStyle,
  border: 0,
  background: "var(--accent-primary)",
  color: "var(--text-on-accent)",
};

const accentButtonStyle: React.CSSProperties = {
  ...baseButtonStyle,
  border: "1px solid color-mix(in oklch, var(--accent-primary) 40%, var(--border-subtle))",
  background: "color-mix(in oklch, var(--accent-primary) 8%, var(--surface-base))",
  color: "var(--accent-primary)",
};

const secondaryButtonStyle: React.CSSProperties = {
  ...baseButtonStyle,
  border: "1px solid var(--border-subtle)",
  background: "var(--surface-soft)",
  color: "var(--text-secondary)",
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
  fontWeight: 750,
  lineHeight: 1,
  flexShrink: 0,
};

const choiceCardTitleStyle: React.CSSProperties = {
  fontSize: "var(--text-sm)",
  fontWeight: 650,
  lineHeight: 1.35,
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
  fontWeight: 700,
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
