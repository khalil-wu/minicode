import { URI } from "monaco-editor/base/common/uri.js";
import { TypeScriptWorker as NativeTypeScriptWorker } from "monaco-editor/languages/features/typescript/tsWorker.js";
import { typescript as ts } from "monaco-editor/languages/features/typescript/lib/typescriptServices.js";
import type { IExtraLibs, TypeScriptWorker as TypeScriptWorkerRpc } from "monaco-editor/languages/features/typescript/register.js";
import type * as TS from "typescript";
import { WORKSPACE_TYPESCRIPT_METADATA_URI, type WorkspaceTypeScriptMetadata } from "./workspaceTypeScriptContract";
export { WORKSPACE_TYPESCRIPT_METADATA_URI, type WorkspaceTypeScriptMetadata } from "./workspaceTypeScriptContract";

export interface WorkspaceMirrorModel {
  uri: { toString(skipEncoding?: boolean): string };
  version: number;
  getValue(): string;
}

export interface WorkspaceTypeScriptContext { getMirrorModels(): WorkspaceMirrorModel[]; }
export interface WorkspaceTypeScriptCreateData {
  compilerOptions: TS.CompilerOptions;
  extraLibs: IExtraLibs;
  inlayHintsOptions?: object;
}

const canonicalUri = (fileName: string): string => fileName.startsWith("file:") ? URI.parse(fileName).toString() : fileName;
const parentUri = (fileName: string): string => {
  const uri = URI.parse(fileName);
  return uri.with({ path: uri.path.slice(0, uri.path.lastIndexOf("/")) || "/" }).toString();
};
const fileBasename = (fileName: string): string => URI.parse(fileName).path.split("/").pop()!;
const isDependency = (fileName: string): boolean => URI.parse(fileName).path.toLowerCase().split("/").includes("node_modules");
const configName = (fileName: string): boolean => /^(?:tsconfig(?:\.[^.]+)*|jsconfig)\.json$/i.test(fileBasename(fileName));

interface WorkspaceProject {
  configFileName?: string;
  directory: string;
  parsed: TS.ParsedCommandLine;
  worker?: WorkspaceProjectWorker;
}

class WorkspaceSnapshot {
  metadata: WorkspaceTypeScriptMetadata;
  mirrors = new Map<string, WorkspaceMirrorModel>();
  private files = new Map<string, { fileName: string; entry: IExtraLibs[string] }>();
  private directories = new Map<string, { files: Set<string>; directories: Set<string> }>();
  private configurationRequests = new Set<string>();

  constructor(readonly context: WorkspaceTypeScriptContext, readonly extraLibs: IExtraLibs) {
    this.metadata = JSON.parse(extraLibs[WORKSPACE_TYPESCRIPT_METADATA_URI].content);
    for (const [fileName, entry] of Object.entries(extraLibs)) {
      if (!fileName.startsWith("file:")) continue;
      const normalized = canonicalUri(fileName);
      this.files.set(this.key(normalized), { fileName: normalized, entry });
      this.addDirectoryEntry(normalized);
    }
    // Monaco creates this service before its worker server exists. Mirror
    // models become available when the first RPC query reaches projectFor.
  }

  key(fileName: string): string {
    const uri = canonicalUri(fileName);
    return this.metadata.caseSensitive ? uri : uri.toLowerCase();
  }

  refreshMirrors(): void {
    this.mirrors.clear();
    for (const model of this.context.getMirrorModels()) {
      const fileName = canonicalUri(model.uri.toString());
      this.mirrors.set(this.key(fileName), model);
      this.addDirectoryEntry(fileName);
    }
  }

