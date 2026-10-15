import { createContext, useContext, useLayoutEffect, useMemo, useSyncExternalStore, type ReactNode } from "react";
import { useDesktopPreferences } from "../hooks/useDesktopPreferences";
import coreEn from "./en.json";
import coreZh from "./zh-CN.json";
import desktopEn from "./desktop.en.json";
import desktopZh from "./desktop.zh-CN.json";
import processEn from "./process.en.json";
import processZh from "./process.zh-CN.json";
import guardianEn from "./guardian.en.json";
import guardianZh from "./guardian.zh-CN.json";
import rootEn from "./root.en.json";
import rootZh from "./root.zh-CN.json";
import { resolveLocale, type Locale } from "./locale";

const en = { ...coreEn, ...desktopEn, ...processEn, ...rootEn, ...guardianEn };
const zh = { ...coreZh, ...desktopZh, ...processZh, ...rootZh, ...guardianZh };
export type TranslationKey = keyof typeof en;
export type TranslationValues = Record<string, string | number>;
export type Translate = (key: TranslationKey, values?: TranslationValues) => string;
const messages: Record<Locale, Record<TranslationKey, string>> = { "en-US": en, "zh-CN": zh };

export function translate(locale: Locale, key: TranslationKey, values: TranslationValues = {}): string {
  return messages[locale][key].replace(/\{(\w+)\}/g, (_, name: string) => String(values[name]));
}

function localeValue(locale: Locale) {
  return { locale, t: ((key, values) => translate(locale, key, values)) as Translate };
}

// Standalone components retain the project's authored Chinese copy. The app always
// provides the user's explicit preference or the detected desktop/browser locale.
const LanguageContext = createContext(localeValue("zh-CN"));
let applicationLocale: Locale = "zh-CN";
const languageListeners = new Set<() => void>();
const subscribeLanguage = (listener: () => void) => { languageListeners.add(listener); return () => { languageListeners.delete(listener); }; };

export function LanguageProvider({ locale, children }: { locale: Locale; children: ReactNode }) {
  const value = useMemo(() => localeValue(locale), [locale]);
  useLayoutEffect(() => {
    document.documentElement.lang = locale;
    applicationLocale = locale;
    languageListeners.forEach((listener) => listener());
  }, [locale]);
  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
}

/** Imperative dialogs own separate React roots and share the app's selected locale. */
export function AppLanguageBridge({ children }: { children: ReactNode }) {
  const locale = useSyncExternalStore(subscribeLanguage, () => applicationLocale);
  const value = useMemo(() => localeValue(locale), [locale]);
  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
}

export function DesktopLanguageProvider({ children }: { children: ReactNode }) {
  const { preferences } = useDesktopPreferences();
  const locale = resolveLocale(preferences?.localeOverride ?? null, preferences?.systemLocale ?? navigator.language);
  return <LanguageProvider locale={locale}>{children}</LanguageProvider>;
}

export const useI18n = () => useContext(LanguageContext);
export { resolveLocale, type Locale, type LocaleOverride } from "./locale";
