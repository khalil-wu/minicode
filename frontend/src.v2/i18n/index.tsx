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
import connectionsEn from "./connections.en.json";
import connectionsZh from "./connections.zh-CN.json";
import appearanceEn from "./appearance.en.json";
import appearanceZh from "./appearance.zh-CN.json";
import gitEn from "./git.en.json";
import gitZh from "./git.zh-CN.json";
import navigationEn from "./navigation.en.json";
import navigationZh from "./navigation.zh-CN.json";
import projectsEn from "./projects.en.json";
import projectsZh from "./projects.zh-CN.json";
import goalsEn from "./goals.en.json";
import goalsZh from "./goals.zh-CN.json";
import localEnvironmentsEn from "./local-environments.en.json";
import localEnvironmentsZh from "./local-environments.zh-CN.json";
import petsEn from "./pets.en.json";
import petsZh from "./pets.zh-CN.json";
import pluginMarketplacesEn from "./plugin-marketplaces.en.json";
import pluginMarketplacesZh from "./plugin-marketplaces.zh-CN.json";
import generalExtraEn from "./general-extra.en.json";
import generalExtraZh from "./general-extra.zh-CN.json";
import appearanceSourceEn from "./appearance-source.en.json";
import appearanceSourceZh from "./appearance-source.zh-CN.json";
import voiceEn from "./voice.en.json";
import voiceZh from "./voice.zh-CN.json";
import computerUseEn from "./computer-use.en.json";
import computerUseZh from "./computer-use.zh-CN.json";
import configurationEn from "./configuration.en.json";
import configurationZh from "./configuration.zh-CN.json";
import { resolveLocale, type Locale } from "./locale";

const en = { ...coreEn, ...desktopEn, ...processEn, ...rootEn, ...guardianEn, ...connectionsEn, ...appearanceEn, ...gitEn, ...navigationEn, ...projectsEn, ...goalsEn, ...localEnvironmentsEn, ...petsEn, ...pluginMarketplacesEn, ...generalExtraEn, ...appearanceSourceEn, ...voiceEn, ...computerUseEn, ...configurationEn };
const zh = { ...coreZh, ...desktopZh, ...processZh, ...rootZh, ...guardianZh, ...connectionsZh, ...appearanceZh, ...gitZh, ...navigationZh, ...projectsZh, ...goalsZh, ...localEnvironmentsZh, ...petsZh, ...pluginMarketplacesZh, ...generalExtraZh, ...appearanceSourceZh, ...voiceZh, ...computerUseZh, ...configurationZh };
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
export const translateApp = (key: TranslationKey, values?: TranslationValues) => translate(applicationLocale, key, values);
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
