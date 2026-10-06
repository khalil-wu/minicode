import type * as Monaco from "monaco-editor/editor/editor.api.js";
import EditorWorker from "monaco-editor/editor/editor.worker?worker";
import TypeScriptWorker from "./workspaceTypeScriptWorker?worker";
import CssWorker from "monaco-editor/languages/features/css/css.worker?worker";
import HtmlWorker from "./workspaceHtmlWorker?worker";
import JsonWorker from "monaco-editor/languages/features/json/json.worker?worker";
import { normalizeWorkspacePath } from "../lib/workspace-path";
import type { IExtraLibs } from "monaco-editor/languages/features/typescript/register.js";
import { WORKSPACE_TYPESCRIPT_METADATA_URI, type WorkspaceTypeScriptMetadata } from "./workspaceTypeScriptContract";
import { loadMiniCodeEditorFeatures } from "./monacoEditorFeatures";
import { registerEditorLanguageServices } from "./editorLanguageClient";
import { registerEditorSnippets } from "./editorSnippets";
import { registerInlinePrediction } from "./editorInlineCompletion";

export function configureMiniCodeMonacoWorkers(): void {
  globalThis.MonacoEnvironment = {
    ...globalThis.MonacoEnvironment,
    getWorker: (_workerId, label) => {
      switch (label) {
        case "typescript":
        case "javascript": return new TypeScriptWorker();
        case "css":
        case "scss":
        case "less": return new CssWorker();
        case "html":
        case "handlebars":
        case "razor": return new HtmlWorker();
        case "json": return new JsonWorker();
        default: return new EditorWorker();
      }
    },
  };
}

let languageServicesPromise: Promise<void> | undefined;
let typescriptServices: typeof import("monaco-editor/languages/features/typescript/register.js");

export function loadMiniCodeLanguageServices(): Promise<void> {
  return languageServicesPromise ??= (async () => {
    const [typescript, , , , { StandaloneServices }, { ILanguageService }] = await Promise.all([
      import("monaco-editor/languages/features/typescript/register.js"),
      import("monaco-editor/languages/features/css/register.js"),
      import("monaco-editor/languages/features/html/register.js"),
      import("monaco-editor/languages/features/json/register.js"),
      import("monaco-editor/editor/standalone/browser/standaloneServices.js"),
      import("monaco-editor/editor/common/languages/language.js"),
      loadMiniCodeEditorFeatures(),
      import("monaco-editor/languages/definitions/cpp/register.js"),
    ]);
    typescriptServices = typescript;
    const compilerOptions = {
      target: typescript.ScriptTarget.ESNext,
      module: typescript.ModuleKind.ESNext,
      moduleResolution: typescript.ModuleResolutionKind.NodeJs,
      jsx: typescript.JsxEmit.Preserve,
      allowJs: true,
      allowSyntheticDefaultImports: true,
    };
    for (const defaults of [typescript.typescriptDefaults, typescript.javascriptDefaults]) {
      defaults.setCompilerOptions({ ...defaults.getCompilerOptions(), ...compilerOptions });
      defaults.setEagerModelSync(true);
      defaults.setDiagnosticsOptions({ ...defaults.getDiagnosticsOptions(), onlyVisible: true });
    }
    // The project index needs both services even when the first file is HTML
    // or the workspace contains only JS. Use Monaco's once-per-language
    // activation so later models do not register a second set of providers.
    // Load the mode's production chunks before firing Monaco's async listeners;
    // Vite's first preload otherwise races a concurrent worker getter.
    await import("monaco-editor/languages/features/typescript/tsMode.js");
    const languages = StandaloneServices.get<{ requestRichLanguageFeatures: (languageId: string) => void }>(ILanguageService);
    languages.requestRichLanguageFeatures("typescript");
    languages.requestRichLanguageFeatures("javascript");
    await Promise.all([typescript.getTypeScriptWorker(), typescript.getJavaScriptWorker()]);
    registerEditorLanguageServices(await import("monaco-editor/editor/editor.api.js"));
    registerEditorSnippets(await import("monaco-editor/editor/editor.api.js"));
    registerInlinePrediction(await import("monaco-editor/editor/editor.api.js"));
  })();
}

