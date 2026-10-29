import { useState } from "react";
import { useI18n } from "../i18n";
import { buildRequestUserInputResponseCommand } from "../protocol/prompt-responses";
import { commandResultSucceeded, sendPromptResponseCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { promptDraftKey } from "../stores/prompt-drafts";
import type { PendingAskUser, UserInputQuestionDraft } from "../stores/types";
import { RequestInputPanel } from "./RequestInputPanel";

export function RequestUserInputCard({ request }: { request: PendingAskUser & { requestUserInput: NonNullable<PendingAskUser["requestUserInput"]> } }) {
  const { t } = useI18n();
  const inputOwner = { turnId: request.turnId, messageId: request.messageId, taskId: request.taskId };
  const draftRequest = { requestId: request.requestId, conversationId: request.conversationId, inputOwner };
  const saved = useAppStore(state => state.promptDrafts[promptDraftKey(draftRequest)]?.userInputDraft);
  const [responding, setResponding] = useState(false);
  const [error, setError] = useState("");
  const questions = request.requestUserInput.questions;
  const respond = async (answers: UserInputQuestionDraft["answers"] | null) => {
    setResponding(true); setError("");
    const response = answers === null ? null : Object.fromEntries(questions.flatMap(question => {
      const answer = answers[question.id];
      const value = answer?.selectedOptionId ?? (answer?.freeformText.trim() ? answer.freeformText : undefined);
      return value ? [[question.id, { answers: [value] }]] : [];
    }));
    try {
      const result = await sendPromptResponseCommand(buildRequestUserInputResponseCommand(request.requestId, response,
        { conversationId: request.conversationId, turnId: request.turnId, messageId: request.messageId, taskId: request.taskId }));
      if (!commandResultSucceeded(result)) throw new Error(result.message || t("回答未被后端接受"));
      useAppStore.getState().clearAskUser(request.requestId, request.conversationId, inputOwner);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason)); setResponding(false);
    }
  };
  return <RequestInputPanel questions={questions.map(question => ({ id: question.id, question: question.question,
    isOther: question.isOther, isSecret: question.isSecret,
    options: (question.options ?? []).map(option => ({ ...option, id: option.label })) }))}
    initialDraft={saved} responding={responding} error={error}
    onDraftChange={draft => useAppStore.getState().updatePromptDraft(draftRequest, { userInputDraft: {
      ...draft, answers: Object.fromEntries(Object.entries(draft.answers).filter(([id]) => !questions.find(question => question.id === id)!.isSecret)),
    } })} onSubmit={answers => void respond(answers)} onCancel={() => void respond(null)} />;
}
