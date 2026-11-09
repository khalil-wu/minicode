import { useEffect } from "react";
import { useDesktopPreferences } from "./useDesktopPreferences";
import { useAppStore } from "../stores";
import { isDesktop } from "../desktop/runtime";
import { mirrorDictationPreferences } from "../lib/dictation-preferences";

export function useDesktopBehaviorPreferences() {
  const { preferences } = useDesktopPreferences();
  useEffect(() => {
    if (preferences) {
      const state = useAppStore.getState();
      if (state.defaultTerminalLocation !== preferences.defaultTerminalLocation
        || state.composerPlainTextMode !== preferences.composerPlainTextMode
        || state.primaryNumberShortcutTarget !== preferences.primaryNumberShortcutTarget) {
        useAppStore.setState({ defaultTerminalLocation: preferences.defaultTerminalLocation,
          composerPlainTextMode: preferences.composerPlainTextMode,
          primaryNumberShortcutTarget: preferences.primaryNumberShortcutTarget });
      }
    }
    if (preferences) mirrorDictationPreferences(preferences);
  }, [preferences]);
  useEffect(() => {
    if (isDesktop()) return;
    const changed = (event: StorageEvent) => {
      if (event.key === "minicode.default-terminal-location" && (event.newValue === "bottom" || event.newValue === "right"))
        useAppStore.setState({ defaultTerminalLocation: event.newValue });
      if (event.key === "minicode.composer-plain-text" && (event.newValue === "true" || event.newValue === "false"))
        useAppStore.setState({ composerPlainTextMode: event.newValue === "true" });
    };
    window.addEventListener("storage", changed);
    return () => window.removeEventListener("storage", changed);
  }, []);
}
