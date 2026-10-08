import { describe, expect, it } from "vitest";
import { URI } from "monaco-editor/base/common/uri.js";
import { typescript as ts } from "monaco-editor/languages/features/typescript/lib/typescriptServices.js";
import type { IExtraLibs } from "monaco-editor/languages/features/typescript/register.js";
import { editorModelUri } from "./monacoLanguageServices";
import { WORKSPACE_TYPESCRIPT_METADATA_URI, WorkspaceTypeScriptService, type WorkspaceMirrorModel } from "./workspaceTypeScriptService";

interface IndexedFile { path: string; content: string; source?: boolean; readOnly?: boolean; }

const modelUri = (path: string, root = "/project") => URI.parse(editorModelUri(path, root)).toString();
const indexExtras = (files: IndexedFile[], root = "/project", caseSensitive = true): IExtraLibs => {
  const extraLibs: IExtraLibs = {};
  for (const file of files) extraLibs[modelUri(file.path, root)] = { content: file.content, version: 1 };
  extraLibs[WORKSPACE_TYPESCRIPT_METADATA_URI] = {
    version: 1,
    content: JSON.stringify({
      workspaceRoot: modelUri(".", root), caseSensitive,
      sourceFileNames: files.filter((file) => file.source ?? (!file.path.includes("node_modules/") && /\.[cm]?[jt]sx?$/.test(file.path))).map((file) => modelUri(file.path, root)),
      readOnlyFileNames: files.filter((file) => file.readOnly).map((file) => modelUri(file.path, root)),
    }),
  };
  return extraLibs;
};
const fixture = (files: IndexedFile[], root = "/project", caseSensitive = true) => {
  const mirrors: WorkspaceMirrorModel[] = [];
  const service = new WorkspaceTypeScriptService({ getMirrorModels: () => mirrors }, {
    compilerOptions: { target: ts.ScriptTarget.ESNext, module: ts.ModuleKind.ESNext, moduleResolution: ts.ModuleResolutionKind.NodeJs, allowJs: true, jsx: ts.JsxEmit.Preserve },
    extraLibs: indexExtras(files, root, caseSensitive),
  });
  return { service, mirrors, uri: (path: string) => modelUri(path, root) };
};

