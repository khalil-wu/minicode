import { useEffect, useState } from "react";
import { desktop, type DesktopPreferenceTargets } from "../desktop/runtime";

/** Installed applications are a fresh discovery, separate from live preference values. */
export function useDesktopPreferenceTargets(active = true, terminalShell?: string) {
  const api = desktop();
  const [targets, setTargets] = useState<DesktopPreferenceTargets | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!api || !active) return;
    let current = true;
    let requested = 0;
    const read = () => {
      const revision = ++requested;
      void api.preferences.getTargets().then(value => {
        if (current && revision === requested) { setTargets(value); setError(""); }
      }, reason => { if (current && revision === requested) setError(String(reason)); });
    };
    read();
    window.addEventListener("focus", read);
    return () => { current = false; window.removeEventListener("focus", read); };
  }, [api, active, terminalShell]);
  return { targets, error };
}
