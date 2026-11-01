import { useSyncExternalStore } from "react";

/** The preference is app-wide, versioned, and enabled by default. */
export const WORD_DIFFS_STORAGE_KEY = "wordDiffsEnabled.2";

const listeners = new Set<() => void>();

export function readWordDiffsEnabled(): boolean {
  const stored = localStorage.getItem(WORD_DIFFS_STORAGE_KEY);
  return stored === null ? true : JSON.parse(stored);
}

const notify = () => listeners.forEach(listener => listener());
const onStorage = (event: StorageEvent) => {
  if (event.storageArea === localStorage && (event.key === WORD_DIFFS_STORAGE_KEY || event.key === null)) notify();
};

export function setWordDiffsEnabled(enabled: boolean): void {
  localStorage.setItem(WORD_DIFFS_STORAGE_KEY, JSON.stringify(enabled));
  notify();
}

export function subscribeWordDiffsPreference(listener: () => void): () => void {
  if (listeners.size === 0) window.addEventListener("storage", onStorage);
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) window.removeEventListener("storage", onStorage);
  };
}

const serverSnapshot = () => true;

export function useWordDiffsPreference(): readonly [boolean, typeof setWordDiffsEnabled] {
  const enabled = useSyncExternalStore(subscribeWordDiffsPreference, readWordDiffsEnabled, serverSnapshot);
  return [enabled, setWordDiffsEnabled];
}
