import { useEffect, useState } from "react";
import { useAppStore } from "../stores";
import { effectiveAppearanceColors } from "./appearance-colors";
import { createDiffPalette, loadDiffCodeColors } from "./diff-palette";
import type { CodeThemeId } from "./code-themes";
import type { AppearanceVariant } from "./appearance-colors";
import { useI18n } from "../i18n";
import { pushToast } from "../overlays/ToastContainer";

export function useDiffPalette() {
  const { t } = useI18n();
  const variant = useAppStore(state => state.resolvedTheme);
  const colors = useAppStore(state => state.appearanceColors[variant]);
  const selection = useAppStore(state => state.appearanceThemes[variant]);
  const id = selection?.codeThemeId;
  const [loaded, setLoaded] = useState<{ id: CodeThemeId; variant: AppearanceVariant; colors: { fg: string; bg: string } }>();
  useEffect(() => {
    if (!id) return;
    let current = true;
    void loadDiffCodeColors(id, variant).then(colors => {
      if (current) setLoaded({ id, variant, colors });
    }).catch(error => {
      if (!current) return;
      console.error("Diff palette load failed", error);
      pushToast(t("无法应用{variant}代码主题", { variant: t(variant === "light" ? "浅色" : "深色") }), "error");
    });
    return () => { current = false; };
  }, [id, variant, t]);
  const palette = selection && loaded && loaded.id === selection.codeThemeId && loaded.variant === variant
    ? createDiffPalette(variant, loaded.colors, colors ?? effectiveAppearanceColors(), selection.semanticColors)
    : null;
  return { palette, variant };
}
