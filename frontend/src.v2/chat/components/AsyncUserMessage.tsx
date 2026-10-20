import { useState } from "react";
import type { TextContentBlock } from "../../stores/types";
import { useAppStore } from "../../stores";
import { sendChatMessage } from "../sendChatMessage";
import { useI18n } from "../../i18n";
import "./AsyncUserMessage.css";

export function AsyncUserMessage({ message, conversationId, turnId, readOnly }: { message: TextContentBlock; conversationId: string; turnId?: string; readOnly: boolean }) {
  const { t } = useI18n();
  const questions = message.questions ?? [];
  const [values, setValues] = useState<string[]>(() => questions.map(question => question.options?.[0] ?? ""));
  const [custom, setCustom] = useState<boolean[]>(() => questions.map(question => !question.options));
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const answered = useAppStore(state => state.getVisibleMessages(conversationId).some(item => item.role === "user" && item.asyncRequestId === message.itemId));
  const submit = async () => {
    setSending(true);
    try {
      const content = questions.map((question, index) => `${question.title}\n${values[index]}`).join("\n\n");
      const accepted = await sendChatMessage({ conversationId, displayContent: content, backendContent: content,
        asyncRequestId: message.itemId, allowWhileStreaming: true, busyBehavior: "steer", expectedTurnId: turnId ?? null, awaitAdmission: true });
      if (accepted) setSent(true);
    } finally { setSending(false); }
  };
  if (!questions.length) return <div className="async-user-message">{message.content}</div>;
  return <form className="async-user-message" onSubmit={event => { event.preventDefault(); void submit(); }}>
    {questions.map((question, index) => <fieldset key={index} disabled={readOnly || answered || sent || sending}>
      <legend>{question.title}</legend>
      {question.options?.map((option, optionIndex) => <label key={optionIndex} className="async-user-option">
        <input type="radio" name={`${message.itemId}-${index}`} checked={!custom[index] && values[index] === option}
          onChange={() => { setCustom(current => current.map((value, at) => at === index ? false : value)); setValues(current => current.map((value, at) => at === index ? option : value)); }} />
        {option}
      </label>)}
      {question.options && <label className="async-user-option"><input type="radio" name={`${message.itemId}-${index}`} checked={custom[index]}
        onChange={() => { setCustom(current => current.map((value, at) => at === index ? true : value)); setValues(current => current.map((value, at) => at === index ? "" : value)); }} />{t("其他")}</label>}
      {custom[index] && <textarea aria-label={question.title} value={values[index]} rows={2}
        onChange={event => setValues(current => current.map((value, at) => at === index ? event.target.value : value))} />}
    </fieldset>)}
    {!readOnly && <button type="submit" disabled={answered || sent || sending || values.some(value => !value.trim())}>{answered || sent ? t("已回答") : sending ? t("发送中…") : t("发送回答")}</button>}
  </form>;
}
