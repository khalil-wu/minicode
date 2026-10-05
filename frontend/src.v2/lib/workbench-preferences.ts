export interface CodeSnippet { id: string; language: string; prefix: string; body: string; description: string }
export interface WorkbenchPreferences {
  speechEnabled: boolean;
  speechProvider: string;
  speechModel: string;
  speechLanguage: string;
  speechVocabulary: string;
  microphoneId: string;
  aiEnabled: boolean;
  aiModel: string;
  aiProvider: string;
  aiMaxTokens: number;
  uiFont: string;
  proseFont: string;
  codeFont: string;
  proseSize: number;
  wordWrap: boolean;
  minimap: boolean;
  tabSize: number;
  insertSpaces: boolean;
  formatOnSave: boolean;
  ligatures: boolean;
  lineNumbers: boolean;
  stickyScroll: boolean;
  previewTabs: boolean;
  snippets: CodeSnippet[];
}
export const defaultWorkbenchPreferences: WorkbenchPreferences = {
  speechEnabled: false, speechProvider: "", speechModel: "", speechLanguage: "zh", speechVocabulary: "", microphoneId: "",
  aiEnabled: false, aiModel: "", aiProvider: "", aiMaxTokens: 256,
  uiFont: "", proseFont: "", codeFont: "", proseSize: 16,
  wordWrap: true, minimap: false, tabSize: 4, insertSpaces: true,
  formatOnSave: false, ligatures: false, lineNumbers: true, stickyScroll: true, previewTabs: true, snippets: [],
};
export function applyWorkbenchPreferences(preferences: WorkbenchPreferences) {
  const style = document.documentElement.style;
  for (const [property, value] of [["--font-ui", preferences.uiFont], ["--font-prose", preferences.proseFont],
    ["--font-mono", preferences.codeFont], ["--editor-font-family", preferences.codeFont]]) {
    if (value) style.setProperty(property, value); else style.removeProperty(property);
  }
  style.setProperty("--mc-font-reading", preferences.proseSize + "px");
  style.setProperty("--prose-font-size", preferences.proseSize + "px");
}
