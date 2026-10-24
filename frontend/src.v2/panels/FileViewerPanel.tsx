import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight, ChevronUp, Columns2, Eye, EyeOff, FileText, FolderTree, LoaderCircle, MoreHorizontal, Search, X } from "../lib/icons";
import { useAppStore } from "../stores";
import { useI18n } from "../i18n";
import { Tip } from "../components/Tooltip";
import { ContextMenu, type ContextMenuItem } from "../components/ContextMenu";
import { FileOpenTargetIcon } from "../components/FileOpenTargetIcon";
import { useDesktopPreferences } from "../hooks/useDesktopPreferences";
import { desktop, runtime, type FileOpenTargetId } from "../desktop/runtime";
import { preferredFileTargetForWorkspace } from "../lib/open-preferred-target";
import { workspacePathWithin, workspaceRootsEqual } from "../lib/workspace-path";
import { guessLanguageFromPath, useColorizedLines } from "../lib/monaco-colorize";
import { readFileWorkspaceBranches, readFileWorkspaceDiff, readFileWorkspaceOriginal, readFileWorkspacePreview, workspaceFileAbsolutePath, type FileWorkspaceBranches } from "../protocol/file-workspace";
import type { WorkspaceFilePreviewResponse } from "../protocol/workspace";
import { saveBlobFile } from "../lib/save-blob";
import { pushToast } from "../overlays/ToastContainer";
import { MarkdownRenderer } from "../chat/messages/MarkdownRenderer";
import { ConversationFilesTree } from "../shell/tabs/ConversationFilesTree";
import { fileViewerRows } from "./file-viewer-rows";
import "./FileViewerPanel.css";

export interface FileViewerPanelProps { tabId: string; active?: boolean }

export function FileViewerPanel({ tabId, active = true }: FileViewerPanelProps) {
  const tab = useAppStore(state => state.workspaceFileTabs.find(value => value.id === tabId));
  return tab ? <FileViewer key={tab.id} tabId={tab.id} active={active} /> : null;
}

