import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowLeft, Search, X } from "lucide-react";
import { useAppStore } from "../stores";
import { editorWorkspaceKey, normalizeEditorPath } from "../stores/shared-helpers";
import { normalizeWorkspacePath, workspacePathWithin, workspaceRootsEqual } from "../lib/workspace-path";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { searchWorkspaceText, selectedReplacementFiles, type WorkspaceSearchOptions, type WorkspaceSearchResult } from "../protocol/workspace-search";
import { applyWorkspaceBufferEdits } from "./applyWorkspaceBufferEdits";
import { configureMiniCodeMonacoWorkers } from "./monacoLanguageServices";
import { loadMiniCodeEditorFeatures } from "./monacoEditorFeatures";
import { defineMiniCodeMonacoTheme, miniCodeMonacoThemeName } from "./monacoTheme";
import { guessLanguageFromPath } from "../lib/monaco-colorize";
import { useOwnedDiffModelCleanup } from "../components/useOwnedDiffModelCleanup";
import "./WorkspaceSearchPanel.css";

const NativeDiff = lazy(async () => {
  configureMiniCodeMonacoWorkers();
  const [reactMonaco, monaco] = await Promise.all([import("@monaco-editor/react"), import("monaco-editor/editor/editor.api.js"), loadMiniCodeEditorFeatures(),
    import("monaco-editor/languages/definitions/typescript/register.js"), import("monaco-editor/languages/definitions/javascript/register.js"),
    import("monaco-editor/languages/definitions/css/register.js"), import("monaco-editor/languages/definitions/scss/register.js"), import("monaco-editor/languages/definitions/less/register.js"),
    import("monaco-editor/languages/definitions/html/register.js"), import("monaco-editor/languages/definitions/python/register.js"),
    import("monaco-editor/languages/definitions/yaml/register.js"), import("monaco-editor/languages/definitions/markdown/register.js"),
    import("monaco-editor/languages/features/json/register.js"),
  ]);
  reactMonaco.loader.config({ monaco });
  return { default: reactMonaco.DiffEditor };
});
type SearchSession = { options: WorkspaceSearchOptions; replacement: string; result: WorkspaceSearchResult | null; selected: Set<string> };
const sessions = new Map<string, SearchSession>();
const initialSession = (): SearchSession => ({ options: { query: "", regex: false, caseSensitive: false, wholeWord: false, include: "", exclude: "" }, replacement: "", result: null, selected: new Set() });

function ReplacementDiff({ file, theme, scale }: { file: { path: string; before: string; after: string }; theme: "light" | "dark"; scale: number }) {
  const onMount = useOwnedDiffModelCleanup();
  const preferences = useAppStore((state) => state.workbenchPreferences);
  return <NativeDiff height="100%" original={file.before} modified={file.after} language={guessLanguageFromPath(file.path)} theme={miniCodeMonacoThemeName(theme)}
    beforeMount={(monaco) => defineMiniCodeMonacoTheme(monaco, theme)} onMount={onMount}
    options={{ readOnly: true, renderSideBySide: true, automaticLayout: true, minimap: { enabled: false }, scrollBeyondLastLine: false,
      fontFamily: preferences.codeFont || getComputedStyle(document.documentElement).getPropertyValue("--editor-font-family").trim(), fontSize: Math.round(14 * scale), lineHeight: Math.round(22 * scale),
      fontLigatures: preferences.ligatures, roundedSelection: false, renderLineHighlight: "all", renderValidationDecorations: "off", originalEditable: false, lineNumbers: "on", wordWrap: "on", padding: { top: 8 } }} />;
}

export function WorkspaceSearchPanel({ onNavigate }: { onNavigate?: () => void }) {
  const workspace = useAppStore((state) => state.workingDirectory);
  return <SearchWorkspace key={editorWorkspaceKey(workspace)} workspace={workspace} onNavigate={onNavigate} />;
}

