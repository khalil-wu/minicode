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
export const defaultWorkbenchPreferences: WorkbenchPreferences = {
  speechEnabled: false, speechProvider: "", speechModel: "", speechLanguage: "zh", speechVocabulary: "", microphoneId: "",
  aiEnabled: false, aiModel: "", aiProvider: "", aiMaxTokens: 256,
  proseSize: 14,
  wordWrap: true, minimap: true, tabSize: 4, insertSpaces: true,
  formatOnSave: false, lineNumbers: true, stickyScroll: true, previewTabs: true, snippets: [],
};
export function applyWorkbenchPreferences(preferences: WorkbenchPreferences) {
  const style = document.documentElement.style;
  for (const property of ["--font-ui", "--font-prose", "--font-mono", "--editor-font-family"]) style.removeProperty(property);
  style.setProperty("--mc-font-reading", preferences.proseSize + "px");
  style.setProperty("--prose-font-size", preferences.proseSize + "px");
}
