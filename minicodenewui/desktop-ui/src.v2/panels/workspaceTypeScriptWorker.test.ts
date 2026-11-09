import { afterAll, expect, it, vi } from "vitest";
import { typescript as ts } from "monaco-editor/languages/features/typescript/lib/typescriptServices.js";
import { WORKSPACE_TYPESCRIPT_METADATA_URI } from "./workspaceTypeScriptContract";

vi.hoisted(() => {
  vi.stubGlobal("self", globalThis);
  vi.stubGlobal("onmessage", undefined);
  vi.stubGlobal("postMessage", vi.fn());
  vi.stubGlobal("importScripts", vi.fn());
});
import "./workspaceTypeScriptWorker";

afterAll(() => vi.unstubAllGlobals());

it("boots the real Monaco worker before syncing mirrors and serves indexed aliases and unsaved source", async () => {
  const root = "file:///project";
  const main = `${root}/src/main.ts`;
  const helper = `${root}/src/value.ts`;
  const mainSource = 'import { value } from "@/value";\nconst label: string = value;';
  const extraLibs = {
    [`${root}/tsconfig.json`]: { version: 1, content: '{"compilerOptions":{"strict":true,"baseUrl":".","paths":{"@/*":["src/*"]}},"include":["src/**/*"]}' },
    [main]: { version: 1, content: mainSource },
    [helper]: { version: 1, content: "export const value = 1;" },
    [WORKSPACE_TYPESCRIPT_METADATA_URI]: { version: 1, content: JSON.stringify({ workspaceRoot: root, caseSensitive: true, sourceFileNames: [main, helper], readOnlyFileNames: [] }) },
  };
  const scope = globalThis as unknown as { onmessage(event: { data: unknown }): void };
  const pending = new Map<string, { resolve(value: unknown): void; reject(error: unknown): void }>();
  vi.mocked(postMessage).mockImplementation((message: { seq: string; res?: unknown; err?: unknown }) => {
    const request = pending.get(message.seq)!;
    pending.delete(message.seq);
    if (message.err) request.reject(message.err);
    else request.resolve(message.res);
  });
  let sequence = 0;
  const request = (method: string, args: unknown[]): Promise<unknown> => new Promise((resolve, reject) => {
    const req = String(++sequence);
    pending.set(req, { resolve, reject });
    scope.onmessage({ data: { type: 0, vsWorker: 1, req, channel: "default", method, args } });
  });
  const call = (method: string, args: unknown[]) => request("$fmr", [method, args]);

  // The two real bootstrap messages precede all model synchronization. Calling
  // getMirrorModels inside the service constructor hits Monaco's unassigned server.
  scope.onmessage({ data: "ignore" });
  scope.onmessage({ data: {
    compilerOptions: { target: ts.ScriptTarget.ESNext, module: ts.ModuleKind.ESNext, moduleResolution: ts.ModuleResolutionKind.NodeJs },
    extraLibs,
  } });
  await request("$initialize", [1]);
  expect(await call("getConfigurationDiagnostics", [])).toEqual([]);
  expect(await call("getSemanticDiagnostics", [main])).toEqual(expect.arrayContaining([expect.objectContaining({ code: 2322 })]));
  expect(await call("getDefinitionAtPosition", [main, mainSource.lastIndexOf("value") + 2]))
    .toEqual(expect.arrayContaining([expect.objectContaining({ fileName: helper })]));

  await request("$acceptNewModel", [{ url: helper, lines: ['export const value = "draft";'], EOL: "\n", versionId: 2 }]);
  expect(await call("getSemanticDiagnostics", [main])).toEqual([]);
  await call("updateExtraLibs", [extraLibs]);
  expect(await call("getSemanticDiagnostics", [main])).toEqual([]);
});
