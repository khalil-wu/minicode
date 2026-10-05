import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { LoaderCircle, Sparkles, X } from "lucide-react";
import type * as Monaco from "monaco-editor/editor/editor.api.js";
import { useAppStore } from "../stores";
import { generateEditorCode } from "./editorInlineCompletion";
import { useOwnedDiffModelCleanup } from "../components/useOwnedDiffModelCleanup";
import { miniCodeMonacoThemeName } from "./monacoTheme";

const Diff = lazy(() => import("@monaco-editor/react").then((module) => ({ default: module.DiffEditor })));

export function EditorInlineEdit({ editor, workspaceRoot, onClose }: { editor: Monaco.editor.IStandaloneCodeEditor; workspaceRoot: string; onClose: () => void }) {
  const [snapshot] = useState(() => {
    const model = editor.getModel()!;
    const range = editor.getSelection()!;
    const text = model.getValue();
    const start = model.getOffsetAt(range.getStartPosition());
    const end = model.getOffsetAt(range.getEndPosition());
    return { model, range, version: model.getVersionId(), original: model.getValueInRange(range), prefix: text.slice(0, start), suffix: text.slice(end) };
  });
  const [instruction, setInstruction] = useState("");
  const [replacement, setReplacement] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const theme = useAppStore((state) => state.resolvedTheme);
  const scale = useAppStore((state) => state.codeTextScale);
  const preferences = useAppStore((state) => state.workbenchPreferences);
  const onDiffMount = useOwnedDiffModelCleanup();
  useEffect(() => () => controller.current?.abort(), []);
  const generate = async () => {
    controller.current?.abort();
    const current = controller.current = new AbortController();
    setBusy(true); setError("");
    try {
      const result = await generateEditorCode(workspaceRoot, { path: snapshot.model.uri.fsPath, prefix: snapshot.prefix, suffix: snapshot.suffix, selected: snapshot.original, instruction, mode: "edit" }, current.signal);
      if (!current.signal.aborted) setReplacement(result.text);
    } catch (reason) { if (!current.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { if (!current.signal.aborted) setBusy(false); }
  };
  const apply = () => {
    if (snapshot.model.isDisposed() || snapshot.model.getVersionId() !== snapshot.version || editor.getRawOptions().readOnly || useAppStore.getState().workingDirectory !== workspaceRoot) {
      setError("原文件已变化，请关闭后重新选择要修改的代码。"); return;
    }
    snapshot.model.pushStackElement();
    snapshot.model.pushEditOperations([], [{ range: snapshot.range, text: replacement! }], () => null);
    snapshot.model.pushStackElement();
    onClose(); editor.focus();
  };
  return <section className="mc-inline-edit" role="dialog" aria-label="修改选中代码" onKeyDown={(event) => { event.stopPropagation(); if (event.key === "Escape") onClose(); }}>
    <header><Sparkles size={14} /><strong>修改选中代码</strong><span>{snapshot.range.startLineNumber}–{snapshot.range.endLineNumber} 行</span><button type="button" aria-label="关闭小改" onClick={onClose}><X size={15} /></button></header>
    <form onSubmit={(event) => { event.preventDefault(); void generate(); }}><input autoFocus aria-label="修改要求" placeholder="描述这段代码需要怎样修改…" value={instruction} onChange={(event) => setInstruction(event.target.value)} disabled={busy} />
      {busy ? <button type="button" onClick={() => { controller.current?.abort(); setBusy(false); }}>取消</button> : <button type="submit" disabled={!instruction.trim()}>{replacement == null ? "生成" : "重新生成"}</button>}</form>
    {busy && <p role="status"><LoaderCircle size={14} />正在生成修改…</p>}
    {error && <p role="alert">{error}</p>}
    {replacement != null && <><Suspense fallback={<p>加载差异…</p>}><Diff height={220} original={snapshot.original} modified={replacement} language={snapshot.model.getLanguageId()}
      theme={miniCodeMonacoThemeName(theme)} onMount={onDiffMount} options={{ readOnly: true, originalEditable: false, renderSideBySide: false, automaticLayout: true, minimap: { enabled: false },
        fontFamily: preferences.codeFont || "var(--editor-font-family)", fontSize: Math.round(14 * scale), scrollBeyondLastLine: false, wordWrap: "on" }} /></Suspense>
      <footer><span>应用后可用 Ctrl+Z 撤销</span><button type="button" onClick={onClose}>放弃</button><button type="button" onClick={apply} disabled={busy}>应用修改</button></footer></>}
  </section>;
}
