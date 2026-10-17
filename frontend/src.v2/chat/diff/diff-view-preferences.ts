import { useSyncExternalStore } from "react";

// Source's app-wide review preferences; the file viewer has separate defaults.
const preferences = {
  loadFullFiles: { key: "load-full-files", initial: true },
  richPreview: { key: "diffRichPreview", initial: false },
  hideWhitespace: { key: "hideDiffWhitespace", initial: false },
  hideImports: { key: "hideDiffImports", initial: false },
  wrap: { key: "wrapCodeDiff.2", initial: false },
  filesVisible: { key: "app-shell-file-tree-open", initial: false },
} as const;

export type DiffViewPreference = keyof typeof preferences;
const listeners = new Set<() => void>();
const notify = () => listeners.forEach(listener => listener());
const onStorage = (event: StorageEvent) => {
  if (event.storageArea === localStorage && (event.key === null
    || Object.values(preferences).some(preference => preference.key === event.key))) notify();
};

export function readDiffViewPreference(name: DiffViewPreference): boolean {
  const preference = preferences[name];
  const stored = localStorage.getItem(preference.key);
  return stored === null ? preference.initial : JSON.parse(stored);
}

export function setDiffViewPreference(name: DiffViewPreference, enabled: boolean): void {
  localStorage.setItem(preferences[name].key, JSON.stringify(enabled));
  notify();
}

function subscribe(listener: () => void): () => void {
  if (listeners.size === 0) window.addEventListener("storage", onStorage);
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) window.removeEventListener("storage", onStorage);
  };
}

export function useDiffViewPreference(name: DiffViewPreference): readonly [boolean, (enabled: boolean) => void] {
  const enabled = useSyncExternalStore(subscribe, () => readDiffViewPreference(name), () => preferences[name].initial);
  return [enabled, value => setDiffViewPreference(name, value)];
}
