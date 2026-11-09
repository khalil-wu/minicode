declare module "monaco-editor/editor/common/languages/language.js" {
  export const ILanguageService: unknown;
}

declare module "monaco-editor/languages/features/typescript/tsMode.js";

declare module "monaco-editor/languages/features/typescript/tsWorker.js" {
  import type { Diagnostic, TypeScriptWorker as WorkerRpc } from "monaco-editor/languages/features/typescript/register.js";
  import type * as TS from "typescript";
  export class TypeScriptWorker {
    constructor(context: unknown, createData: unknown);
    static clearFiles(diagnostics: readonly TS.Diagnostic[]): Diagnostic[];
  }
  export interface TypeScriptWorker extends WorkerRpc, TS.LanguageServiceHost {
    _languageService: TS.LanguageService;
    _getScriptText(fileName: string): string | undefined;
    _getModel(fileName: string): { uri: { toString(skipEncoding?: boolean): string }; version: number; getValue(): string } | null;
    getScriptVersion(fileName: string): string;
    readFile(fileName: string): string | undefined;
    fileExists(fileName: string): boolean;
    getLibFiles(): Promise<Record<string, string>>;
  }
}

declare module "monaco-editor/languages/features/typescript/lib/typescriptServices.js" {
  import type * as TS from "typescript";
  export const typescript: typeof TS & {
    getScriptKindFromFileName(fileName: string): TS.ScriptKind;
    matchFiles(path: string, extensions: readonly string[] | undefined, excludes: readonly string[] | undefined,
      includes: readonly string[] | undefined, caseSensitive: boolean, currentDirectory: string, depth: number | undefined,
      getEntries: (path: string) => { files: string[]; directories: string[] }, realpath: (path: string) => string): string[];
  };
}

declare module "monaco-editor/base/common/uri.js" {
  export { Uri as URI } from "monaco-editor/editor/editor.api.js";
}

declare module "monaco-editor/internal/common/initialize.js" {
  export function initialize(factory: (context: unknown, createData: unknown) => unknown): void;
}