  readFile = (fileName: string): string | undefined => {
    const key = this.key(fileName);
    return this.mirrors.get(key)?.getValue() ?? this.files.get(key)?.entry.content;
  };
  fileExists = (fileName: string): boolean => this.readFile(fileName) !== undefined;
  readConfigurationFile = (fileName: string): string | undefined => {
    const content = this.readFile(fileName);
    if (content === undefined && fileName.startsWith("file:") && /\.json$/i.test(URI.parse(fileName).path)) {
      this.configurationRequests.add(canonicalUri(fileName));
    }
    return content;
  };
  configurationFileExists = (fileName: string): boolean => this.readConfigurationFile(fileName) !== undefined;
  requestedConfigurationFiles(): string[] { return Array.from(this.configurationRequests); }
  directoryExists = (directory: string): boolean => this.directories.has(this.directoryKey(directory));
  getDirectories = (directory: string): string[] => Array.from(this.directories.get(this.directoryKey(directory))?.directories ?? []);
  version(fileName: string): string | undefined {
    const key = this.key(fileName);
    const model = this.mirrors.get(key);
    const file = this.files.get(key);
    return model ? String(model.version) : file ? String(file.entry.version) : undefined;
  }
  fileNames(): string[] { return Array.from(this.files.values(), (file) => file.fileName); }
  sourceNames(): string[] { return this.metadata.sourceFileNames.map(canonicalUri); }
  readDirectory = (root: string, extensions?: readonly string[], excludes?: readonly string[], includes?: readonly string[], depth?: number): string[] => {
    return ts.matchFiles(root, extensions, excludes, includes, this.metadata.caseSensitive, this.metadata.workspaceRoot, depth,
      (directory) => {
        const entry = this.directories.get(this.directoryKey(directory));
        return { files: Array.from(entry?.files ?? []), directories: Array.from(entry?.directories ?? []) };
      }, canonicalUri);
  };

  private directoryKey(directory: string): string { return this.key(directory).replace(/\/$/, ""); }
  private directory(directory: string) {
    const key = this.directoryKey(directory);
    let entry = this.directories.get(key);
    if (!entry) {
      entry = { files: new Set<string>(), directories: new Set<string>() };
      this.directories.set(key, entry);
    }
    return entry;
  }
  private addDirectoryEntry(fileName: string): void {
    let directory = parentUri(fileName);
    this.directory(directory).files.add(fileBasename(fileName));
    while (URI.parse(directory).path !== "/" && URI.parse(directory).path !== "") {
      const parent = parentUri(directory);
      this.directory(parent).directories.add(fileBasename(directory));
      directory = parent;
    }
  }
}

class WorkspaceProjectWorker extends NativeTypeScriptWorker {
  constructor(readonly snapshot: WorkspaceSnapshot, readonly project: WorkspaceProject,
    readonly service: WorkspaceTypeScriptService, createData: WorkspaceTypeScriptCreateData) {
    super(snapshot.context, { ...createData, compilerOptions: project.parsed.options, extraLibs: snapshot.extraLibs });
    this._languageService.dispose();
    this._languageService = ts.createLanguageService(this);
  }
  getScriptFileNames = (): string[] => this.project.parsed.fileNames;
  getScriptKind(fileName: string): TS.ScriptKind { return ts.getScriptKindFromFileName(fileName); }
  getCurrentDirectory = (): string => this.project.directory;
  useCaseSensitiveFileNames = (): boolean => this.snapshot.metadata.caseSensitive;
  getProjectReferences = (): readonly TS.ProjectReference[] | undefined => this.project.parsed.projectReferences;
  getParsedCommandLine = (configFileName: string): TS.ParsedCommandLine | undefined => this.service.parsedConfig(configFileName);
  useSourceOfProjectReferenceRedirect(): boolean { return true; }
  directoryExists = (directory: string): boolean => this.snapshot.directoryExists(directory);
  getDirectories = (directory: string): string[] => this.snapshot.getDirectories(directory);
  readDirectory = (root: string, extensions?: readonly string[], excludes?: readonly string[], includes?: readonly string[], depth?: number): string[] => this.snapshot.readDirectory(root, extensions, excludes, includes, depth);
  _getModel = (fileName: string): WorkspaceMirrorModel | null => this.snapshot.mirrors.get(this.snapshot.key(fileName)) ?? null;
  _getScriptText = (fileName: string): string | undefined => this.snapshot.readFile(fileName) ?? super._getScriptText(fileName);
  getScriptVersion = (fileName: string): string => this.snapshot.version(fileName) ?? super.getScriptVersion(fileName);
  readFile = (fileName: string): string | undefined => this._getScriptText(fileName);
  fileExists = (fileName: string): boolean => this._getScriptText(fileName) !== undefined;
  getCompilerOptionsDiagnostics(fileName: string) {
    return super.getCompilerOptionsDiagnostics(fileName).then((diagnostics) => [
      ...diagnostics,
      ...NativeTypeScriptWorker.clearFiles(this.project.parsed.errors),
    ]);
  }
}

