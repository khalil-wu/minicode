import { useEffect, useRef } from "react";
import type { GuardianAssessment } from "../protocol/guardian-types";
import { useAppStore } from "../stores";
import { useI18n } from "../i18n";
import { guardianActionSummary } from "../chat/cells/GuardianReviewCell";
import { approveGuardianDeniedAction, recentGuardianDenials, useGuardianApprovalState } from "../chat/guardianApprovalState";
import { pushToast } from "../overlays/ToastContainer";
import "./GuardianApprovalMenu.css";

/** Slash submenu for approving a denied action; the retry stays subject to auto-review. */
export function GuardianApprovalMenu({ conversationId, onClose, onApproved }: { conversationId: string; onClose: () => void; onApproved: () => void }) {
  const { t } = useI18n();
  const ref = useRef<HTMLDivElement>(null);
  const state = useGuardianApprovalState(conversationId);
  const messages = useAppStore(store => store.messages);
  const reviews = recentGuardianDenials(conversationId, state.approved);
  useEffect(() => {
    const pointer = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) onClose(); };
    const key = (event: KeyboardEvent) => {
      if (event.isComposing || event.keyCode === 229) return;
      const owner = ref.current?.closest(".composer-container");
      if (owner && !owner.contains(event.target as Node)) return;
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); return; }
      if (!["ArrowDown", "ArrowUp", "Home", "End", "Enter"].includes(event.key)) return;
      const buttons = [...(ref.current?.querySelectorAll<HTMLButtonElement>('button[role="menuitem"]:not(:disabled)') ?? [])];
      if (!buttons.length) return;
      event.preventDefault(); event.stopPropagation();
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
      if (event.key === "Enter") { buttons[Math.max(0, index)].click(); return; }
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
        : event.key === "ArrowUp" ? (index + buttons.length - 1) % buttons.length : (index + 1) % buttons.length;
      buttons[next].focus();
    };
    document.addEventListener("pointerdown", pointer); document.addEventListener("keydown", key, true);
    return () => { document.removeEventListener("pointerdown", pointer); document.removeEventListener("keydown", key, true); };
  }, [onClose, messages]);
  const approve = async (review: GuardianAssessment) => {
    try {
      if (!await approveGuardianDeniedAction(conversationId, review) || useAppStore.getState().conversationId !== conversationId) return;
      onApproved(); pushToast(t("已记录批准"), "success");
    } catch {
      if (useAppStore.getState().conversationId === conversationId) pushToast(t("无法记录自动审核批准"), "error");
    }
  };
  return <div ref={ref} className="guardian-approval-menu" role="menu" aria-label={t("批准")}>
    <p>{t("选择一条自动审查驳回记录，以批准一次重试。此次重试仍会经过自动审查。")}</p>
    {reviews.map(review => <button key={review.id} type="button" role="menuitem" disabled={state.pending !== null} onClick={() => void approve(review)}>
      <span>{guardianActionSummary(review, t)}</span><span>{state.pending === review.id ? t("正在记录批准操作…") : review.rationale ?? t("自动审查未提供理由")}</span>
    </button>)}
  </div>;
}
