import { useEffect, useState } from "react";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";

type PluginsFeaturePayload = {
  flags: Array<{ name: string; source: string }>;
  effective: { plugins: { enabled: boolean } };
};
type PluginsFeature = { enabled: boolean | undefined; locked: boolean; error: string };
const CHANGED = "minicode:plugins-feature-changed";

const fromPayload = (payload: PluginsFeaturePayload): PluginsFeature => ({
  enabled: payload.effective.plugins.enabled,
  locked: ["managed", "env"].includes(payload.flags.find(flag => flag.name === "plugins")!.source),
  error: "",
});

export const usePluginsFeature = (): PluginsFeature => {
  const [state, setState] = useState<PluginsFeature>({ enabled: undefined, locked: false, error: "" });
  useEffect(() => {
    let active = true;
    let revision = 0;
    const changed = (event: Event) => { revision++; setState((event as CustomEvent<PluginsFeature>).detail); };
    const read = async () => {
      const requested = ++revision;
      try {
        const response = await fetchWithTimeout(`${apiBase()}/api/settings/feature-flags`, { cache: "no-store", headers: authHeaders() });
        if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
        const payload = await response.json() as PluginsFeaturePayload;
        if (active && requested === revision) setState(fromPayload(payload));
      } catch (reason) {
        if (active && requested === revision) setState(previous => ({ ...previous, error: reason instanceof Error ? reason.message : String(reason) }));
      }
    };
    const focus = () => void read();
    window.addEventListener(CHANGED, changed);
    window.addEventListener("focus", focus);
    void read();
    return () => { active = false; window.removeEventListener(CHANGED, changed); window.removeEventListener("focus", focus); };
  }, []);
  return state;
};

export const setPluginsFeatureEnabled = async (enabled: boolean): Promise<void> => {
  const response = await fetchWithTimeout(`${apiBase()}/api/settings/feature-flags`, {
    method: "PUT", headers: authHeaders({ "content-type": "application/json" }), body: JSON.stringify({ flags: { plugins: enabled } }),
  });
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  const payload = await response.json() as PluginsFeaturePayload;
  window.dispatchEvent(new CustomEvent(CHANGED, { detail: fromPayload(payload) }));
};
