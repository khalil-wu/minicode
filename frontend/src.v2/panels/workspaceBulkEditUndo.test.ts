/* @vitest-environment jsdom */
import { afterEach, describe, expect, it, vi } from "vitest";
import * as monaco from "monaco-editor/editor/editor.api.js";
import { IBulkEditService, ResourceFileEdit, type BulkEditService } from "monaco-editor/editor/browser/services/bulkEditService.js";
import { StandaloneServices } from "monaco-editor/editor/standalone/browser/standaloneServices.js";
import { installWorkspaceBulkEditUndo } from "./workspaceBulkEditUndo";

vi.hoisted(() => Object.defineProperty(window, "matchMedia", {
  configurable: true,
  value: () => ({ matches: false, media: "", addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
}));

const registrations: monaco.IDisposable[] = [];
afterEach(() => {
  for (const registration of registrations.splice(0).reverse()) registration.dispose();
  for (const model of monaco.editor.getModels()) model.dispose();
});

function type(model: monaco.editor.ITextModel, text: string) {
  model.pushStackElement();
  model.pushEditOperations([], [{ range: model.getFullModelRange(), text }], () => []);
  model.pushStackElement();
}

function renameEdits(model: monaco.editor.ITextModel): monaco.languages.IWorkspaceTextEdit[] {
  return [...model.getValue().matchAll(/\bvalue\b/g)].map((match) => {
    const start = model.getPositionAt(match.index!);
    const end = model.getPositionAt(match.index! + match[0].length);
    return {
      resource: model.uri, versionId: model.getVersionId(),
      textEdit: { range: new monaco.Range(start.lineNumber, start.column, end.lineNumber, end.column), text: "renamed" },
    };
  });
}

function fixture() {
  const a = monaco.editor.createModel("export const value = 1;", "typescript", monaco.Uri.parse("file:///workspace/a.ts"));
  const b = monaco.editor.createModel('import { value } from "./a";\nvalue;', "typescript", monaco.Uri.parse("file:///workspace/b.ts"));
  const original = [a.getValue(), b.getValue()];
  type(a, `// earlier input A\n${original[0]}`);
  type(b, `// earlier input B\n${original[1]}`);
  const typed = [a.getValue(), b.getValue()];
  const renamed = typed.map((text) => text.replace(/\bvalue\b/g, "renamed"));
  const service = StandaloneServices.get<BulkEditService>(IBulkEditService);
  const registration = installWorkspaceBulkEditUndo(monaco, (uri) => uri.scheme === "file" && uri.path.startsWith("/workspace/"));
  registrations.push(registration);
  return { a, b, original, typed, renamed, service, registration };
}

describe("workspace bulk-edit Undo/Redo", () => {
  it.each([0, 1])("undoes and redoes a real two-file rename from file %s, preserving earlier input and adopt notifications", async (index) => {
    const { a, b, original, typed, renamed, service } = fixture();
    const models = [a, b];
    const events = models.map(() => vi.fn());
    const adopted = new Map<string, string>();
    models.forEach((model, modelIndex) => registrations.push(model.onDidChangeContent((event) => {
      events[modelIndex](event);
      adopted.set(model.uri.toString(), model.getValue());
    })));
    const originalMethods = models.map((model) => model.pushEditOperations);
    const originalDescriptors = models.map((model) => Object.getOwnPropertyDescriptor(model, "pushEditOperations"));
    const typedVersionIds = models.map((model) => model.getAlternativeVersionId());

    const applying = service.apply({ edits: [...renameEdits(a), ...renameEdits(b)] }, { label: "Rename value" });
    models.forEach((model, modelIndex) => {
      expect(model.pushEditOperations).toBe(originalMethods[modelIndex]);
      expect(Object.getOwnPropertyDescriptor(model, "pushEditOperations")).toEqual(originalDescriptors[modelIndex]);
    });
    expect(await applying).toMatchObject({ isApplied: true });
    expect(models.map((model) => model.getValue())).toEqual(renamed);
    expect([...adopted.values()]).toEqual(renamed);

    await models[index].undo();
    expect(models.map((model) => model.getValue())).toEqual(typed);
    expect(models.map((model) => model.getAlternativeVersionId())).toEqual(typedVersionIds);
    models.forEach((model) => expect(adopted.get(model.uri.toString())).toBe(model.getValue()));
    expect(events[0]).toHaveBeenLastCalledWith(expect.objectContaining({ isUndoing: true }));
    expect(events[1]).toHaveBeenLastCalledWith(expect.objectContaining({ isUndoing: true }));

    await models[1 - index].redo();
    expect(models.map((model) => model.getValue())).toEqual(renamed);
    expect(events[0]).toHaveBeenLastCalledWith(expect.objectContaining({ isRedoing: true }));
    expect(events[1]).toHaveBeenLastCalledWith(expect.objectContaining({ isRedoing: true }));

    await models[index].undo();
    await models[index].undo();
    expect(models[index].getValue()).toBe(original[index]);
    expect(models[1 - index].getValue()).toBe(typed[1 - index]);
    await models[index].redo();
    expect(models.map((model) => model.getValue())).toEqual(typed);
    await models[1 - index].redo();
    expect(models.map((model) => model.getValue())).toEqual(renamed);
  });

  it("leaves native default behavior intact when an edit belongs to another workspace", async () => {
    const { a, b, typed, service } = fixture();
    const outside = monaco.editor.createModel("export const value = 2;", "typescript", monaco.Uri.parse("file:///other/c.ts"));
    await service.apply({ edits: [...renameEdits(a), ...renameEdits(b), ...renameEdits(outside)] });
    await a.undo();
    expect(a.getValue()).toBe(typed[0]);
    expect(b.getValue()).toBe(typed[1].replace(/\bvalue\b/g, "renamed"));
    expect(outside.getValue()).toBe("export const renamed = 2;");
  });

  it("does not group a single-file bulk edit with another file's input history", async () => {
    const { a, b, original, typed, service } = fixture();
    await service.apply({ edits: renameEdits(a) });
    await a.undo();
    expect([a.getValue(), b.getValue()]).toEqual(typed);
    await a.undo();
    expect(a.getValue()).toBe(original[0]);
    expect(b.getValue()).toBe(typed[1]);
  });

  it("retains native version validation and restores model methods before a rejected Promise is observed", async () => {
    const { a, b, typed, service } = fixture();
    const methods = [a.pushEditOperations, b.pushEditOperations];
    const edits = [...renameEdits(a), ...renameEdits(b)];
    edits[edits.length - 1].versionId = b.getVersionId() - 1;
    const applying = service.apply({ edits });
    expect(a.pushEditOperations).toBe(methods[0]);
    expect(b.pushEditOperations).toBe(methods[1]);
    await expect(applying).rejects.toThrow("model changed in the meantime");
    expect([a.getValue(), b.getValue()]).toEqual(typed);
  });

  it("keeps native non-text edit rejection and restores the original service when disposed", async () => {
    const service = StandaloneServices.get<BulkEditService>(IBulkEditService);
    const originalApply = service.apply;
    const { a, b, typed, registration } = fixture();
    expect(service.apply).not.toBe(originalApply);
    await expect(service.apply([new ResourceFileEdit(a.uri, b.uri)])).rejects.toThrow("only text edits are supported");
    expect([a.getValue(), b.getValue()]).toEqual(typed);
    registration.dispose();
    expect(service.apply).toBe(originalApply);
    // Disposal restores ordinary per-file history for subsequent native edits.
    await service.apply({ edits: [...renameEdits(a), ...renameEdits(b)] });
    await a.undo();
    expect(a.getValue()).toBe(typed[0]);
    expect(b.getValue()).toBe(typed[1].replace(/\bvalue\b/g, "renamed"));
  });
});
