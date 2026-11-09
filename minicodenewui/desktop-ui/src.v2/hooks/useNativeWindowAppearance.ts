import { useEffect, useLayoutEffect } from "react";
import { desktop, isDesktop, type WindowAppearanceState } from "../desktop/runtime";
import { useAppStore } from "../stores";
import { pushToast } from "../overlays/ToastContainer";
import { useI18n } from "../i18n";

const paintNativeAppearance = (state: WindowAppearanceState) => {
  document.documentElement.dataset.nativeWindowOpaque = String(state.opaque);
};

export function useNativeWindowAppearance() {
  const { t } = useI18n();
  const themeMode = useAppStore(state => state.themeMode);
  const resolvedTheme = useAppStore(state => state.resolvedTheme);
  const opaqueWindows = useAppStore(state => state.appearanceThemes[state.resolvedTheme]?.opaqueWindows ?? false);
  useLayoutEffect(() => {
    if (!isDesktop()) return;
    return desktop()!.windowControls.onAppearanceChanged(paintNativeAppearance);
  }, []);
  useEffect(() => {
    if (!isDesktop()) return;
    let current = true;
    void desktop()!.windowControls.setAppearance({ themeMode, opaqueWindows }).then(state => {
      if (current) paintNativeAppearance(state);
    }).catch(error => {
      if (current) pushToast(t("窗口外观更新失败：{v0}", { v0: String(error) }), "error");
    });
    return () => { current = false; };
  }, [themeMode, resolvedTheme, opaqueWindows, t]);
}
