import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type * as Monaco from "monaco-editor/editor/editor.api.js";
import { Braces, Search, X } from "lucide-react";
import { useAppStore } from "../stores";
import { workspaceFilePathsEqual, workspacePathWithin, workspacePathsEqual } from "../lib/workspace-path";
import { isDependencyIndexPath } from "./workspaceModelIndex";
import { documentSymbols } from "./editorNativeServices";
import { useFocusTrap } from "../hooks/useFocusTrap";

interface SymbolRow { name: string; detail: string; path: string; line: number; column: number; depth: number }
export function EditorSymbols({ monaco, workspaceRoot, path, project, sourceFiles, onClose }: {
  monaco: typeof Monaco; workspaceRoot: string; path: string; project: boolean; onClose: () => void;
  sourceFiles: () => Array<{ uri: Monaco.Uri; content: string }>;
}) {
  const [query, setQuery] = useState("");
  const [rows, setRows] = useState<SymbolRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(0);
  const dialog = useFocusTrap(true);
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const cancellation = new monaco.CancellationTokenSource();
    const load = async () => {
      const models = monaco.editor.getModels().filter((model) => model.uri.scheme === "file" && workspacePathWithin(model.uri.fsPath, workspaceRoot)
        && !isDependencyIndexPath(model.uri.fsPath) && (project || workspacePathsEqual(model.uri.fsPath, monaco.Uri.parse(path).fsPath)));
      const files = new Map((project ? sourceFiles() : []).map((file) => [file.uri.toString(), file]));
      for (const model of models) files.set(model.uri.toString(), { uri: model.uri, content: model.getValue(undefined, true) });
      const result: SymbolRow[] = [];
      for (const file of files.values()) {
        if (cancellation.token.isCancellationRequested) break;
        const existing = monaco.editor.getModel(file.uri);
        const model = existing ?? monaco.editor.createModel(file.content,
          /\.[cm]?tsx?$/i.test(file.uri.path) ? "typescript" : "javascript", file.uri);
        const flatten = (symbols: Monaco.languages.DocumentSymbol[], depth = 0): SymbolRow[] => symbols.flatMap((symbol) => [
          { name: symbol.name, detail: symbol.detail, path: model.uri.fsPath, line: symbol.selectionRange.startLineNumber, column: symbol.selectionRange.startColumn, depth },
          ...flatten(symbol.children ?? [], depth + 1),
        ]);
        try { result.push(...flatten(await documentSymbols(model, cancellation.token))); }
        finally {
          const open = useAppStore.getState().editorTabs.some((tab) => workspaceFilePathsEqual(tab.path, file.uri.fsPath, workspaceRoot));
          if (!existing && !open && model.getValue(undefined, true) === file.content) model.dispose();
        }
      }
      if (!cancellation.token.isCancellationRequested) setRows(result);
    };
    void load().catch((reason) => { if (!cancellation.token.isCancellationRequested) setError(String(reason)); })
      .finally(() => { if (!cancellation.token.isCancellationRequested) setLoading(false); });
    return () => cancellation.dispose(true);
  }, [monaco, workspaceRoot, path, project, sourceFiles]);
  const matches = rows.filter((row) => (row.name + " " + (project ? row.path : "")).toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const open = (row: SymbolRow) => { useAppStore.getState().openEditorFile(row.path, undefined, { line: row.line, column: row.column, exact: true }); onClose(); };
  useEffect(() => { list.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" }); }, [selected]);
  return createPortal(<div className="mc-symbol-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section ref={dialog} className="mc-symbol-picker" role="dialog" aria-modal="true" aria-label={project ? "项目符号" : "文件大纲"} onKeyDown={(event) => {
      if (event.key === "Escape") { event.stopPropagation(); onClose(); }
      else if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); setSelected((value) => Math.max(0, Math.min(matches.length - 1, value + (event.key === "ArrowDown" ? 1 : -1)))); }
      else if (event.key === "Enter" && matches[selected]) { event.preventDefault(); open(matches[selected]); }
    }}>
      <div className="mc-symbol-search"><Search size={16} /><input autoFocus value={query} onChange={(event) => { setQuery(event.target.value); setSelected(0); }} aria-label="搜索符号" placeholder={project ? "搜索项目符号…" : "跳转到符号…"} /><button type="button" aria-label="关闭大纲" onClick={onClose}><X size={16} /></button></div>
      <div className="mc-symbol-results" ref={list} role="listbox" aria-label="符号">
        {matches.map((row, index) => <button type="button" role="option" aria-selected={index === selected} key={row.path + ":" + row.line + ":" + index} onClick={() => open(row)} style={{ paddingLeft: 14 + (project ? 0 : row.depth * 14) }}>
          <Braces size={14} /><span><strong>{row.name}</strong><small>{project ? row.path.slice(workspaceRoot.length + 1) : row.detail}</small></span><small>{row.line}</small></button>)}
        {loading && <p role="status">读取语言服务符号…</p>}
        {error && <p role="alert">{error}</p>}
        {!loading && !error && !matches.length && <p>没有匹配的符号。</p>}
      </div>
      <footer>{project ? "已索引源码与打开的文档" : "当前文件"}<span>↑↓ 选择 · Enter 跳转 · Esc 关闭</span></footer>
    </section>
  </div>, document.body);
}
