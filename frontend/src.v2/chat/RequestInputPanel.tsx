import { X } from "../lib/icons";
import { PromptSourceIcon } from "../lib/prompt-source-icons";
import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import type { UserInputQuestionDraft } from "../stores/types";

export interface RequestInputPanelQuestion {
  id: string;
  question: string;
  isOther?: boolean;
  isSecret?: boolean;
  options: Array<{ id: string; label: string; description?: string }>;
}

/** Request-input surface: one question at a time, inside the composer. */
export function RequestInputPanel({ questions, initialDraft, responding, error, onDraftChange, onSubmit, onCancel }: {
  questions: RequestInputPanelQuestion[];
  initialDraft?: UserInputQuestionDraft;
  responding: boolean;
  error: string;
  onDraftChange: (draft: UserInputQuestionDraft) => void;
  onSubmit: (answers: UserInputQuestionDraft["answers"]) => void;
  onCancel: () => void;
}) {
  const { t } = useI18n();
  const [draft, setDraft] = useState<UserInputQuestionDraft>(() => initialDraft ?? {
    questionIndex: 0,
    answers: Object.fromEntries(questions.map(question => [question.id, {
      selectedOptionId: question.options[0]?.id ?? null, freeformText: "",
    }])),
  });
  const [advancingChoice, setAdvancingChoice] = useState<string | null>(null);
  const [hoveredChoice, setHoveredChoice] = useState<string | null>();
  const timer = useRef<number | null>(null);
  const root = useRef<HTMLFormElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const question = questions[draft.questionIndex];
  const answer = draft.answers[question.id] ?? { selectedOptionId: question.options[0]?.id ?? null, freeformText: "" };
  const last = draft.questionIndex === questions.length - 1;
  const busy = responding || advancingChoice !== null;
  const update = (next: UserInputQuestionDraft) => { setDraft(next); onDraftChange(next); };
  const stopAdvance = () => { if (timer.current !== null) window.clearTimeout(timer.current); timer.current = null; setAdvancingChoice(null); };
  useEffect(() => {
    (question.options.length ? root.current : input.current)?.focus({ preventScroll: true });
    return () => { if (timer.current !== null) window.clearTimeout(timer.current); };
  }, []);
  const setAnswer = (next: typeof answer) => {
    setHoveredChoice(next.selectedOptionId);
    const answers = { ...draft.answers, [question.id]: next };
    update({ ...draft, answers });
    return answers;
  };
  const goTo = (questionIndex: number) => {
    stopAdvance(); setHoveredChoice(undefined); update({ ...draft, questionIndex }); root.current?.focus({ preventScroll: true });
  };
  const advance = (answers: UserInputQuestionDraft["answers"]) => {
    if (last) onSubmit(answers);
    else { setHoveredChoice(undefined); update({ questionIndex: draft.questionIndex + 1, answers }); root.current?.focus({ preventScroll: true }); }
  };
  const selectOption = (id: string) => {
    const answers = setAnswer({ selectedOptionId: id, freeformText: "" });
    setAdvancingChoice(id);
    // Keep the clicked option visible for 180 ms before advancing.
    timer.current = window.setTimeout(() => { timer.current = null; setAdvancingChoice(null); advance(answers); }, 180);
  };
  const submitCurrent = () => {
    if (answer.freeformText.trim()) advance(draft.answers);
    else if (answer.selectedOptionId !== null) selectOption(answer.selectedOptionId);
    else advance(draft.answers);
  };
  const skipOrSubmitCustom = () => {
    advance(answer.freeformText.trim() ? draft.answers : setAnswer({ selectedOptionId: null, freeformText: "" }));
  };
  return <form ref={root} tabIndex={0} className="inline-request-input" aria-busy={responding}
    data-request-input-navigation="true" onSubmit={event => { event.preventDefault(); if (!busy) submitCurrent(); }}
    onKeyDown={event => {
      if (event.nativeEvent.isComposing || event.defaultPrevented) return;
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); stopAdvance(); if (!responding) onCancel(); return; }
      if (busy || event.target instanceof HTMLElement && event.target.closest("textarea, input")) return;
      if (event.key >= "1" && event.key <= "9" && !event.altKey && !event.ctrlKey && !event.metaKey) {
        const index = Number(event.key) - 1, option = question.options[index];
        if (option) { event.preventDefault(); selectOption(option.id); }
        else if (question.isOther && index === question.options.length) { event.preventDefault(); input.current?.focus({ preventScroll: true }); }
        return;
      }
      if (event.key === "ArrowLeft" && draft.questionIndex > 0 || event.key === "ArrowRight" && !last) {
        event.preventDefault(); goTo(draft.questionIndex + (event.key === "ArrowRight" ? 1 : -1));
      } else if ((event.key === "ArrowUp" || event.key === "ArrowDown") && question.options.length) {
        event.preventDefault();
        const current = question.options.findIndex(option => option.id === answer.selectedOptionId);
        if (event.key === "ArrowDown" && input.current && (current === question.options.length - 1 || current === -1)) {
          input.current.focus({ preventScroll: true }); return;
        }
        const next = current < 0 ? event.key === "ArrowDown" ? 0 : question.options.length - 1
          : Math.max(0, Math.min(question.options.length - 1, current + (event.key === "ArrowDown" ? 1 : -1)));
        setAnswer({ selectedOptionId: question.options[next].id, freeformText: "" }); optionRefs.current[next]?.focus();
      } else if (event.key === "Enter" && event.target === root.current) { event.preventDefault(); submitCurrent(); }
    }}>
    <div className="inline-request-heading">
      <div className="inline-request-question" key={question.id}>{question.question}</div>
      <div className="inline-request-navigation">
        {questions.length > 1 && <>
          <button type="button" title={t("上一题")} aria-label={t("上一题")} disabled={busy || draft.questionIndex === 0} onClick={() => goTo(draft.questionIndex - 1)}><PromptSourceIcon icon="chevron-down" size={14} className="inline-request-previous-glyph" /></button>
          <span>{t("第 {v0} 题，共 {v1} 题", { v0: draft.questionIndex + 1, v1: questions.length })}</span>
          <button type="button" title={t("下一题")} aria-label={t("下一题")} disabled={busy || last} onClick={() => goTo(draft.questionIndex + 1)}><PromptSourceIcon icon="chevron-down" size={14} className="inline-request-next-glyph" /></button>
        </>}
        <button type="button" data-request-input-dismiss title={t("取消")} aria-label={t("取消")} disabled={responding} onClick={() => { stopAdvance(); onCancel(); }}><X size={14} /></button>
      </div>
    </div>
    <div className="inline-request-body" key={question.id}>
      <div className="inline-request-options" role="radiogroup" aria-label={question.question}>
        {question.options.map((option, index) => <button ref={element => { optionRefs.current[index] = element; }} key={option.id}
          type="button" role="radio" aria-label={option.label} aria-description={option.description}
          aria-checked={answer.selectedOptionId === option.id} disabled={busy}
          className="inline-request-option" data-advancing={advancingChoice === option.id || undefined}
          data-highlighted={(advancingChoice !== null ? advancingChoice === option.id
            : hoveredChoice !== undefined ? hoveredChoice === option.id : answer.selectedOptionId === option.id) || undefined}
          onPointerEnter={() => setHoveredChoice(option.id)} onClick={() => selectOption(option.id)}>
          <span className="inline-request-option-number">{index + 1}</span>
          <span className="inline-request-option-copy"><span className="inline-request-option-label">
            <span>{option.label.endsWith(" (Recommended)") ? option.label.slice(0, -14) : option.label}</span>
            {option.label.endsWith(" (Recommended)") && <span className="inline-request-recommended">{t("推荐")}</span>}
          </span>{option.description && <small>{option.description}</small>}</span>
          <span className="inline-request-option-enter" aria-hidden="true"><PromptSourceIcon icon="arrow-left" /></span>
        </button>)}
      </div>
      <div className="inline-request-footer" onPointerEnter={() => setHoveredChoice(null)}>
        {(question.isOther || !question.options.length) && <textarea ref={input} rows={1} disabled={busy}
          aria-label={t("回答当前问题")} value={answer.freeformText} autoComplete="off"
          data-secret={question.isSecret || undefined} className="inline-request-other"
          placeholder={question.options.length ? t("不，请告诉 MiniCode 应该如何调整") : t("在这里输入")}
          onFocus={() => { if (answer.selectedOptionId !== null) setAnswer({ ...answer, selectedOptionId: null }); }}
          onChange={event => setAnswer({ selectedOptionId: null, freeformText: event.target.value })}
          onKeyDown={event => {
            if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); if (!event.nativeEvent.isComposing && !busy) advance(draft.answers); }
          }} />}
        <button type="button" className="inline-request-submit" data-has-answer={Boolean(answer.freeformText.trim())}
          disabled={busy} onClick={skipOrSubmitCustom}>{answer.freeformText.trim() ? last ? t("提交") : t("下一步") : t("跳过")}</button>
      </div>
      {error && <div role="alert" className="inline-prompt-error">{error}</div>}
    </div>
  </form>;
}
