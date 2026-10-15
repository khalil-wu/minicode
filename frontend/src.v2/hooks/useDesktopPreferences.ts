import { useEffect, useState } from "react";
import { getDesktopPreferences, isDesktop, onDesktopPreferencesChanged, setDesktopPreferences, type DesktopPreferences } from "../desktop/runtime";
import { pushToast } from "../overlays/ToastContainer";

export function useDesktopPreferences() {
  const [preferences, setPreferences] = useState<DesktopPreferences | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!isDesktop()) return;
    let mounted = true;
    const unsubscribe = onDesktopPreferencesChanged((value) => { if (mounted) setPreferences(value); });
    void getDesktopPreferences().then((value) => { if (mounted) setPreferences(value); }).catch((reason) => { if (mounted) setError(String(reason)); });
    return () => { mounted = false; unsubscribe(); };
  }, []);
  const update = async (patch: Partial<Omit<DesktopPreferences, "fileOpenTargets" | "fileOpenTargetPerPath" | "terminalShellTargets" | "systemLocale">>) => {
    try { const value = await setDesktopPreferences(patch); setPreferences(value); }
    catch (reason) { pushToast(reason instanceof Error ? reason.message : String(reason), "error"); }
  };
  return { preferences, error, update };
}