function FileViewer({ tabId, active }: Required<FileViewerPanelProps>) {
  const { t } = useI18n();
  const tab = useAppStore(state => state.workspaceFileTabs.find(value => value.id === tabId)!);
  const connected = useAppStore(state => state.isConnected);
  const version = useAppStore(state => state.fileTreeVersion);
  const { preferences } = useDesktopPreferences();
  const [preview, setPreview] = useState<WorkspaceFilePreviewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [branches, setBranches] = useState<FileWorkspaceBranches | null>(null);
  const [base, setBase] = useState("");
  const [patch, setPatch] = useState("");
  const [patchBase, setPatchBase] = useState("");
  const [comparisonLoading, setComparisonLoading] = useState(false);
  const [comparisonError, setComparisonError] = useState("");
  const [changes, setChanges] = useState(true);
  const [split, setSplit] = useState(false);
  const [treeVisible, setTreeVisible] = useState(true);
  const [treeRoot, setTreeRoot] = useState(tab.owner.browserRoot);
  const [find, setFind] = useState(false);
  const [query, setQuery] = useState("");
  const [hit, setHit] = useState(0);
  const [menu, setMenu] = useState<{ kind: "branches" | "open"; x: number; y: number } | null>(null);
  const [mediaUrl, setMediaUrl] = useState("");
  const [mediaLoading, setMediaLoading] = useState(false);
  const [renderMarkdown, setRenderMarkdown] = useState(false);
  const findInput = useRef<HTMLInputElement>(null);
  const codeRoot = useRef<HTMLDivElement>(null);
  const absolutePath = workspaceFileAbsolutePath(tab.owner, tab.path);
  const treeOwner = useMemo(() => !workspacePathWithin(treeRoot, tab.owner.browserRoot)
    ? { ...tab.owner, conversationId: null, executionCwd: treeRoot, browserRoot: treeRoot } : tab.owner, [tab.owner, treeRoot]);

  useEffect(() => {
    if (!active || !connected) return;
    const operation = new AbortController();
    setLoading(true); setError("");
    void readFileWorkspacePreview(tab.owner, tab.path, operation.signal).then(value => {
      if (!operation.signal.aborted) setPreview(value);
    }).catch(reason => { if (!operation.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason)); })
      .finally(() => { if (!operation.signal.aborted) setLoading(false); });
    if (workspacePathWithin(absolutePath, tab.owner.executionCwd)) {
      void readFileWorkspaceBranches(tab.owner, operation.signal).then(value => {
        if (operation.signal.aborted) return;
        setBranches(value); setBase(current => current || value.default_base_branch || "HEAD");
      }).catch(reason => { if (!operation.signal.aborted) setComparisonError(reason instanceof Error ? reason.message : String(reason)); });
    }
    return () => operation.abort();
  }, [tab.id, active, connected, version]);

  useEffect(() => {
    if (!active || !connected || !changes || !branches?.is_git_repo || !base) return;
    const operation = new AbortController();
    setComparisonError(""); setPatch(""); setComparisonLoading(true);
    void readFileWorkspaceDiff(tab.owner, tab.path, base, operation.signal).then(value => {
      if (!operation.signal.aborted) { setPatch(value.diff); setPatchBase(base); }
    }).catch(reason => { if (!operation.signal.aborted) setComparisonError(reason instanceof Error ? reason.message : String(reason)); })
      .finally(() => { if (!operation.signal.aborted) setComparisonLoading(false); });
    return () => { operation.abort(); setComparisonLoading(false); };
  }, [tab.id, active, connected, branches, base, changes, version]);

  const media = preview?.media_type ?? "";
  const nativeMedia = /^(image\/|audio\/|video\/|application\/pdf)/.test(media);
  useEffect(() => {
    if (!active || !preview || !/^(image\/|audio\/|video\/|application\/pdf)/.test(media)) return;
    const operation = new AbortController();
    let url = "";
    setMediaLoading(true);
    void readFileWorkspaceOriginal(tab.owner, tab.path, operation.signal).then(blob => {
      if (operation.signal.aborted) return;
      url = URL.createObjectURL(blob); setMediaUrl(url);
    }).catch(reason => { if (!operation.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason)); })
      .finally(() => { if (!operation.signal.aborted) setMediaLoading(false); });
    return () => { operation.abort(); if (url) URL.revokeObjectURL(url); setMediaUrl(""); setMediaLoading(false); };
  }, [tab.id, active, media]);

  const rows = useMemo(() => fileViewerRows(preview?.content ?? "", comparisonError || comparisonLoading || patchBase !== base ? "" : patch, changes && Boolean(branches?.is_git_repo)), [preview?.content, patch, patchBase, base, comparisonError, comparisonLoading, changes, branches]);
  const syntax = useColorizedLines(rows, guessLanguageFromPath(tab.path));
  const hits = useMemo(() => query ? rows.flatMap((row, index) => row.text.toLocaleLowerCase().includes(query.toLocaleLowerCase()) ? [index] : []) : [], [rows, query]);
  useEffect(() => { setHit(0); }, [query]);
  useEffect(() => {
    const row = hits.length ? hits[hit % hits.length] : tab.line ? rows.findIndex(value => value.newLine === tab.line) : -1;
    if (row >= 0) codeRoot.current?.querySelector(`[data-file-row="${row}"]`)?.scrollIntoView({ block: "center" });
  }, [hit, hits, tab.line, rows]);
  useEffect(() => { if (find) findInput.current?.focus(); }, [find]);

  const visibleTargets = desktop() && !runtime()?.remoteHost ? (preferences?.fileOpenTargets ?? []).filter(target => !target.hidden && target.id !== "minicode") : [];
  const preferred = preferences ? preferredFileTargetForWorkspace(preferences, tab.owner.executionCwd) : undefined;
  const primary = visibleTargets.find(target => target.id === preferred && target.kind !== "fileManager");
  const external = async (targetId: FileOpenTargetId) => {
    try { await desktop()!.openPreferredPath(absolutePath, undefined, { targetId, workspaceRoot: tab.owner.executionCwd,
      persistPreferredTargetPath: tab.owner.executionCwd }); }
    catch (reason) { pushToast(reason instanceof Error ? reason.message : String(reason), "error"); }
  };
  const save = async () => {
    try { await saveBlobFile(await readFileWorkspaceOriginal(tab.owner, tab.path), tab.title); }
    catch (reason) { pushToast(reason instanceof Error ? reason.message : String(reason), "error"); }
  };
  const openItems: ContextMenuItem[] = [
    ...visibleTargets.filter(target => target.kind !== "fileManager").map(target => ({ label: target.label,
      icon: <FileOpenTargetIcon target={target} />, onClick: () => { void external(target.id); } })),
    ...visibleTargets.filter(target => target.kind === "fileManager").map(target => ({ label: t("打开所在文件夹"), icon: <FileOpenTargetIcon target={target} />, onClick: () => { void external(target.id); } })),
    { label: t("另存为…"), onClick: () => { void save(); } },
  ];
  const toggleMenu = (kind: "branches" | "open", button: HTMLButtonElement) => {
    const rect = button.getBoundingClientRect(); setMenu(value => value?.kind === kind ? null : { kind, x: rect.left, y: rect.bottom + 4 });
  };
  const action = (label: string, icon: ReactNode, onClick: () => void, pressed?: boolean) => <Tip content={label}><button type="button" aria-label={label} aria-pressed={pressed} onClick={onClick}>{icon}</button></Tip>;
  const segments = [tab.owner.browserRoot.split(/[\\/]/).filter(Boolean).at(-1)!, ...tab.path.split("/")];
  const highlight = (text: string) => {
    if (!query) return text;
    const values: ReactNode[] = []; let from = 0, index = text.toLocaleLowerCase().indexOf(query.toLocaleLowerCase());
    while (index >= 0) { values.push(text.slice(from, index), <mark key={index}>{text.slice(index, index + query.length)}</mark>); from = index + query.length; index = text.toLocaleLowerCase().indexOf(query.toLocaleLowerCase(), from); }
    values.push(text.slice(from)); return values;
  };
  const line = (index: number, side?: "old" | "new") => {
    const row = rows[index], blank = side === "old" && row.kind === "add" || side === "new" && row.kind === "del";
    return <div key={index} data-file-row={index} className="mc-file-viewer-line" data-kind={blank ? "context" : row.kind} data-search-hit={hits.length > 0 && hits[hit % hits.length] === index}>
      <span className="mc-file-viewer-number">{blank ? "" : side === "old" ? row.oldLine : row.newLine ?? row.oldLine}</span>
      {blank ? <code /> : query ? <code>{highlight(row.text) || " "}</code> : syntax?.[index] ? <code dangerouslySetInnerHTML={{ __html: syntax[index] }} /> : <code>{row.text || " "}</code>}
    </div>;
  };

  return <section className="mc-file-viewer" aria-label={t("文件查看器：{v0}", { v0: tab.title })} onKeyDown={event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") { event.preventDefault(); event.stopPropagation(); setFind(true); }
  }}>
    <nav className="mc-file-viewer-path" aria-label={t("文件路径")}>{segments.map((segment, index) => <span key={index} style={{ display: "contents" }}>
      {index > 0 && <ChevronRight size={12} />}<button type="button" onClick={() => {
        if (index === segments.length - 1) return;
        setTreeVisible(true); setTreeRoot(index === 0 ? tab.owner.browserRoot : workspaceFileAbsolutePath(tab.owner, segments.slice(1, index + 1).join("/")));
      }}>{segment}</button></span>)}</nav>
    <div className="mc-file-viewer-toolbar">
      {branches?.is_git_repo && <><button type="button" aria-label={t("文件比较基线")} aria-haspopup="menu" aria-expanded={menu?.kind === "branches"} onClick={event => toggleMenu("branches", event.currentTarget)}>{base}<ChevronDown size={13} /></button>
        {action(t(changes ? "隐藏更改" : "显示更改"), changes ? <EyeOff size={16} /> : <Eye size={16} />, () => setChanges(value => !value), changes)}</>}
      <span className="mc-file-viewer-toolbar-spacer" />
      {media === "text/markdown" && action(t("渲染 Markdown"), <FileText size={16} />, () => setRenderMarkdown(value => !value), renderMarkdown)}
      {!nativeMedia && preview?.kind !== "binary" && action(t("在文件中搜索"), <Search size={16} />, () => setFind(value => !value), find)}
      {branches?.is_git_repo && changes && action(t(split ? "切换到统一差异视图" : "切换到拆分差异视图"), <Columns2 size={16} />, () => setSplit(value => !value))}
      {action(t("切换文件树"), <FolderTree size={16} />, () => setTreeVisible(value => !value), treeVisible)}
      {primary && action(t("在 {v0} 中打开", { v0: primary.label }), <FileOpenTargetIcon target={primary} />, () => { void external(primary.id); })}
      <button type="button" aria-label={t("打开选项菜单")} aria-haspopup="menu" aria-expanded={menu?.kind === "open"} onClick={event => toggleMenu("open", event.currentTarget)}><MoreHorizontal size={17} /></button>
    </div>
    {find && <div className="mc-file-viewer-find"><Search size={14} /><input ref={findInput} aria-label={t("在文件中搜索")} value={query} onChange={event => setQuery(event.target.value)} onKeyDown={event => {
      if (event.key === "Escape") { event.stopPropagation(); setFind(false); setQuery(""); }
      if (event.key === "Enter" && hits.length) { event.preventDefault(); setHit(value => (value + (event.shiftKey ? -1 : 1) + hits.length) % hits.length); }
    }} /><span>{hits.length ? `${hit % hits.length + 1}/${hits.length}` : "0/0"}</span><button type="button" aria-label={t("上一个匹配项")} disabled={!hits.length} onClick={() => setHit(value => (value - 1 + hits.length) % hits.length)}><ChevronUp size={14} /></button><button type="button" aria-label={t("下一个匹配项")} disabled={!hits.length} onClick={() => setHit(value => (value + 1) % hits.length)}><ChevronDown size={14} /></button><button type="button" aria-label={t("关闭搜索")} onClick={() => { setFind(false); setQuery(""); }}><X size={14} /></button></div>}
    <div className="mc-file-viewer-body"><div className="mc-file-viewer-content" ref={codeRoot} aria-busy={comparisonLoading || loading}>
      {loading && !preview || mediaLoading ? <div className="mc-file-viewer-message" role="status"><LoaderCircle size={18} className="animate-spin" aria-label={t("正在加载文件…")} /></div> : error ? <div className="mc-file-viewer-message" role="alert">{error}</div> : <>
        {comparisonError && <div className="mc-file-viewer-message" role="status">{comparisonError}</div>}
        {preview?.parse_warning && <div className="mc-file-viewer-message">{preview.parse_warning}</div>}
        {preview?.parse_error && <div className="mc-file-viewer-message" role="status">{preview.parse_error}</div>}
        {preview?.truncated && <div className="mc-file-viewer-message">{t("文件较大，仅显示可预览的内容。")}</div>}
        {mediaUrl && media.startsWith("image/") ? <img className="mc-file-viewer-media" src={mediaUrl} alt={tab.title} />
          : mediaUrl && media === "application/pdf" ? <iframe className="mc-file-viewer-media" src={mediaUrl} title={tab.title} />
          : mediaUrl && media.startsWith("audio/") ? <audio className="mc-file-viewer-media" controls src={mediaUrl} />
          : mediaUrl && media.startsWith("video/") ? <video className="mc-file-viewer-media" controls src={mediaUrl} />
          : preview?.kind === "binary" ? <div className="mc-file-viewer-message">{t("不支持应用内预览。此文件是二进制文件，无法提取可显示文本。")}</div>
          : renderMarkdown ? <div className="md-prose" style={{ padding: 20 }}><MarkdownRenderer content={preview?.content ?? ""} workspaceRoot={tab.owner.browserRoot} conversationId={tab.owner.conversationId ?? undefined} /></div>
          : <div className="mc-file-viewer-code">{split && changes && branches?.is_git_repo ? <div className="mc-file-viewer-split"><div>{rows.map((_, index) => line(index, "old"))}</div><div>{rows.map((_, index) => line(index, "new"))}</div></div> : rows.map((_, index) => line(index))}</div>}
      </>}
    </div>{treeVisible && <aside className="mc-file-viewer-tree"><ConversationFilesTree key={`${treeOwner.executionCwd}:${treeRoot}`} owner={treeOwner} browserRoot={treeRoot} connected={active && connected} version={version} onRootChange={setTreeRoot}
      comparison={treeOwner === tab.owner && workspaceRootsEqual(treeRoot, tab.owner.executionCwd) ? branches : undefined}
      comparisonBase={treeOwner === tab.owner && workspaceRootsEqual(treeRoot, tab.owner.executionCwd) ? base : undefined} /></aside>}</div>
    {menu && <ContextMenu position={menu} onClose={() => setMenu(null)} ariaLabel={t(menu.kind === "open" ? "打开选项菜单" : "文件比较基线")} items={menu.kind === "open" ? openItems : [...new Set(["HEAD", ...(branches?.branches ?? [])])].map(value => ({ label: value, checked: base === value, checkType: "radio", onClick: () => setBase(value) }))} />}
  </section>;
}