export class WorkspaceTypeScriptService implements TypeScriptWorkerRpc {
  private snapshot: WorkspaceSnapshot;
  private projects = new Map<string, WorkspaceProject>();
  private viewProjects = new Map<string, WorkspaceProject>();
  private defaultProject: WorkspaceProject;

  constructor(private readonly context: WorkspaceTypeScriptContext, private createData: WorkspaceTypeScriptCreateData) {
    this.snapshot = new WorkspaceSnapshot(context, createData.extraLibs);
    this.defaultProject = this.createDefaultProject();
    this.buildProjects();
  }

  private createDefaultProject(): WorkspaceProject {
    return { directory: this.snapshot.metadata.workspaceRoot, parsed: { options: this.createData.compilerOptions, fileNames: this.snapshot.sourceNames(), errors: [] } };
  }

  private parseConfig(configFileName: string): WorkspaceProject {
    const normalized = canonicalUri(configFileName);
    const key = this.snapshot.key(normalized);
    const existing = this.projects.get(key);
    if (existing) return existing;
    const directory = parentUri(normalized);
    const config = ts.readJsonConfigFile(normalized, this.snapshot.readConfigurationFile);
    const parsed = ts.parseJsonSourceFileConfigFileContent(config, {
      useCaseSensitiveFileNames: this.snapshot.metadata.caseSensitive,
      readDirectory: this.snapshot.readDirectory, fileExists: this.snapshot.configurationFileExists, readFile: this.snapshot.readConfigurationFile,
    }, directory, undefined, normalized);
    Object.assign(parsed.options, { ...this.createData.compilerOptions, ...parsed.options });
    const sourceKeys = new Set(this.snapshot.sourceNames().map((fileName) => this.snapshot.key(fileName)));
    parsed.fileNames = parsed.fileNames.map(canonicalUri).filter((fileName) => sourceKeys.has(this.snapshot.key(fileName)));
    const project = { configFileName: normalized, directory, parsed };
    this.projects.set(key, project);
    return project;
  }

  private buildProjects(): void {
    for (const fileName of this.snapshot.fileNames()) {
      if (configName(fileName) && !isDependency(fileName)) this.parseConfig(fileName);
    }
    for (const project of this.projects.values()) {
      for (const reference of project.parsed.projectReferences ?? []) this.parsedConfig(ts.resolveProjectReferencePath(reference));
    }
  }

  parsedConfig(configFileName: string): TS.ParsedCommandLine | undefined {
    const resolved = configFileName.endsWith(".json") ? configFileName : `${configFileName.replace(/\/$/, "")}/tsconfig.json`;
    return this.snapshot.configurationFileExists(resolved) ? this.parseConfig(resolved).parsed : undefined;
  }

