/* @vitest-environment jsdom */
import { useEffect } from "react";
import { render } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import * as monaco from "monaco-editor/editor/editor.api.js";
import { useOwnedDiffModelCleanup } from "./useOwnedDiffModelCleanup";

vi.hoisted(() => Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));

it("detaches the owned real models before a vendor child's passive model disposal", () => {
  const original = monaco.editor.createModel("before", "plaintext");
  const modified = monaco.editor.createModel("after", "plaintext");
  let attached: monaco.editor.IDiffEditorModel | null = { original, modified };
  const order: string[] = [];
  original.onWillDispose(() => { expect(attached).toBeNull(); order.push("original"); });
  modified.onWillDispose(() => { expect(attached).toBeNull(); order.push("modified"); });
  const editor = { getModel: () => attached, setModel: (model: monaco.editor.IDiffEditorModel | null) => { attached = model; order.push("detach"); } } as monaco.editor.IStandaloneDiffEditor;
  function Vendor({ onMount }: { onMount: (editor: monaco.editor.IStandaloneDiffEditor) => void }) {
    useEffect(() => { onMount(editor); return () => { order.push("vendor-passive"); original.dispose(); modified.dispose(); }; }, []);
    return null;
  }
  function Surface() { const onMount = useOwnedDiffModelCleanup(); return <Vendor onMount={onMount} />; }
  const mounted = render(<Surface />);
  mounted.unmount();
  expect(order).toEqual(["detach", "original", "modified", "vendor-passive"]);
});
