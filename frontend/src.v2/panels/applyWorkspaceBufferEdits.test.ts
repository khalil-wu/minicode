/* @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as monaco from "monaco-editor/editor/editor.api.js";
import { useAppStore } from "../stores";
import { applyOffsetEdits, applyWorkspaceBufferEdits } from "./applyWorkspaceBufferEdits";
import { editorModelUri } from "./monacoLanguageServices";
import { selectedReplacementFiles, splitSearchGlobs } from "../protocol/workspace-search";

vi.hoisted(() => Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
const root = "/search-workspace";
beforeEach(() => useAppStore.setState({ workingDirectory: root, appMode: "cowork", editorTabs: [], activeTabPath: null, activeEditorPath: null,
  editorOpenRequests: [], panelSlots: [{ id: "chat", kind: "chat", focused: true }], workspaceSearchOpen: false }));
afterEach(() => { for (const model of monaco.editor.getModels()) model.dispose(); });

describe("project replacements in real editor buffers", () => {
  it("applies unopened TS, JSON and Python changes as one native Undo group without writing files", async () => {
    useAppStore.getState().openWorkspaceSearch();
    expect(useAppStore.getState().appMode).toBe("code");
    const before = "😀 foo\r\nfoo\r\n";
    await applyWorkspaceBufferEdits(root, ["main.ts", "config.json", "main.py"].map((path) => ({ path, before, original: before, contentHash: "disk-hash", edits: [{ offset: 3, length: 3, text: "bar" }] })), "selected replacements");
    expect(useAppStore.getState().editorTabs).toHaveLength(3);
    expect(useAppStore.getState().editorTabs.every((tab) => tab.content === "😀 bar\r\nfoo\r\n" && tab.original === before)).toBe(true);
    const model = monaco.editor.getModel(monaco.Uri.parse(editorModelUri("config.json", root)))!;
    await model.undo();
    expect(useAppStore.getState().editorTabs.every((tab) => tab.content === before)).toBe(true);
    await model.redo();
    expect(useAppStore.getState().editorTabs.every((tab) => tab.content.startsWith("😀 bar"))).toBe(true);
  });
  it("rejects stale previews before any buffer is modified and queues unopened Markdown native-history transactions", async () => {
    const before = "# foo\r\n";
    const store = useAppStore.getState();
    store.openEditorTab("active.md"); store.markTabLoaded("active.md", "new user text", null, "hash");
    await expect(applyWorkspaceBufferEdits(root, [
      { path: "unopened.json", before: "foo", edits: [{ offset: 0, length: 3, text: "bar" }] },
      { path: "active.md", before, edits: [{ offset: 2, length: 3, text: "bar" }] },
    ], "replace")).rejects.toThrow("已改变");
    expect(monaco.editor.getModel(monaco.Uri.parse(editorModelUri("unopened.json", root)))).toBeNull();
    await applyWorkspaceBufferEdits(root, [{ path: "notes.md", before, contentHash: "hash", edits: [{ offset: 2, length: 3, text: "bar" }] }], "replace Markdown");
    const notes = useAppStore.getState().editorTabs.find((tab) => tab.path === "notes.md")!;
    expect(notes.content).toBe("# bar\r\n");
    expect(notes.pendingBufferTransactions?.[0]).toMatchObject({ before, after: "# bar\r\n" });
    expect(monaco.editor.getModel(monaco.Uri.parse(editorModelUri("notes.md", root)))).toBeNull();
  });
  it("previews only selected UTF-16 matches and preserves regex groups, literal dollars and CRLF", () => {
    const source = "😀 foo\r\nfoo\r\n";
    const matches = [3, 8].map((offset, index) => ({ id: `m${index}`, offset, length: 3, line: index + 1, column: index ? 1 : 4, end_line: index + 1, end_column: index ? 4 : 7,
      text: "foo", snippet: "foo", groups: ["foo"], named_groups: {} }));
    const selected = selectedReplacementFiles([{ path: "main.ts", content: source, original: source, content_hash: "hash", size_bytes: 18, read_only: false, from_buffer: true, matches }], new Set(["m0"]), "$1!\\n$$", true);
    expect(selected[0].after).toBe("😀 foo!\r\n$\r\nfoo\r\n");
    expect(selected[0].edits).toHaveLength(1);
    expect(splitSearchGlobs("src/**/*.{ts,tsx}, !generated/**")).toEqual(["src/**/*.{ts,tsx}", "!generated/**"]);
    expect(applyOffsetEdits("foo", [{ offset: 0, length: 0, text: "prefix " }])).toBe("prefix foo");
  });
});
