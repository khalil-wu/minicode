import { useSyncExternalStore } from "react";
import { desktop, type DesktopPreferences, type DesktopPreferencesPatch, type MiniCodeDesktop } from "../desktop/runtime";
import { pushToast } from "../overlays/ToastContainer";

type Snapshot = { preferences: DesktopPreferences | null; error: string };

function createPreferencesOwner(api: MiniCodeDesktop) {
  let snapshot: Snapshot = { preferences: null, error: "" };
  let revision = 0;
  let stop: (() => void) | undefined;
  let reading: Promise<DesktopPreferences> | undefined;
  const listeners = new Set<() => void>();
  const publish = (next: Snapshot) => { snapshot = next; for (const listener of listeners) listener(); };
  const receive = (preferences: DesktopPreferences) => { revision++; publish({ ...snapshot, preferences, error: "" }); };
  return {
    getSnapshot: () => snapshot,
    subscribe(listener: () => void) {
      listeners.add(listener);
      if (listeners.size === 1) {
        stop = api.preferences.onChanged(receive);
        const requested = revision;
        reading ??= api.preferences.get().finally(() => { reading = undefined; });
        void reading.then(preferences => {
          if (revision === requested) receive(preferences);
        }, reason => {
          if (revision === requested) publish({ ...snapshot, error: String(reason) });
        });
      }
      return () => { listeners.delete(listener); if (!listeners.size) { revision++; stop!(); stop = undefined; } };
    },
    async update(patch: DesktopPreferencesPatch) {
      const requested = revision;
      try {
        const preferences = await api.preferences.set(patch);
        if (revision === requested) receive(preferences);
      } catch (reason) { pushToast(reason instanceof Error ? reason.message : String(reason), "error"); }
    },
  };
}

const owners = new WeakMap<MiniCodeDesktop, ReturnType<typeof createPreferencesOwner>>();
const webSnapshot: Snapshot = { preferences: null, error: "" };
const webOwner = { getSnapshot: () => webSnapshot, subscribe: () => () => {}, update: async (_patch: DesktopPreferencesPatch) => {} };

export function useDesktopPreferences() {
  const api = desktop();
  let owner: ReturnType<typeof createPreferencesOwner> = webOwner;
  if (api) {
    if (!owners.has(api)) owners.set(api, createPreferencesOwner(api));
    owner = owners.get(api)!;
  }
  return { ...useSyncExternalStore(owner.subscribe, owner.getSnapshot), update: owner.update };
}
