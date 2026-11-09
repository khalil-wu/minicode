import type * as Monaco from "monaco-editor/editor/editor.api.js";
import { language as cppLanguage } from "monaco-editor/languages/definitions/cpp/cpp.js";
import { useAppStore } from "../stores";
export function registerEditorSnippets(monaco: typeof Monaco) {
  return monaco.languages.registerCompletionItemProvider(["typescript", "javascript", "python", "yaml", "html", "css", "json", "c", "cpp"], {
    provideCompletionItems(model, position) {
      const word = model.getWordUntilPosition(position);
      const range = { startLineNumber: position.lineNumber, endLineNumber: position.lineNumber, startColumn: word.startColumn, endColumn: word.endColumn };
      const keywords = model.getLanguageId() === "cpp" && word.word ? cppLanguage.keywords.map((keyword) => ({
        label: keyword, kind: monaco.languages.CompletionItemKind.Keyword, insertText: keyword, range,
      })) : [];
      return { suggestions: [...keywords, ...useAppStore.getState().workbenchPreferences.snippets.filter((snippet) => snippet.language === model.getLanguageId()).map((snippet) => ({
        label: snippet.prefix, detail: snippet.description, documentation: { value: "~~~\n" + snippet.body + "\n~~~" },
        kind: monaco.languages.CompletionItemKind.Snippet, insertText: snippet.body,
        insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet,
        range,
      }))] };
    },
  });
}
