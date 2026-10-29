import { useRef, useState } from "react";
import { TriangleAlert } from "../lib/icons";
import { Button } from "../components/Button";
import { useI18n } from "../i18n";
import { buildElicitationResponseCommand } from "../protocol/prompt-responses";
import { commandResultSucceeded, sendPromptResponseCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import type { PendingAskUser } from "../stores/types";
import { useApprovalShortcuts } from "./useApprovalShortcuts";

type PersistMode = "session" | "always";
interface ComputerUseMeta extends Record<string, unknown> {
  codex_approval_kind: "mcp_tool_call";
  connector_id: "computer-use";
  connector_name: string;
  persist: PersistMode[];
  riskLevel: "low" | "high";
  tool_params: { app: string };
  tool_params_display: { name: string; display_name: string; value: string }[];
}
type ComputerUseRequest = PendingAskUser & {
  elicitation: NonNullable<PendingAskUser["elicitation"]> & { meta: ComputerUseMeta };
};
const approvedAppDisclosureKey = "minicode:has-approved-computer-use-app";

export function isComputerUseApproval(request: PendingAskUser): request is ComputerUseRequest {
  return request.elicitation?.mode === "form"
    && request.elicitation.meta?.codex_approval_kind === "mcp_tool_call"
    && request.elicitation.meta.connector_id === "computer-use";
}

// Source computer-use-app-approval-request-card: this is app access, not a
// generic empty MCP form. The existing prompt owner/queue still owns settlement.
export function ComputerUseApprovalCard({ request }: { request: ComputerUseRequest }) {
  const { t } = useI18n();
  const meta = request.elicitation.meta;
  const appDisplayName = meta.tool_params_display.find(parameter => parameter.name === "app")!.value;
  const title = t("允许 ChatGPT 使用 {appDisplayName}？", { appDisplayName });
  const [showDisclosure] = useState(() => localStorage.getItem(approvedAppDisclosureKey) !== "true");
  const [responding, setResponding] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef(false);
  const respond = async (action: "accept" | "decline", persist?: PersistMode) => {
    if (pending.current) return;
    pending.current = true;
    setResponding(true);
    setError("");
    const owner = { conversationId: request.conversationId, turnId: request.turnId,
      messageId: request.messageId, taskId: request.taskId };
    try {
      const result = await sendPromptResponseCommand(buildElicitationResponseCommand(request.requestId,
        action, action === "accept" && persist ? { persist } : undefined, owner));
      if (!commandResultSucceeded(result)) throw new Error(result.message || t("回答未被后端接受"));
      if (action === "accept") localStorage.setItem(approvedAppDisclosureKey, "true");
      useAppStore.getState().clearAskUser(request.requestId, request.conversationId,
        { turnId: request.turnId, messageId: request.messageId, taskId: request.taskId });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      pending.current = false;
      setResponding(false);
    }
  };
  const allowConversation = () => void respond("accept", meta.persist.includes("session") ? "session" : undefined);
  const actions = useApprovalShortcuts({ responding, onDeny: () => void respond("decline"), onAllow: allowConversation });
  const elevated = meta.riskLevel === "high";
  return <section className="inline-approval-bar inline-computer-use-approval" aria-label={title}
    aria-busy={responding} data-computer-use-app-id={meta.tool_params.app}>
    <div className="inline-approval-heading" style={elevated ? { color: "var(--warning-yellow)" } : undefined}>
      {elevated ? <TriangleAlert size={16} aria-hidden="true" /> : <img alt="" draggable={false}
        src={`${import.meta.env.BASE_URL}apps/computer-use.png`}
        style={{ width: 16, height: 16, borderRadius: 3, objectFit: "contain" }} />}
      <span>{elevated ? t("较高风险") : meta.connector_name}</span>
    </div>
    <div className="inline-approval-main">
      <div className="inline-prompt-title">{title}</div>
      {showDisclosure && <div className="inline-prompt-subtitle">{t("电脑操作应用访问说明")}</div>}
    </div>
    {error && <div role="alert" className="inline-prompt-error">{error}</div>}
    <div ref={actions} className="inline-approval-actions">
      {meta.persist.includes("always") && <Button variant="secondary" size="sm" className="inline-approval-leading"
        disabled={responding} onClick={() => void respond("accept", "always")}>{t("始终允许")}</Button>}
      <Button variant="secondary" size="sm" className="inline-approval-deny" disabled={responding}
        onClick={() => void respond("decline")}>{t("拒绝")}<kbd>Esc</kbd></Button>
      <div className="inline-approval-allow-group"><Button variant="primary" size="sm" className="inline-approval-allow"
        disabled={responding} loading={responding} onClick={allowConversation}>{t("允许此对话")}{!responding && <kbd>↵</kbd>}</Button></div>
    </div>
  </section>;
}
