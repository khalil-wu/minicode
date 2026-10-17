/* @vitest-environment jsdom */
import { expect, it, vi } from "vitest";
import * as monaco from "./monacoRuntime";
import "monaco-editor/languages/definitions/typescript/register.js";

vi.hoisted(() => {
  Object.defineProperty(document, "queryCommandSupported", { configurable: true, value: () => false });
  vi.stubGlobal("CSS", { escape: (value: string) => value });
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: () => ({ matches: false, media: "", addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
  });
  HTMLCanvasElement.prototype.getContext = (() => ({
    font: "", measureText: (text: string) => ({ width: text.length * 8 }),
    clearRect() {}, fillRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {},
  })) as unknown as typeof HTMLCanvasElement.prototype.getContext;
  Range.prototype.getBoundingClientRect = () => new DOMRect();
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
});

// Keep Vite's cold module transformation in suite loading, while preserving
// production order: the actual preview initializes services before TS mode.
monaco.editor.setTheme("vs-dark");
const colorized = await monaco.editor.colorize("const proof = 1;", "typescript", {});
const { EditorExtensionsRegistry } = await import("monaco-editor/editor/browser/editorExtensions.js");
const dropRegisteredBeforeEditor = EditorExtensionsRegistry.getEditorContributions().some(entry => entry.id === "editor.contrib.dropIntoEditorController");
// Monaco 0.56's real TS worker manager imports internal/common/workers.js,
// which registers additional contributions after the preview was rendered.
await import("monaco-editor/languages/features/typescript/tsMode.js");

it("opens the real editor after a diff has already initialized Monaco colorization", async () => {
  expect(colorized).toContain("proof");
  expect(dropRegisteredBeforeEditor).toBe(false);
  const host = document.createElement("div");
  document.body.append(host);
  const model = monaco.editor.createModel("const proof = 1;", "plaintext");
  const editor = monaco.editor.create(host, { model, dimension: { width: 800, height: 420 }, minimap: { enabled: false } });
  try {
    for (const id of ["editor.contrib.dropIntoEditorController", "editor.contrib.copyPasteActionController", "css.editor.codeLens", "editor.contrib.InlayHints"])
      expect(editor.getContribution(id), id).not.toBeNull();
    editor.setSelection(new monaco.Range(1, 15, 1, 16));
    editor.trigger("keyboard", "type", { text: "2" });
    expect(model.getValue()).toBe("const proof = 2;");
    await model.undo();
    expect(model.getValue()).toBe("const proof = 1;");
  } finally {
    editor.dispose();
    model.dispose();
    host.remove();
  }
});
