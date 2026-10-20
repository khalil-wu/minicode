import { useEffect, useState } from "react";
import { getDesktopPreferences, isDesktop, onDesktopPreferencesChanged, setDesktopPreferences, type DesktopPreferences, type DesktopPreferencesPatch } from "../desktop/runtime";
import { pushToast } from "../overlays/ToastContainer";

export function useDesktopPreferences() {
  const [preferences, setPreferences] = useState<DesktopPreferences | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!isDesktop()) return;
    let mounted = true;
    let receivedState = false;
    const unsubscribe = onDesktopPreferencesChanged((value) => {
      receivedState = true;
      if (mounted) { setPreferences(value); setError(""); }
    });
    void getDesktopPreferences().then((value) => {
      if (mounted && !receivedState) { setPreferences(value); setError(""); }
    }).catch((reason) => { if (mounted && !receivedState) setError(String(reason)); });
    return () => { mounted = false; unsubscribe(); };
  }, []);
  const update = async (patch: DesktopPreferencesPatch) => {
    try { const value = await setDesktopPreferences(patch); setPreferences(value); }
    catch (reason) { pushToast(reason instanceof Error ? reason.message : String(reason), "error"); }
  };
  return { preferences, error, update };
}
