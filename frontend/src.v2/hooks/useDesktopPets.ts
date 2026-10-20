import { useEffect } from "react";
import { desktop, isDesktop } from "../desktop/runtime";
import { useAppStore } from "../stores";
import { pushToast } from "../overlays/ToastContainer";
import { useI18n } from "../i18n";
import { desktopPetActivity, petTaskActions, submitPetFollowup } from "../pets/petActivities";

export function useDesktopPets() {
  const { t } = useI18n();
  const reducedMotion = useAppStore(state => state.reducedMotion);
  useEffect(() => {
    if (!isDesktop()) return;
    const pets = desktop()!.pets;
    let last = "";
    const publish = () => {
      const activity = desktopPetActivity(useAppStore.getState(), t);
      const signature = JSON.stringify(activity);
      if (signature === last) return;
      last = signature;
      void pets.publishActivity(activity).catch(error => pushToast(t("无法更新宠物状态：{v0}", { v0: String(error) }), "error"));
    };
    const removeActions = pets.onActionRequested(async request => {
      let error: string | undefined;
      try {
        if (request.kind === "followup") await submitPetFollowup(request.taskId, request.prompt, t);
        else {
          const action = petTaskActions(useAppStore.getState(), request.taskId, t).find(action => action.id === request.actionId);
          if (!action) throw new Error("The original task request is no longer pending.");
          await action.submit();
        }
      } catch (reason) { error = String(reason); }
      await pets.completeAction({ id: request.id, ...(error ? { error } : {}) });
    });
    publish();
    const unsubscribe = useAppStore.subscribe(publish);
    const refresh = () => { last = ""; publish(); };
    window.addEventListener("pets:refreshed", refresh);
    window.addEventListener("pets:activity-changed", publish);
    return () => { removeActions(); window.removeEventListener("pets:refreshed", refresh); window.removeEventListener("pets:activity-changed", publish); unsubscribe(); };
  }, [t]);
  useEffect(() => {
    if (!isDesktop()) return;
    void desktop()!.pets.update({ reducedMotion }).catch(error => pushToast(t("无法更新宠物状态：{v0}", { v0: String(error) }), "error"));
  }, [reducedMotion, t]);
}
