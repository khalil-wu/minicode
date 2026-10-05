import type * as Monaco from "monaco-editor/editor/editor.api.js";

/** Monaco's API entry does not register its editing interactions. */
export function loadMiniCodeEditorFeatures(): Promise<unknown[]> {
  return Promise.all([
    import("monaco-editor/editor/browser/coreCommands.js"),
    import("monaco-editor/editor/contrib/suggest/browser/suggestController.js"),
    import("monaco-editor/editor/contrib/gotoSymbol/browser/goToCommands.js"),
    import("monaco-editor/features/find/register.js"),
    import("monaco-editor/features/gotoLine/register.js"),
    import("monaco-editor/features/folding/register.js"),
    import("monaco-editor/features/format/register.js"),
    import("monaco-editor/features/gotoSymbol/register.js"),
    import("monaco-editor/features/referenceSearch/register.js"),
    import("monaco-editor/features/rename/register.js"),
    import("monaco-editor/features/hover/register.js"),
    import("monaco-editor/features/parameterHints/register.js"),
    import("monaco-editor/features/snippet/register.js"),
    import("monaco-editor/features/inlineCompletions/register.js"),
    import("monaco-editor/features/contextmenu/register.js"),
    import("monaco-editor/features/bracketMatching/register.js"),
    import("monaco-editor/features/indentation/register.js"),
    import("monaco-editor/features/multicursor/register.js"),
    import("monaco-editor/features/linesOperations/register.js"),
    import("monaco-editor/features/wordOperations/register.js"),
    import("monaco-editor/features/wordPartOperations/register.js"),
    import("monaco-editor/features/smartSelect/register.js"),
    import("monaco-editor/features/stickyScroll/register.js"),
    import("monaco-editor/features/tokenization/register.js"),
    import("monaco-editor/features/clipboard/register.js"),
    import("monaco-editor/features/comment/register.js"),
    import("monaco-editor/features/readOnlyMessage/register.js"),
  ]);
}

export const miniCodeCodeEditingOptions = {
  quickSuggestions: { other: true, comments: false, strings: true },
  quickSuggestionsDelay: 120,
  suggestOnTriggerCharacters: true,
  acceptSuggestionOnEnter: "smart",
  tabCompletion: "on",
  wordBasedSuggestions: "matchingDocuments",
  snippetSuggestions: "inline",
  parameterHints: { enabled: true },
  hover: { enabled: "on", delay: 350 },
  suggest: { showStatusBar: true },
} satisfies Monaco.editor.IStandaloneEditorConstructionOptions;