  private projectFor(fileName: string): WorkspaceProject {
    this.snapshot.refreshMirrors();
    for (const [key, project] of this.viewProjects) {
      if (!this.snapshot.mirrors.has(key)) { project.worker?._languageService.dispose(); this.viewProjects.delete(key); }
    }
    const key = this.snapshot.key(fileName);
    const candidates = Array.from(this.projects.values()).filter((project) =>
      project.parsed.fileNames.some((source) => this.snapshot.key(source) === key));
    candidates.sort((left, right) => right.directory.length - left.directory.length
      || Number(/(?:tsconfig|jsconfig)\.json$/.test(right.configFileName!)) - Number(/(?:tsconfig|jsconfig)\.json$/.test(left.configFileName!)));
    if (candidates[0]) return candidates[0];
    if (this.snapshot.sourceNames().some((source) => this.snapshot.key(source) === key)) return this.defaultProject;
    if (!this.snapshot.mirrors.has(key)) return this.defaultProject;
    // Diff excerpts, dependency viewers and an opened file before indexing
    // still need a native root. Keep each view out of the workspace program.
    let project = this.viewProjects.get(key);
    if (!project) {
      project = { directory: parentUri(fileName), parsed: { options: this.createData.compilerOptions, fileNames: [fileName], errors: [] } };
      this.viewProjects.set(key, project);
    }
    return project;
  }

  private workerFor(fileName: string): WorkspaceProjectWorker {
    const project = this.projectFor(fileName);
    return project.worker ??= new WorkspaceProjectWorker(this.snapshot, project, this, this.createData);
  }

  private relatedWorkers(fileName: string): WorkspaceProjectWorker[] {
    const owner = this.workerFor(fileName);
    const workers = new Set([owner]);
    for (const project of [...this.projects.values(), this.defaultProject]) {
      const worker = project.worker ??= new WorkspaceProjectWorker(this.snapshot, project, this, this.createData);
      if (worker._languageService.getProgram()?.getSourceFile(fileName)) workers.add(worker);
    }
    return Array.from(workers);
  }

  private mergeLocations<T extends { fileName: string; textSpan: { start: number; length: number } }>(groups: readonly (readonly T[] | undefined)[]): T[] {
    const locations = new Map<string, T>();
    for (const group of groups) {
      for (const location of group ?? []) {
        locations.set(`${this.snapshot.key(location.fileName)}:${location.textSpan.start}:${location.textSpan.length}`, location);
      }
    }
    return Array.from(locations.values());
  }

