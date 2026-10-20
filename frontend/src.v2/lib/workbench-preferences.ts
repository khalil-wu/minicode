export interface CodeSnippet { id: string; language: string; prefix: string; body: string; description: string }
export interface WorkbenchPreferences {
  speechEnabled: boolean;
  speechProvider: string;
  speechModel: string;
  speechLanguage: string;
  dictationDictionary: string[];
  microphoneId: string;
  aiEnabled: boolean;
  aiModel: string;
  aiProvider: string;
  aiMaxTokens: number;
  proseSize: number;
  wordWrap: boolean;
  minimap: boolean;
  tabSize: number;
  insertSpaces: boolean;
  formatOnSave: boolean;
  lineNumbers: boolean;
  stickyScroll: boolean;
  previewTabs: boolean;
  snippets: CodeSnippet[];
}
export type DictationPreferences = Pick<WorkbenchPreferences, "speechEnabled" | "speechProvider" | "speechModel" | "speechLanguage" | "microphoneId" | "dictationDictionary">;
const dictationPreferenceKeys = new Set(["speechEnabled", "speechProvider", "speechModel", "speechLanguage", "microphoneId", "dictationDictionary", "speechVocabulary"]);
let desktopDictationPreferences = false;
export const usesDesktopDictationPreferences = () => desktopDictationPreferences;
export function useDesktopDictationPreferences() { desktopDictationPreferences = true; }
export function dictationPreferencesOf(preferences: DictationPreferences): DictationPreferences {
  return { speechEnabled: preferences.speechEnabled, speechProvider: preferences.speechProvider, speechModel: preferences.speechModel,
    speechLanguage: preferences.speechLanguage, microphoneId: preferences.microphoneId, dictationDictionary: preferences.dictationDictionary };
}
export function persistedWorkbenchPreferences(preferences: WorkbenchPreferences) {
  return desktopDictationPreferences ? Object.fromEntries(Object.entries(preferences).filter(([key]) => !dictationPreferenceKeys.has(key))) : preferences;
}
export const defaultWorkbenchPreferences: WorkbenchPreferences = {
  speechEnabled: false, speechProvider: "", speechModel: "", speechLanguage: "zh", dictationDictionary: [], microphoneId: "",
  aiEnabled: false, aiModel: "", aiProvider: "", aiMaxTokens: 256,
  proseSize: 14,
  wordWrap: true, minimap: true, tabSize: 4, insertSpaces: true,
  formatOnSave: false, lineNumbers: true, stickyScroll: true, previewTabs: true, snippets: [],
};
export function migrateWorkbenchPreferences(stored: Partial<WorkbenchPreferences> & { speechVocabulary?: string }): WorkbenchPreferences {
  const { speechVocabulary, ...preferences } = stored;
  return { ...defaultWorkbenchPreferences, ...preferences,
    dictationDictionary: preferences.dictationDictionary ?? (speechVocabulary?.trim() ? [speechVocabulary.trim()] : []) };
}
export function applyWorkbenchPreferences(preferences: WorkbenchPreferences) {
  const style = document.documentElement.style;
  style.setProperty("--mc-font-reading", preferences.proseSize + "px");
  style.setProperty("--prose-font-size", preferences.proseSize + "px");
}
