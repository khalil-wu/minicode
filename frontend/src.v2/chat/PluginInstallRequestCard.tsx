import { useRef } from "react";
import { Button } from "../components/Button";
import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import type { PendingAskUser } from "../stores/types";
import { apiBase, authHeaders, fetchWithTimeout, LONG_HTTP_TIMEOUT_MS } from "../protocol/api";
import { getWebSocket } from "../hooks/useWebSocket";
import { buildElicitationResponseCommand } from "../protocol/prompt-responses";
import { commandResultSucceeded, sendPromptResponseCommand } from "../protocol/ws-outbox";
import { useApprovalShortcuts } from "./useApprovalShortcuts";

interface PluginInstallMeta extends Record<string, unknown> {
  codex_approval_kind: "tool_suggestion"; tool_type: "plugin"; suggest_type: "install";
  tool_id: string; tool_name: string; suggest_reason: string; suggestion_id: string; persist: "always";
}
type Request = PendingAskUser & { elicitation: NonNullable<PendingAskUser["elicitation"]> & { meta: PluginInstallMeta } };
export const isPluginInstallRequest = (request: PendingAskUser): request is Request =>
  request.elicitation?.mode === "form" && request.elicitation.meta?.codex_approval_kind === "tool_suggestion"
    && request.elicitation.meta.tool_type === "plugin" && request.elicitation.meta.suggest_type === "install";

export function PluginInstallRequestCard({ request }: { request: Request }) {
  const { t } = useI18n();
  const busy = useRef(false);
  const responding = request.status === "submitted";
  const meta = request.elicitation.meta;
  const respond = async (action: "accept" | "decline", persist = false) => {
    if (busy.current) return;
    busy.current = true;
    const owner = { conversationId: request.conversationId, turnId: request.turnId, messageId: request.messageId, taskId: request.taskId };
    useAppStore.getState().updateAskUserStatus(request.requestId, { status: "submitted", error: undefined }, owner);
    try {
      if (action === "accept") {
        const response = await fetchWithTimeout(`${apiBase()}/api/plugins/install-request`, {
          method: "POST", headers: authHeaders({ "content-type": "application/json" }),
          body: JSON.stringify({ session_id: getWebSocket()!.sessionId, request_id: request.requestId,
            conversation_id: request.conversationId, turn_id: request.turnId, message_id: request.messageId ?? "" }),
        }, { timeoutMs: LONG_HTTP_TIMEOUT_MS, timeoutMessage: t("插件安装超时，请重试查看安装状态。") });
        const result = await response.json();
        if (!response.ok || !result.installed) throw new Error(result.detail || t("安装插件失败"));
      }
      const command = buildElicitationResponseCommand(request.requestId, action, undefined, owner);
      if (persist && command.type === "control_response") command.response.response._meta = { persist: "always" };
      const result = await sendPromptResponseCommand(command);
      if (!commandResultSucceeded(result)) throw new Error(result.message || t("回答未被后端接受"));
      useAppStore.getState().clearAskUser(request.requestId, request.conversationId, owner);
    } catch (error) {
      useAppStore.getState().updateAskUserStatus(request.requestId, { status: "error", error: String(error instanceof Error ? error.message : error) }, owner);
      busy.current = false;
    }
  };
  const actions = useApprovalShortcuts({ responding, onDeny: () => void respond("decline"), onAllow: () => void respond("accept") });
  return <section className="inline-approval-bar" aria-label={t("安装插件")} aria-busy={responding}>
    <div className="inline-approval-main"><div className="inline-prompt-title">{meta.tool_name}</div>
      <div className="inline-prompt-subtitle">{meta.suggest_reason}</div></div>
    {request.error && <div role="alert" className="inline-prompt-error">{request.error}</div>}
    <div ref={actions} className="inline-approval-actions">
      <Button variant="secondary" size="sm" className="inline-approval-leading" disabled={responding}
        onClick={() => void respond("decline", true)}>{t("不再建议此插件")}</Button>
      <Button variant="secondary" size="sm" className="inline-approval-deny" disabled={responding}
        onClick={() => void respond("decline")}>{t("暂不安装")}<kbd>Esc</kbd></Button>
      <div className="inline-approval-allow-group"><Button variant="primary" size="sm" className="inline-approval-allow"
        disabled={responding} loading={responding} onClick={() => void respond("accept")}>{t("安装插件")}{!responding && <kbd>↵</kbd>}</Button></div>
    </div>
  </section>;
}