function SearchWorkspace({ workspace, onNavigate }: { workspace: string; onNavigate?: () => void }) {
  const key = editorWorkspaceKey(workspace);
  const [session, setSession] = useState(() => sessions.get(key) ?? initialSession());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState(false);
  const [previewPath, setPreviewPath] = useState("");
  const [applied, setApplied] = useState(false);
  const queryInput = useRef<HTMLInputElement>(null);
  const request = useRef<AbortController | null>(null);
  const dialog = useFocusTrap(preview);
  const theme = useAppStore((state) => state.resolvedTheme);
  const codeTextScale = useAppStore((state) => state.codeTextScale);
  useEffect(() => { queryInput.current?.focus(); return () => request.current?.abort(); }, []);
  const update = (changes: Partial<SearchSession>) => setSession((current) => { const next = { ...current, ...changes }; sessions.set(key, next); return next; });
  const option = (changes: Partial<WorkspaceSearchOptions>) => { update({ options: { ...session.options, ...changes }, result: null, selected: new Set() }); setApplied(false); setError(""); };
  const files = session.result?.files ?? [];
  const replacements = selectedReplacementFiles(files, session.selected, session.replacement, session.options.regex).filter((file) => file.before !== file.after);
  const currentPreview = replacements.find((file) => file.path === previewPath) ?? replacements[0];
  const search = async () => {
    if (!workspace || !session.options.query) return;
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    setBusy(true); setError(""); setApplied(false);
    const state = useAppStore.getState();
    const buffers = state.editorTabs.filter((tab) => !tab.loading && !tab.error && !tab.largeFile).filter((tab) => {
      const path = normalizeWorkspacePath(tab.path);
      return workspacePathWithin(path.startsWith("/") || /^[A-Za-z]:\//.test(path) ? path : `${workspace}/${path}`, workspace);
    }).map((tab) => ({ path: normalizeEditorPath(tab.path, workspace), content: tab.content, original: tab.original, content_hash: tab.contentHash ?? "", read_only: Boolean(tab.readOnly) }));
    try {
      const result = await searchWorkspaceText(workspace, session.options, buffers, controller.signal);
      if (controller.signal.aborted || !workspaceRootsEqual(workspace, useAppStore.getState().workingDirectory)) return;
      update({ result, selected: new Set(result.files.filter((file) => !file.read_only).flatMap((file) => file.matches.map((match) => match.id))) });
    } catch (caught) {
      if (!controller.signal.aborted) setError(caught instanceof Error ? caught.message : String(caught));
    } finally { if (request.current === controller) setBusy(false); }
  };
  const select = (ids: string[], checked: boolean) => { const selected = new Set(session.selected); for (const id of ids) checked ? selected.add(id) : selected.delete(id); update({ selected }); setApplied(false); };
  const apply = async () => {
    setBusy(true); setError("");
    try { await applyWorkspaceBufferEdits(workspace, replacements, "项目内容替换"); update({ result: null, selected: new Set() }); setPreview(false); setApplied(true); onNavigate?.(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setBusy(false); }
  };
  return <section className="workspace-search-panel" aria-label="项目内容搜索">
    <header><button type="button" className="mc-icon-button" aria-label="返回项目文件" onClick={() => useAppStore.getState().closeWorkspaceSearch()}><ArrowLeft size={15} /></button><strong>项目内容搜索</strong></header>
    <form className="workspace-search-form" onSubmit={(event) => { event.preventDefault(); void search(); }}>
      <label className="workspace-search-input"><Search size={14} /><input ref={queryInput} aria-label="搜索项目内容" placeholder="查找文字或正则…" value={session.options.query} onChange={(event) => option({ query: event.target.value })} /></label>
      <div className="workspace-search-options">{([
        ["regex", ".*", "正则表达式"], ["caseSensitive", "Aa", "区分大小写"], ["wholeWord", "ab", "全词匹配"],
      ] as const).map(([name, label, title]) => <button key={name} type="button" aria-label={title} title={title} aria-pressed={session.options[name]} onClick={() => option({ [name]: !session.options[name] })}>{label}</button>)}<button type="submit" disabled={busy || !workspace || !session.options.query}>{busy ? "搜索中…" : "搜索"}</button></div>
      <input aria-label="包含文件" placeholder="包含：src/**, **/*.ts" value={session.options.include} onChange={(event) => option({ include: event.target.value })} />
      <input aria-label="排除文件" placeholder="排除：**/*.test.*, generated/**" value={session.options.exclude} onChange={(event) => option({ exclude: event.target.value })} />
      <details className="workspace-search-replace"><summary>替换</summary><input aria-label="替换为" placeholder="替换为…" value={session.replacement} onChange={(event) => { update({ replacement: event.target.value }); setApplied(false); }} />{session.options.regex && <small>支持 $1、$&、$&lt;名称&gt; 和 \n</small>}
        <button type="button" disabled={busy || !replacements.length || applied} onClick={() => { setPreviewPath(replacements[0].path); setPreview(true); }}>预览选中替换 · {replacements.length} 个文件</button></details>
    </form>
    {error && <p role="alert" className="workspace-search-error">{error}</p>}
    {applied && <p role="status" className="workspace-search-notice">已修改编辑缓冲区。可在编辑器撤销；保存仍需手动。<button type="button" onClick={() => void search()}>重新搜索</button></p>}
    {session.result && <div className="workspace-search-count">已显示 {session.result.match_count} 处匹配 · {files.length} 个文件{session.result.truncated && <strong> · 结果已截断，仅可替换显示的匹配</strong>}</div>}
    {!workspace && <p className="workspace-search-notice">打开项目后可搜索文件正文。</p>}
    <div className="workspace-search-results">{files.map((file) => <section key={file.path} className="workspace-search-file">
      <header><input type="checkbox" aria-label={`选择 ${file.path} 的匹配`} disabled={file.read_only || applied} checked={file.matches.every((match) => session.selected.has(match.id))} onChange={(event) => select(file.matches.map((match) => match.id), event.target.checked)} /><strong title={file.path}>{file.path}</strong><span>{file.matches.length}</span>{file.from_buffer && <small>缓冲区</small>}{file.read_only && <small>只读</small>}</header>
      {file.matches.map((match) => <div key={match.id} className="workspace-search-match"><input type="checkbox" aria-label={`选择 ${file.path} 第${match.line}行第${match.column}列`} disabled={file.read_only || applied} checked={session.selected.has(match.id)} onChange={(event) => select([match.id], event.target.checked)} />
        <button type="button" title={match.snippet} onClick={() => { useAppStore.getState().openEditorFile(file.path, undefined, { exact: true, line: match.line, column: match.column, endLine: match.end_line, endColumn: match.end_column }); onNavigate?.(); }}><span>{match.line}:{match.column}</span><code>{match.snippet}</code></button></div>)}
    </section>)}{session.result && files.length === 0 && <p>没有匹配项。</p>}</div>
    {!!session.result?.issues.length && <details className="workspace-search-issues"><summary>{session.result.issues.length} 个文件未参与搜索</summary>{session.result.issues.map((issue) => <p key={issue.path}>{issue.path} · {issue.message}</p>)}</details>}
    {preview && createPortal(<div className="overlay-backdrop workspace-replacement-backdrop"><section ref={dialog} className="workspace-replacement-dialog" role="dialog" aria-modal="true" aria-label="项目替换预览" onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setPreview(false); } }}>
      <header><strong>替换预览 · {replacements.length} 个文件</strong><button type="button" className="mc-icon-button" aria-label="关闭替换预览" onClick={() => setPreview(false)}><X size={16} /></button></header>
      <div className="workspace-replacement-body"><nav aria-label="待替换文件">{replacements.map((file) => <button key={file.path} type="button" aria-current={file === currentPreview ? "page" : undefined} onClick={() => setPreviewPath(file.path)}>{file.path}<small>{file.edits.length} 处</small></button>)}</nav>
        <div className="workspace-replacement-diff">{currentPreview && <Suspense fallback={<p>正在载入差异…</p>}><ReplacementDiff file={currentPreview} theme={theme} scale={codeTextScale} /></Suspense>}</div></div>
      {error && <p role="alert" className="workspace-search-error">{error}</p>}<footer><span>仅应用选中的匹配；源文件保持未保存状态。</span><button type="button" onClick={() => setPreview(false)} disabled={busy}>取消</button><button type="button" onClick={() => void apply()} disabled={busy || !replacements.length}>{busy ? "正在应用…" : "应用到编辑缓冲区"}</button></footer>
    </section></div>, document.body)}
  </section>;
}