  getSyntacticDiagnostics(fileName: string) { return this.workerFor(fileName).getSyntacticDiagnostics(fileName); }
  getSemanticDiagnostics(fileName: string) { return this.workerFor(fileName).getSemanticDiagnostics(fileName); }
  getSuggestionDiagnostics(fileName: string) { return this.workerFor(fileName).getSuggestionDiagnostics(fileName); }
  getCompilerOptionsDiagnostics(fileName: string) { return this.workerFor(fileName).getCompilerOptionsDiagnostics(fileName); }
  getCompletionsAtPosition(fileName: string, position: number) { return this.workerFor(fileName).getCompletionsAtPosition(fileName, position); }
  getCompletionEntryDetails(fileName: string, position: number, entry: string) { return this.workerFor(fileName).getCompletionEntryDetails(fileName, position, entry); }
  getSignatureHelpItems(fileName: string, position: number, options: object) { return this.workerFor(fileName).getSignatureHelpItems(fileName, position, options); }
  getQuickInfoAtPosition(fileName: string, position: number) { return this.workerFor(fileName).getQuickInfoAtPosition(fileName, position); }
  getDocumentHighlights(fileName: string, position: number, filesToSearch: string[]) { return this.workerFor(fileName).getDocumentHighlights(fileName, position, filesToSearch); }
  getDefinitionAtPosition(fileName: string, position: number) { return this.workerFor(fileName).getDefinitionAtPosition(fileName, position); }
  async getReferencesAtPosition(fileName: string, position: number) {
    return this.mergeLocations(await Promise.all(this.relatedWorkers(fileName).map((worker) => worker.getReferencesAtPosition(fileName, position))));
  }
  getNavigationTree(fileName: string) { return this.workerFor(fileName).getNavigationTree(fileName); }
  getFormattingEditsForDocument(fileName: string, options: object) { return this.workerFor(fileName).getFormattingEditsForDocument(fileName, options); }
  getFormattingEditsForRange(fileName: string, start: number, end: number, options: object) { return this.workerFor(fileName).getFormattingEditsForRange(fileName, start, end, options); }
  getFormattingEditsAfterKeystroke(fileName: string, position: number, ch: string, options: object) { return this.workerFor(fileName).getFormattingEditsAfterKeystroke(fileName, position, ch, options); }
  async getRenameInfo(fileName: string, position: number, options: object) {
    const worker = this.workerFor(fileName);
    const info = await worker.getRenameInfo(fileName, position, options);
    if (!info.canRename) return info;
    const locations = isDependency(fileName) ? [] : await this.findRenameLocations(fileName, position, false, false, false);
    if (isDependency(fileName) || locations?.some((location) => isDependency(location.fileName))) {
      return { canRename: false, localizedErrorMessage: "不能重命名 node_modules 中的依赖声明；可以重命名项目内的别名。" };
    }
    const readOnly = new Set(this.snapshot.metadata.readOnlyFileNames.map((source) => this.snapshot.key(source)));
    if (readOnly.has(this.snapshot.key(fileName)) || locations.some((location) => readOnly.has(this.snapshot.key(location.fileName)))) {
      return { canRename: false, localizedErrorMessage: "重命名涉及只读文件，无法修改全部引用。" };
    }
    return info;
  }
  async findRenameLocations(fileName: string, position: number, strings: boolean, comments: boolean, prefixAndSuffix: boolean) {
    return this.mergeLocations(await Promise.all(this.relatedWorkers(fileName).map((worker) => worker.findRenameLocations(fileName, position, strings, comments, prefixAndSuffix))));
  }
  getEmitOutput(fileName: string, emitOnlyDtsFiles?: boolean, forceDtsEmit?: boolean) { return this.workerFor(fileName).getEmitOutput(fileName, emitOnlyDtsFiles, forceDtsEmit); }
  getCodeFixesAtPosition(fileName: string, start: number, end: number, codes: number[], options: object) { return this.workerFor(fileName).getCodeFixesAtPosition(fileName, start, end, codes, options); }
  provideInlayHints(fileName: string, start: number, end: number) { return this.workerFor(fileName).provideInlayHints(fileName, start, end); }
  getScriptText(fileName: string) { return this.workerFor(fileName).getScriptText(fileName); }
  getLibFiles() { return this.workerFor(this.snapshot.sourceNames()[0] ?? this.snapshot.metadata.workspaceRoot).getLibFiles(); }
  async getConfigurationFileRequests(): Promise<string[]> { return this.snapshot.requestedConfigurationFiles(); }
  async getConfigurationDiagnostics(): Promise<Array<{ path: string; message: string }>> {
    return Array.from(this.projects.values()).flatMap((project) => project.parsed.errors.map((diagnostic) => ({
      path: project.configFileName!,
      message: ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n"),
    })));
  }
  async updateExtraLibs(extraLibs: IExtraLibs): Promise<void> {
    for (const project of [...this.projects.values(), ...this.viewProjects.values(), this.defaultProject]) project.worker?._languageService.dispose();
    this.createData = { ...this.createData, extraLibs };
    this.snapshot = new WorkspaceSnapshot(this.context, extraLibs);
    this.snapshot.refreshMirrors();
    this.projects.clear();
    this.viewProjects.clear();
    this.defaultProject = this.createDefaultProject();
    this.buildProjects();
  }
}

export function createWorkspaceTypeScriptService(context: WorkspaceTypeScriptContext, createData: WorkspaceTypeScriptCreateData): WorkspaceTypeScriptService {
  return new WorkspaceTypeScriptService(context, createData);
}
