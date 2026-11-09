import { ExternalLink, LoaderCircle } from "../lib/icons";
import { useState } from "react";
import { useI18n } from "../i18n";
import { openPreferredWebLink } from "../lib/open-preferred-target";
import { buildElicitationResponseCommand } from "../protocol/prompt-responses";
import { commandResultSucceeded, sendPromptResponseCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import type { PendingAskUser } from "../stores/types";

export function McpUrlElicitationCard({ request }: { request: PendingAskUser & { elicitation: NonNullable<PendingAskUser["elicitation"]> } }) {
  const { t } = useI18n();
  const [opened, setOpened] = useState(false);
  const responding = request.status === "submitted";
  const [opening, setOpening] = useState(false);
  const respond = async (action: "accept" | "decline" | "cancel") => {
    useAppStore.getState().updateAskUserStatus(request.requestId, { status: "submitted", error: undefined }, request);
    try {
      const result = await sendPromptResponseCommand(buildElicitationResponseCommand(request.requestId, action, undefined,
        { conversationId: request.conversationId, turnId: request.turnId, messageId: request.messageId, taskId: request.taskId }));
      if (!commandResultSucceeded(result)) throw new Error(result.message || t("回答未被后端接受"));
      useAppStore.getState().clearAskUser(request.requestId, request.conversationId,
        { turnId: request.turnId, messageId: request.messageId, taskId: request.taskId });
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      useAppStore.getState().updateAskUserStatus(request.requestId, { status: "error", error: message }, request);
    }
  };
  const openLink = async () => {
    setOpening(true);
    const accepted = await openPreferredWebLink(request.elicitation.url!, request.conversationId);
    if (accepted) setOpened(true);
    setOpening(false);
  };
  return <form className="inline-prompt-card inline-mcp-url-card" aria-busy={responding}
    onSubmit={event => { event.preventDefault(); if (!responding && !opening) void (opened ? respond("accept") : openLink()); }}
    onKeyDown={event => { if (event.key === "Escape" && !responding) { event.preventDefault(); event.stopPropagation(); void respond("cancel"); } }}>
    <div className="inline-prompt-header"><ExternalLink size={17} aria-hidden="true" />
      <div className="inline-prompt-header-copy"><div className="inline-prompt-title">{t("需要操作")}</div>
        <div className="inline-prompt-context inline-prompt-context-body">{request.question}</div>
        <div className="inline-mcp-url"><span>URL</span><span>{request.elicitation.url}</span></div>
      </div>
    </div>
    {request.error && <div role="alert" className="inline-prompt-error">{request.error}</div>}
    <div className="inline-prompt-footer inline-prompt-button-row">
      <button type="button" className="inline-request-submit" disabled={responding || opening} onClick={() => void respond("decline")}>{t("拒绝")}</button>
      <button type="submit" className="inline-request-submit" data-has-answer="true" disabled={responding || opening}>
        {responding || opening ? <LoaderCircle size={14} className="animate-spin" /> : opened ? t("继续") : t("打开链接")}
      </button>
    </div>
  </form>;
}
