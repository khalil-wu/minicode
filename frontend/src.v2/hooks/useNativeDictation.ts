import { useEffect } from "react";
import { desktop } from "../desktop/runtime";
import { composerDictationTarget, dictationController } from "../composer/dictation-controller";
import { pushToast } from "../overlays/ToastContainer";
import { ensureDesktopDictationPreferences } from "../lib/dictation-preferences";

export function useNativeDictation() {
  useEffect(() => {
    const native = desktop()?.dictation;
    if (!native) return;
    let token: string | undefined;
    const report = (error: unknown) => pushToast(error instanceof Error ? error.message : String(error), "error");
    const complete = () => {
      if (!token) return;
      const completed = token; token = undefined;
      void native.complete(completed).catch(report);
    };
    const unsubscribe = dictationController.subscribe(() => { if (dictationController.getSnapshot().phase === "idle") complete(); });
    const stop = native.onEvent(event => {
      if (event.action === "start") {
        if (dictationController.getSnapshot().phase !== "idle") { void native.complete(event.token).catch(report); return; }
        token = event.token;
        const element = document.activeElement;
        const inComposer = event.targetKind === "composer" && element?.closest(".composer-container");
        const appEditable = element?.matches("input, textarea, [contenteditable='true'], [role='textbox']") || element?.closest("[contenteditable='true']");
        const target = inComposer ? composerDictationTarget() : { kind: "external" as const, token: event.token, insert: (text: string) => {
          if (event.targetKind === "composer" && (!appEditable || !element?.isConnected || document.activeElement !== element)) return Promise.resolve(false);
          return native.insert(event.token, text);
        } };
        void dictationController.start(target).then(() => { if (dictationController.getSnapshot().phase === "idle") complete(); });
      } else if (token === event.token) dictationController.stop(event.action === "cancel" ? "cancelled" : "saved");
    });
    void native.get().catch(report);
    void ensureDesktopDictationPreferences().catch(report);
    return () => { stop(); unsubscribe(); dictationController.stop("interrupted"); complete(); };
  }, []);
}
