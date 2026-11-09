import { desktop } from "../desktop/runtime";
import { useAppStore } from "../stores";
import { openSettings } from "./settings-navigation";
import { dictationPreferencesOf, migrateWorkbenchPreferences, persistedWorkbenchPreferences, useDesktopDictationPreferences, usesDesktopDictationPreferences, type DictationPreferences } from "./workbench-preferences";

let migration: Promise<void> | undefined;

export function mirrorDictationPreferences(preferences: DictationPreferences) {
  if (!usesDesktopDictationPreferences()) return;
  const next = dictationPreferencesOf(preferences);
  useAppStore.setState(state => {
    const previous = state.workbenchPreferences;
    const unchanged = (Object.keys(next) as Array<keyof DictationPreferences>).every(key => key === "dictationDictionary"
      ? next[key].length === previous[key].length && next[key].every((word, index) => word === previous[key][index])
      : next[key] === previous[key]);
    return unchanged ? state : { workbenchPreferences: { ...previous, ...next } };
  });
}

export function ensureDesktopDictationPreferences(): Promise<void> {
  const native = desktop()?.dictation;
  if (!native) return Promise.resolve();
  return migration ??= (async () => {
    const legacy = dictationPreferencesOf(migrateWorkbenchPreferences(JSON.parse(localStorage.getItem("minicode.workbench.preferences") ?? "{}")));
    const preferences = await native.migratePreferences(legacy);
    useDesktopDictationPreferences();
    mirrorDictationPreferences(preferences);
    localStorage.setItem("minicode.workbench.preferences", JSON.stringify(persistedWorkbenchPreferences(JSON.parse(localStorage.getItem("minicode.workbench.preferences") ?? "{}"))));
  })().catch(error => { migration = undefined; throw error; });
}

export async function currentDictationPreferences(): Promise<DictationPreferences> {
  const native = desktop();
  if (!native?.dictation) return dictationPreferencesOf(useAppStore.getState().workbenchPreferences);
  await ensureDesktopDictationPreferences();
  const preferences = await native.preferences.get();
  mirrorDictationPreferences(preferences);
  return dictationPreferencesOf(preferences);
}

export async function saveDictationPreferences(patch: Partial<DictationPreferences>): Promise<void> {
  const native = desktop();
  if (!native?.dictation) { useAppStore.getState().setWorkbenchPreferences(patch); return; }
  await ensureDesktopDictationPreferences();
  mirrorDictationPreferences(await native.preferences.set(patch));
}

export async function openDictationProviderSettings(): Promise<void> {
  const native = desktop()?.dictation;
  if (native) {
    await native.openProviderSettings();
  } else openSettings("provider");
}

export async function consumeDictationProviderSettings(): Promise<void> {
  const native = desktop()?.dictation;
  if (native && await native.consumeProviderSettings()) openSettings("provider");
}