export function setWorkspaceTypeScriptFiles(metadata: WorkspaceTypeScriptMetadata, files: Array<{ filePath: string; content: string }>): void {
  const extraLibs = [...files, { filePath: WORKSPACE_TYPESCRIPT_METADATA_URI, content: JSON.stringify(metadata) }];
  for (const defaults of [typescriptServices.typescriptDefaults, typescriptServices.javascriptDefaults]) defaults.setExtraLibs(extraLibs);
}

export async function getWorkspaceTypeScriptWorker(resource: Monaco.Uri) {
  const factory = /\.[cm]?tsx?$/i.test(resource.path)
    ? await typescriptServices.getTypeScriptWorker()
    : await typescriptServices.getJavaScriptWorker();
  return factory(resource);
}

export async function syncWorkspaceTypeScriptModels(resources: Monaco.Uri[]): Promise<{
  configurationRequests: () => Promise<string[]>;
  configurationDiagnostics: () => Promise<Array<{ path: string; message: string }>>;
  addConfigurationFiles: (files: Array<{ filePath: string; content: string }>) => Promise<void>;
}> {
  const [typescriptFactory, javascriptFactory] = await Promise.all([typescriptServices.getTypeScriptWorker(), typescriptServices.getJavaScriptWorker()]);
  const [typescriptWorker, javascriptWorker] = await Promise.all([typescriptFactory(...resources), javascriptFactory(...resources)]);
  const workers = [typescriptWorker, javascriptWorker] as unknown as Array<{
    getConfigurationFileRequests: () => Promise<string[]>;
    getConfigurationDiagnostics: () => Promise<Array<{ path: string; message: string }>>;
    updateExtraLibs: (extraLibs: IExtraLibs) => Promise<void>;
  }>;
  return {
    configurationRequests: async () => [...new Set((await Promise.all(workers.map((worker) => worker.getConfigurationFileRequests()))).flat())],
    configurationDiagnostics: async () => [...new Map((await Promise.all(workers.map((worker) => worker.getConfigurationDiagnostics()))).flat().map((issue) => [`${issue.path}:${issue.message}`, issue])).values()],
    addConfigurationFiles: async (files) => {
      const extras = Object.entries(typescriptServices.typescriptDefaults.getExtraLibs()).map(([filePath, entry]) => ({ filePath, content: entry.content }));
      const merged = new Map(extras.map((file) => [file.filePath, file]));
      for (const file of files) merged.set(file.filePath, file);
      for (const defaults of [typescriptServices.typescriptDefaults, typescriptServices.javascriptDefaults]) defaults.setExtraLibs([...merged.values()]);
      await Promise.all(workers.map((worker, index) => worker.updateExtraLibs(index === 0 ? typescriptServices.typescriptDefaults.getExtraLibs() : typescriptServices.javascriptDefaults.getExtraLibs())));
    },
  };
}

/** Real file paths let Monaco resolve relative imports between open models. */
export function editorModelUri(path: string, workspaceRoot: string): string {
  const normalizedPath = normalizeWorkspacePath(path);
  const absolutePath = normalizedPath.startsWith("/") || /^[A-Za-z]:\//.test(normalizedPath)
    ? normalizedPath
    : normalizeWorkspacePath(`${workspaceRoot}/${normalizedPath}`);
  if (absolutePath.startsWith("//")) {
    const [host, ...parts] = absolutePath.slice(2).split("/");
    return `file://${host}/${parts.map(encodeURIComponent).join("/")}`;
  }
  return `file:///${absolutePath.replace(/^\//, "").split("/").map(encodeURIComponent).join("/")}`;
}

type OpenEditorFile = (
  path: string,
  label?: string,
  target?: { line?: number; column?: number; exact?: boolean },
) => void;

export function registerMiniCodeEditorOpener(monaco: typeof Monaco, openFile: OpenEditorFile): Monaco.IDisposable {
  return monaco.editor.registerEditorOpener({
    openCodeEditor: (_source, resource, selectionOrPosition) => {
      if (resource.scheme !== "file") return false;
      const position = selectionOrPosition && ("startLineNumber" in selectionOrPosition
        ? { line: selectionOrPosition.startLineNumber, column: selectionOrPosition.startColumn }
        : { line: selectionOrPosition.lineNumber, column: selectionOrPosition.column });
      const path = normalizeWorkspacePath(resource.fsPath);
      openFile(path, path.split("/").pop(), { ...position, exact: true });
      return true;
    },
  });
}
