export type Locale = "zh-CN" | "en-US";
export type LocaleOverride = Locale | null;

/** A null override means automatic detection; unsupported UI locales use English. */
export function resolveLocale(override: LocaleOverride, detectedLocale: string): Locale {
  if (override !== null) return override;
  const detected = detectedLocale.replaceAll("_", "-").toLowerCase();
  if (detected === "zh" || detected === "zh-cn" || detected === "zh-sg" || detected.startsWith("zh-hans")) return "zh-CN";
  return "en-US";
}
