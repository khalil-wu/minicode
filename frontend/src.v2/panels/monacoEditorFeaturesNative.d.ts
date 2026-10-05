declare module "monaco-editor/editor/browser/coreCommands.js";
declare module "monaco-editor/editor/common/services/languageFeatures.js" {
  export const ILanguageFeaturesService: unknown;
}
declare module "monaco-editor/editor/contrib/suggest/browser/suggestController.js";
declare module "monaco-editor/editor/contrib/gotoSymbol/browser/goToCommands.js";

declare module "monaco-editor/editor/browser/editorExtensions.js" {
  export namespace EditorExtensionsRegistry {
    function getEditorContributions(): Array<{ id: string }>;
    function getEditorCommand(id: string): unknown;
  }
}
declare module "monaco-editor/platform/commands/common/commands.js" {
  export namespace CommandsRegistry {
    function getCommand(id: string): unknown;
  }
}
declare module "monaco-editor/languages/features/css/cssWorker.js";
declare module "monaco-editor/languages/features/html/htmlWorker.js";
declare module "monaco-editor/languages/features/json/jsonWorker.js";
declare module "monaco-editor/languages/features/css/cssMode.js";
declare module "monaco-editor/languages/features/html/htmlMode.js";
declare module "monaco-editor/languages/features/json/jsonMode.js";
