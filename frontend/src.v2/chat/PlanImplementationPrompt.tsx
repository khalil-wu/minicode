import { useState } from "react";
import { useI18n } from "../i18n";
import { proposedPlanParts } from "../lib/proposed-plan";
import { useAppStore } from "../stores";
import { promptDraftKey } from "../stores/prompt-drafts";
import type { AgentMode, ChatMessage, UserInputQuestionDraft } from "../stores/types";
import { sendChatMessage } from "./sendChatMessage";
import { RequestInputPanel } from "./RequestInputPanel";

export function pendingPlanImplementation(messages: ChatMessage[], mode: AgentMode, streaming: boolean) {
  if (mode !== "plan" || streaming) return null;
  const message = messages.filter(item => item.role === "user" || item.role === "assistant").at(-1);
  if (!message || message.role !== "assistant" || message.isStreaming || message.isThinkingStreaming
    || message.terminalStatus !== "completed") return null;
  const parts = proposedPlanParts(message.content);
  return parts?.complete ? { messageId: message.id, turnId: message.turnId, plan: parts.plan } : null;
}

export function PlanImplementationPrompt({ owner, plan }: { owner: string; plan: NonNullable<ReturnType<typeof pendingPlanImplementation>> }) {
  const { t } = useI18n();
  const request = { conversationId: owner, requestId: `implement-plan:${plan.turnId || plan.messageId}` };
  const saved = useAppStore(state => state.promptDrafts[promptDraftKey(request)]);
  const [responding, setResponding] = useState(false);
  const [error, setError] = useState("");
  if (saved?.planImplementationSettled) return null;
  const settle = () => useAppStore.getState().updatePromptDraft(request, { planImplementationSettled: true });
  const answer = async (answers: UserInputQuestionDraft["answers"]) => {
    const choice = answers.implementation;
    const implement = choice?.selectedOptionId === "implement-plan";
    const feedback = choice?.freeformText.trim();
    if (!implement && !feedback) { settle(); return; }
    const before = useAppStore.getState();
    const ownerMode = owner === before.conversationId ? before.agentMode : before.conversationWorkbenchStates[owner]?.agentMode;
    const modeCommandId = before.latestAgentModeCommandIds[owner];
    setResponding(true); setError("");
    // Codex pending-request Wr uses this exact prefix and the plan once.
    const content = implement ? `PLEASE IMPLEMENT THIS PLAN:\n${plan.plan}` : feedback!;
    const accepted = await sendChatMessage({ conversationId: owner, agentMode: implement ? "build" : "plan",
      displayContent: content, backendContent: content, quotedMessage: null, awaitAdmission: true });
    if (!accepted) { setResponding(false); setError(t("消息发送失败，请重试。")); return; }
    settle();
    const current = useAppStore.getState();
    const currentMode = owner === current.conversationId ? current.agentMode : current.conversationWorkbenchStates[owner]?.agentMode;
    if (implement && currentMode === ownerMode && current.latestAgentModeCommandIds[owner] === modeCommandId) {
      await current.setAgentMode("build", owner);
    }
    setResponding(false);
  };
  const dismiss = async () => {
    setResponding(true); setError("");
    if (await useAppStore.getState().setAgentMode("build", owner)) settle();
    setResponding(false);
  };
  return <RequestInputPanel questions={[{ id: "implementation", question: t("执行这个计划吗？"), isOther: true,
    options: [{ id: "implement-plan", label: t("是的，执行这个计划") }] }]}
    initialDraft={saved?.userInputDraft} responding={responding} error={error}
    onDraftChange={userInputDraft => useAppStore.getState().updatePromptDraft(request, { userInputDraft })}
    onSubmit={answers => void answer(answers)} onCancel={() => void dismiss()} />;
}
