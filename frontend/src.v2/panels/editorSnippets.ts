import type * as Monaco from "monaco-editor/editor/editor.api.js";
import { useAppStore } from "../stores";
export function registerEditorSnippets(monaco: typeof Monaco) {
  return monaco.languages.registerCompletionItemProvider(["typescript", "javascript", "python", "yaml", "html", "css", "json"], {
    provideCompletionItems(model, position) {
      const word = model.getWordUntilPosition(position);
      return { suggestions: useAppStore.getState().workbenchPreferences.snippets.filter((snippet) => snippet.language === model.getLanguageId()).map((snippet) => ({
        label: snippet.prefix, detail: snippet.description, documentation: { value: "~~~\n" + snippet.body + "\n~~~" },
        kind: monaco.languages.CompletionItemKind.Snippet, insertText: snippet.body,
        insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet,
        range: { startLineNumber: position.lineNumber, endLineNumber: position.lineNumber, startColumn: word.startColumn, endColumn: word.endColumn },
      })) };
    },
  });
}
