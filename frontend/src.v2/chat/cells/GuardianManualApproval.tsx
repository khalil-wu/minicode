import type { GuardianAssessment } from "../../protocol/guardian-types";
import { useI18n } from "../../i18n";
import { approveGuardianDeniedAction, useGuardianApprovalState } from "../guardianApprovalState";
import { pushToast } from "../../overlays/ToastContainer";

/** Installed Codex tC: authorizes one retry; the button itself never executes. */
export function GuardianManualApproval({ conversationId, review }: { conversationId: string; review: GuardianAssessment }) {
  const { t } = useI18n();
  const state = useGuardianApprovalState(conversationId);
  if (state.approved.has(review.id)) return null;
  const approve = async () => {
    try {
      if (await approveGuardianDeniedAction(conversationId, review)) pushToast(t("已记录批准"), "success");
    } catch {
      pushToast(t("无法记录自动审核批准"), "error");
    }
  };
  return <div className="guardian-manual-approval"><p>{t("批准后允许的操作")}</p>
    <div><p>{t("记录对此操作的批准，并允许重试一次，但不会运行该操作。")}</p>
      <button type="button" disabled={state.pending !== null} onClick={event => { event.stopPropagation(); void approve(); }}>
        {t(state.pending === review.id ? "正在批准…" : "批准")}
      </button></div>
  </div>;
}