describe("workspace TypeScript service", () => {
  it("keeps the native parsed project for a repeated versioned snapshot and rebuilds when source content changes", async () => {
    const files = [
      { path: "tsconfig.json", content: '{"compilerOptions":{"strict":true},"include":["src"]}' },
      { path: "src/main.ts", content: "export const value: number = 1;" },
    ];
    const { service, uri } = fixture(files);
    const parsed = service.parsedConfig(uri("tsconfig.json"));
    expect(await service.getSemanticDiagnostics(uri("src/main.ts"))).toEqual([]);
    await service.updateExtraLibs(indexExtras(files));
    expect(service.parsedConfig(uri("tsconfig.json"))).toBe(parsed);
    files[1].content = 'export const value: number = "changed";';
    await service.updateExtraLibs(indexExtras(files));
    expect(service.parsedConfig(uri("tsconfig.json"))).not.toBe(parsed);
    expect(await service.getSemanticDiagnostics(uri("src/main.ts"))).toEqual(expect.arrayContaining([expect.objectContaining({ code: 2322 })]));
  });

  it("gives an opened mirror a native root before the source index arrives", async () => {
    const { service, mirrors, uri } = fixture([]);
    const path = uri("src/open.ts");
    mirrors.push({ uri: URI.parse(path), version: 1, getValue: () => 'export const value: number = "wrong";' });
    expect(await service.getSyntacticDiagnostics(path)).toEqual([]);
    expect(await service.getSemanticDiagnostics(path)).toEqual(expect.arrayContaining([expect.objectContaining({ code: 2322 })]));
    await service.updateExtraLibs(indexExtras([{ path: "src/open.ts", content: 'export const value: number = "wrong";' }]));
    expect(await service.getSemanticDiagnostics(path)).toEqual(expect.arrayContaining([expect.objectContaining({ code: 2322 })]));
  });
  it("keeps diff preview globals out of the workspace and out of another diff side's native program", async () => {
    const { service, mirrors, uri } = fixture([{ path: "src/main.ts", content: "export {}; shared;" }]);
    const original = "minicode-diff://preview/A/original.ts";
    const modified = "minicode-diff://preview/A/modified.ts";
    mirrors.push({ uri: URI.parse(original), version: 1, getValue: () => "declare const shared: string; const value: number = 'wrong';" });
    mirrors.push({ uri: URI.parse(modified), version: 1, getValue: () => "declare const shared: number; const value = 1;" });
    expect(await service.getSemanticDiagnostics(original)).toEqual(expect.arrayContaining([expect.objectContaining({ code: 2322 })]));
    expect(await service.getSemanticDiagnostics(modified)).toEqual([]);
    expect(await service.getSemanticDiagnostics(uri("src/main.ts"))).toEqual(expect.arrayContaining([expect.objectContaining({ code: 2304 })]));
  });
  it("does not reuse a closed preview's old native snapshot when its URI is reopened at version one", async () => {
    const { service, mirrors, uri } = fixture([{ path: "src/main.ts", content: "export const value = 1;" }]);
    const preview = "minicode-diff://preview/A/modified.ts";
    mirrors.push({ uri: URI.parse(preview), version: 1, getValue: () => "const value: number = 'wrong';" });
    expect(await service.getSemanticDiagnostics(preview)).toEqual(expect.arrayContaining([expect.objectContaining({ code: 2322 })]));
    mirrors.length = 0;
    await service.getSemanticDiagnostics(uri("src/main.ts"));
    mirrors.push({ uri: URI.parse(preview), version: 1, getValue: () => "const value: number = 1;" });
    expect(await service.getSemanticDiagnostics(preview)).toEqual([]);
  });
  it("rejects a cross-file rename when an actual source reference belongs to a read-only buffer", async () => {
    const main = 'import { value } from "./value";\nvalue;';
    const { service, uri } = fixture([
      { path: "src/value.ts", content: "export const value = 1;" },
      { path: "src/main.ts", content: main, readOnly: true },
    ]);
    expect(await service.getDefinitionAtPosition(uri("src/main.ts"), main.lastIndexOf("value") + 2))
      .toEqual(expect.arrayContaining([expect.objectContaining({ fileName: uri("src/value.ts") })]));
    expect(await service.getRenameInfo(uri("src/value.ts"), 15, {})).toMatchObject({ canRename: false, localizedErrorMessage: expect.stringContaining("只读文件") });
    await service.updateExtraLibs(indexExtras([
      { path: "src/value.ts", content: "export const value = 1;" },
      { path: "src/main.ts", content: main },
    ]));
    expect(await service.getRenameInfo(uri("src/value.ts"), 15, {})).toMatchObject({ canRename: true });
  });
  it("resolves actual hidden project sources without changing configured project selection", async () => {
    const main = 'import { preview } from "../.storybook/preview";\nexport const value: number = preview;';
    const { service, uri } = fixture([
      { path: "tsconfig.json", content: '{"compilerOptions":{"strict":true},"include":["src"]}' },
      { path: "src/main.ts", content: main },
      { path: ".storybook/preview.ts", content: "export const preview = 1;" },
    ]);
    expect(await service.getSemanticDiagnostics(uri("src/main.ts"))).toEqual([]);
    expect(await service.getDefinitionAtPosition(uri("src/main.ts"), main.lastIndexOf("preview") + 2))
      .toEqual(expect.arrayContaining([expect.objectContaining({ fileName: uri(".storybook/preview.ts") })]));
  });

  it("uses the bundled TypeScript compiler and resolves unopened sources without making dependency declarations roots", async () => {
    expect(ts.version).toBe("5.9.3");
    const main = 'import { greet } from "./greet";\nconst message: string = greet("MiniCode");\npoisoned;';
    const helper = 'export function greet(name: string) { return "Hello " + name; }';
    const { service, uri } = fixture([
      { path: "src/main.ts", content: main }, { path: "src/greet.ts", content: helper },
      { path: "node_modules/unimported/index.d.ts", content: "declare const poisoned: true;", source: false },
    ]);
    const diagnostics = await service.getSemanticDiagnostics(uri("src/main.ts"));
    expect(diagnostics.map((diagnostic) => diagnostic.code)).toEqual([2304]);
    const definition = await service.getDefinitionAtPosition(uri("src/main.ts"), main.indexOf('greet("') + 2);
    expect(definition).toEqual(expect.arrayContaining([expect.objectContaining({ fileName: uri("src/greet.ts") })]));
    const references = await service.getReferencesAtPosition(uri("src/greet.ts"), helper.indexOf("greet") + 2);
    expect(references.map((reference) => reference.fileName)).toEqual(expect.arrayContaining([uri("src/main.ts"), uri("src/greet.ts")]));
    const rename = await service.findRenameLocations(uri("src/greet.ts"), helper.indexOf("greet") + 2, false, false, false);
    expect(rename.map((location) => location.fileName)).toEqual(expect.arrayContaining([uri("src/main.ts"), uri("src/greet.ts")]));
  });

  it("parses JSONC, relative extends, aliases, include/exclude, and inherited config directories", async () => {
    const main = 'import { greet } from "@/greet";\nconst message: string = greet();\nexcludedMarker;';
    const { service, uri } = fixture([
      { path: "configs/base.json", content: '{ // base options\n "compilerOptions": { "strict": true, "baseUrl": "..", "paths": { "@/*": ["src/*"] } }\n}' },
      { path: "tsconfig.json", content: '{"extends":"./configs/base.json","include":["src/**/*"],"exclude":["src/excluded.ts"]}' },
      { path: "src/main.ts", content: main }, { path: "src/greet.ts", content: 'export const greet = () => "hello";' },
      { path: "src/excluded.ts", content: "declare const excludedMarker: string;" },
    ]);
    expect((await service.getSemanticDiagnostics(uri("src/main.ts"))).map((diagnostic) => diagnostic.code)).toEqual([2304]);
    const definition = await service.getDefinitionAtPosition(uri("src/main.ts"), main.indexOf("greet()") + 2);
    expect(definition).toEqual(expect.arrayContaining([expect.objectContaining({ fileName: uri("src/greet.ts") })]));
    expect(await service.getCompilerOptionsDiagnostics(uri("src/main.ts"))).toEqual([]);
  });

  it("resolves tsconfig extends from node_modules and package declarations on demand", async () => {
    const main = 'import { greet } from "mini-lib";\nconst message: string = greet();';
    const { service, uri } = fixture([
      { path: "tsconfig.json", content: '{"extends":"@mini/tsconfig/base.json","include":["src"]}' },
      { path: "node_modules/@mini/tsconfig/package.json", content: '{"name":"@mini/tsconfig","version":"1.0.0"}', source: false },
      { path: "node_modules/@mini/tsconfig/base.json", content: '{"compilerOptions":{"strict":true}}', source: false },
      { path: "node_modules/mini-lib/package.json", content: '{"name":"mini-lib","types":"index.d.ts"}', source: false },
      { path: "node_modules/mini-lib/index.d.ts", content: "export declare function greet(): string;", source: false },
      { path: "src/main.ts", content: main },
    ]);
    expect(await service.getSemanticDiagnostics(uri("src/main.ts"))).toEqual([]);
    expect(await service.getCompilerOptionsDiagnostics(uri("src/main.ts"))).toEqual([]);
    expect(await service.getDefinitionAtPosition(uri("src/main.ts"), main.indexOf("greet()") + 2)).toEqual(expect.arrayContaining([
      expect.objectContaining({ fileName: uri("node_modules/mini-lib/index.d.ts") }),
    ]));
    expect(await service.getRenameInfo(uri("node_modules/mini-lib/index.d.ts"), 25, {})).toMatchObject({ canRename: false, localizedErrorMessage: expect.stringContaining("依赖声明") });
  });

  it("requests missing native configuration candidates and resolves aliases after their real content arrives", async () => {
    const files: IndexedFile[] = [
      { path: "tsconfig.json", content: '{"extends":"./configs/base.json","include":["src"]}' },
      { path: "src/main.ts", content: 'import { value } from "@/value";\nexport const result: string = value;' },
      { path: "src/value.ts", content: 'export const value = "ready";' },
    ];
    const { service, uri } = fixture(files);
    expect(await service.getConfigurationFileRequests()).toContain(uri("configs/base.json"));
    expect((await service.getCompilerOptionsDiagnostics(uri("src/main.ts"))).some((diagnostic) => diagnostic.code === 5083)).toBe(true);
    await service.updateExtraLibs(indexExtras([...files, {
      path: "configs/base.json", content: '{"compilerOptions":{"strict":true,"baseUrl":"..","paths":{"@/*":["src/*"]}}}',
    }]));
    expect(await service.getSemanticDiagnostics(uri("src/main.ts"))).toEqual([]);
    expect(await service.getCompilerOptionsDiagnostics(uri("src/main.ts"))).toEqual([]);
    expect(await service.getConfigurationFileRequests()).not.toContain(uri("configs/base.json"));
  });

  it("does not turn ordinary missing module probes into configuration hydration requests", async () => {
    const { service, uri } = fixture([
      { path: "tsconfig.json", content: '{"include":["src"]}' },
      { path: "src/main.ts", content: 'import { value } from "missing-pkg";\nvalue();' },
    ]);
    const requests = await service.getConfigurationFileRequests();
    expect((await service.getSemanticDiagnostics(uri("src/main.ts"))).some((diagnostic) => diagnostic.code === 2307)).toBe(true);
    expect(await service.getConfigurationFileRequests()).toEqual(requests);
  });

  it.each(["/project", "C:/Work/Project"])("resolves mixed TS/JS imports and case-insensitive paths in %s", async (root) => {
    const main = 'import { greet } from "./HELPERS";\nconst message: string = greet("MiniCode");';
    const { service, uri } = fixture([
      { path: "tsconfig.json", content: '{"compilerOptions":{"allowJs":true,"checkJs":true},"include":["src"]}' },
      { path: "src/main.ts", content: main }, { path: "src/helpers.js", content: 'export function greet(name) { return "Hello " + name; }' },
    ], root, false);
    expect((await service.getSemanticDiagnostics(uri("src/main.ts"))).map((diagnostic) => diagnostic.code)).toEqual([1149]);
    const definition = await service.getDefinitionAtPosition(uri("src/main.ts"), main.indexOf('greet("') + 2);
    expect(definition?.some((entry) => entry.fileName.toLowerCase() === uri("src/helpers.js").toLowerCase())).toBe(true);
  });

  it("uses each query file's nearest configured project rather than sharing one compiler configuration", async () => {
    const { service, uri } = fixture([
      { path: "tsconfig.json", content: '{"compilerOptions":{"strict":true},"include":["src","packages"]}' },
      { path: "packages/relaxed/tsconfig.json", content: '{"compilerOptions":{"strict":false},"include":["src"]}' },
      { path: "src/main.ts", content: "export const value: number = undefined;" },
      { path: "packages/relaxed/src/main.ts", content: "export const value: number = undefined;" },
    ]);
    expect((await service.getSemanticDiagnostics(uri("src/main.ts"))).some((diagnostic) => diagnostic.code === 2322)).toBe(true);
    expect(await service.getSemanticDiagnostics(uri("packages/relaxed/src/main.ts"))).toEqual([]);
  });

  it("honors configured compiler enums instead of overwriting them with editor defaults", async () => {
    const { service, uri } = fixture([
      { path: "tsconfig.json", content: '{"compilerOptions":{"target":"ES2020","module":"NodeNext","moduleResolution":"NodeNext","jsx":"react-jsx","strict":true},"include":["src"]}' },
      { path: "src/main.ts", content: "export const value = 1;" },
    ]);
    const parsed = service.parsedConfig(uri("tsconfig.json"))!;
    expect(parsed.options).toMatchObject({ target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.NodeNext, moduleResolution: ts.ModuleResolutionKind.NodeNext, jsx: ts.JsxEmit.ReactJSX, strict: true });
    expect(await service.getCompilerOptionsDiagnostics(uri("src/main.ts"))).toEqual([]);
  });

  it("uses the compiler's script kinds for modern module extensions", async () => {
    const { service, uri } = fixture([
      { path: "src/main.mts", content: "export const value: number = 1;" },
      { path: "src/Common.cts", content: "export const value: string = 'ok';" },
    ]);
    expect(await service.getSyntacticDiagnostics(uri("src/main.mts"))).toEqual([]);
    expect(await service.getSyntacticDiagnostics(uri("src/Common.cts"))).toEqual([]);
  });

  it("uses source project references for navigation and cross-project references and rename", async () => {
    const library = "export const sharedValue = 42;";
    const main = 'import { sharedValue } from "../../lib/src/value";\nexport const answer = sharedValue;';
    const { service, uri } = fixture([
      { path: "tsconfig.json", content: '{"files":[],"references":[{"path":"./packages/lib"},{"path":"./packages/app"}]}' },
      { path: "packages/lib/tsconfig.json", content: '{"compilerOptions":{"composite":true},"include":["src"]}' },
      { path: "packages/app/tsconfig.json", content: '{"compilerOptions":{"composite":true},"include":["src"],"references":[{"path":"../lib"}]}' },
      { path: "packages/lib/src/value.ts", content: library }, { path: "packages/app/src/main.ts", content: main },
    ]);
    const definitions = await service.getDefinitionAtPosition(uri("packages/app/src/main.ts"), main.lastIndexOf("sharedValue") + 2);
    expect(definitions).toEqual(expect.arrayContaining([expect.objectContaining({ fileName: uri("packages/lib/src/value.ts") })]));
    const references = await service.getReferencesAtPosition(uri("packages/lib/src/value.ts"), library.indexOf("sharedValue") + 2);
    expect(references.map((entry) => entry.fileName)).toEqual(expect.arrayContaining([uri("packages/lib/src/value.ts"), uri("packages/app/src/main.ts")]));
    const rename = await service.findRenameLocations(uri("packages/lib/src/value.ts"), library.indexOf("sharedValue") + 2, false, false, false);
    expect(rename.map((entry) => entry.fileName)).toEqual(expect.arrayContaining([uri("packages/lib/src/value.ts"), uri("packages/app/src/main.ts")]));
  });

  it("includes inferred JavaScript consumers when references and rename begin in a configured project", async () => {
    const helper = "export function greet(name: string) { return name; }\ninferredOnly;";
    const { service, uri } = fixture([
      { path: "pkg/tsconfig.json", content: '{"compilerOptions":{"strict":true},"include":["src"]}' },
      { path: "pkg/src/foo.ts", content: helper },
      { path: "pkg/src/use.ts", content: 'import { greet } from "./foo";\nexport const result = greet("configured");' },
      { path: "tools/main.js", content: 'import { greet } from "../pkg/src/foo";\nexport const result = greet("inferred");' },
      { path: "tools/globals.d.ts", content: "declare const inferredOnly: string;" },
    ]);
    const helperUri = uri("pkg/src/foo.ts");
    const position = helper.indexOf("greet") + 2;
    expect((await service.getSemanticDiagnostics(helperUri)).map((diagnostic) => diagnostic.code)).toEqual([2304]);
    const references = await service.getReferencesAtPosition(helperUri, position);
    expect(references.map((entry) => entry.fileName)).toEqual(expect.arrayContaining([helperUri, uri("pkg/src/use.ts"), uri("tools/main.js")]));
    expect(new Set(references.map((entry) => `${entry.fileName}:${entry.textSpan.start}:${entry.textSpan.length}`)).size).toBe(references.length);
    const rename = await service.findRenameLocations(helperUri, position, false, false, false);
    expect(rename.map((entry) => entry.fileName)).toEqual(expect.arrayContaining([helperUri, uri("pkg/src/use.ts"), uri("tools/main.js")]));
    expect(new Set(rename.map((entry) => `${entry.fileName}:${entry.textSpan.start}:${entry.textSpan.length}`)).size).toBe(rename.length);
    expect((await service.getSemanticDiagnostics(helperUri)).map((diagnostic) => diagnostic.code)).toEqual([2304]);
  });

  it("prefers current dirty buffers to disk and updates diagnostics as mirror versions change", async () => {
    const { service, uri, mirrors } = fixture([
      { path: "src/main.ts", content: 'import { greet } from "./greet";\nconst message: string = greet();' },
      { path: "src/greet.ts", content: 'export const greet = () => "hello";' },
    ]);
    expect(await service.getSemanticDiagnostics(uri("src/main.ts"))).toEqual([]);
    let dirty = "export const greet = () => 42;";
    const model = { uri: URI.parse(uri("src/greet.ts")), version: 2, getValue: () => dirty };
    mirrors.push(model);
    expect((await service.getSemanticDiagnostics(uri("src/main.ts"))).some((diagnostic) => diagnostic.code === 2322)).toBe(true);
    dirty = 'export const greet = () => "edited";';
    model.version++;
    expect(await service.getSemanticDiagnostics(uri("src/main.ts"))).toEqual([]);
  });

  it("replaces deleted files and configuration instead of retaining the previous snapshot or workspace", async () => {
    const files = [
      { path: "src/main.ts", content: 'import { greet } from "./greet"; greet();' },
      { path: "src/greet.ts", content: 'export const greet = () => "hello";' },
    ];
    const { service, uri } = fixture(files);
    expect(await service.getSemanticDiagnostics(uri("src/main.ts"))).toEqual([]);
    await service.updateExtraLibs(indexExtras(files.slice(0, 1)));
    expect((await service.getSemanticDiagnostics(uri("src/main.ts"))).some((diagnostic) => diagnostic.code === 2307)).toBe(true);
    const nextRoot = "/another-project";
    await service.updateExtraLibs(indexExtras([{ path: "src/main.ts", content: "export const value = 1;" }], nextRoot));
    expect(await service.getSemanticDiagnostics(modelUri("src/main.ts", nextRoot))).toEqual([]);
    expect(await service.getScriptText(uri("src/greet.ts"))).toBeUndefined();
  });
});
