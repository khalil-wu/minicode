import { useEffect, useRef, useState } from "react";
import { ChevronRight, LoaderCircle } from "../../lib/icons";
import { ContextMenu } from "../../components/ContextMenu";
import { openConversationBrowserFilePreview } from "../../chat/openAttachmentPreview";
import { uploadComposerFiles } from "../../composer/uploads";
import { useDesktopPreferences } from "../../hooks/useDesktopPreferences";
import { desktop, type FileOpenTargetId } from "../../desktop/runtime";
import { FileOpenTargetIcon } from "../../components/FileOpenTargetIcon";
import { isNativeExternalFile, preferredFileTargetForWorkspace } from "../../lib/open-preferred-target";
import { useI18n } from "../../i18n";
import { copyText } from "../../lib/clipboard";
import { fileIcon, folderIcon } from "../../lib/file-icons";
import { normalizeWorkspacePath, workspacePathWithin, workspaceRootsEqual } from "../../lib/workspace-path";
import { isTextMediaType } from "../../lib/media-types";
import { saveBlobFile } from "../../lib/save-blob";
import { pushToast } from "../../overlays/ToastContainer";
import {
  fetchConversationBrowserOriginal, fetchConversationBrowserTree,
  type ConversationBrowserEntry, type ConversationBrowserOwner, type ConversationBrowserTree,
} from "../../protocol/conversation-browser";
import { useAppStore } from "../../stores";
import { ROW_HEIGHT } from "../fileTreeTypes";

export function ConversationFilesTree({ owner, browserRoot, connected, version }: {
  owner: ConversationBrowserOwner; browserRoot: string; connected: boolean; version: number;
}) {
  const { t } = useI18n();
  const { preferences } = useDesktopPreferences();
  const [directories, setDirectories] = useState<Record<string, ConversationBrowserTree>>({});
  const [expanded, setExpanded] = useState(new Set<string>());
  const expandedRef = useRef(expanded);
  expandedRef.current = expanded;
  const [loading, setLoading] = useState(new Set<string>());
  const [error, setError] = useState<{ path: string; message: string } | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [menu, setMenu] = useState<{ entry: ConversationBrowserEntry; x: number; y: number } | null>(null);
  const controller = useRef<AbortController | null>(null);

  const loadDirectory = async (path: string, signal: AbortSignal) => {
    setLoading((current) => new Set(current).add(path));
    setError(null);
    try {
      const page = await fetchConversationBrowserTree(owner, path, signal);
      if (!signal.aborted) setDirectories((current) => ({ ...current, [path]: page }));
    } catch (reason) {
      if (!signal.aborted) setError({ path, message: reason instanceof Error ? reason.message : String(reason) });
    } finally {
      if (!signal.aborted) setLoading((current) => { const next = new Set(current); next.delete(path); return next; });
    }
  };

  useEffect(() => {
    if (!connected) { setMenu(null); return; }
    const operation = new AbortController();
    controller.current = operation;
    void Promise.all([".", ...expandedRef.current].map((path) => loadDirectory(path, operation.signal)));
    return () => { operation.abort(); controller.current = null; };
  }, [owner.conversationId, owner.executionCwd, connected, version]);

  const root = directories["."]?.workspace_root ?? browserRoot;
  const preferredTarget = preferences ? preferredFileTargetForWorkspace(preferences, owner.executionCwd) : "minicode";
  const fileTarget = (entry: ConversationBrowserEntry) => preferredTarget !== "minicode" && isNativeExternalFile(entry.path) ? "system" : preferredTarget;
  const absolutePath = (entry: ConversationBrowserEntry) => `${normalizeWorkspacePath(root).replace(/\/$/, "")}/${entry.path}`;
  const ownerIsCurrent = () => {
    const state = useAppStore.getState();
    const conversation = state.conversations.find((item) => item.id === owner.conversationId);
    return state.conversationId === owner.conversationId && !state.pendingConversationSwitchId
      && workspaceRootsEqual(conversation?.executionCwd || conversation?.worktreePath || conversation?.workspaceRoot, owner.executionCwd);
  };
  const openFile = (entry: ConversationBrowserEntry) => openConversationBrowserFilePreview({ ...owner, path: entry.path, name: entry.name });
  const toggle = (entry: ConversationBrowserEntry) => {
    const opening = !expanded.has(entry.path);
    setExpanded((current) => { const next = new Set(current); opening ? next.add(entry.path) : next.delete(entry.path); return next; });
    if (opening && !directories[entry.path]) void loadDirectory(entry.path, controller.current!.signal);
  };
  const runFileAction = async (entry: ConversationBrowserEntry, action: "open-in" | "save" | "attach", targetId?: FileOpenTargetId) => {
    const signal = controller.current!.signal;
    try {
      if (action === "open-in") {
        const fullPath = absolutePath(entry);
        const openInApp = () => {
          if (!ownerIsCurrent()) return;
          const state = useAppStore.getState();
          if (workspaceRootsEqual(state.workingDirectory, owner.executionCwd) && workspacePathWithin(fullPath, owner.executionCwd) && isTextMediaType("", entry.name)) {
            state.openEditorFile(fullPath, entry.name, { exact: true });
            state.setAppMode("code");
          } else openFile(entry);
        };
        if (!ownerIsCurrent()) return;
        if (!desktop() || (targetId ?? fileTarget(entry)) === "minicode") openInApp();
        else {
          const result = await desktop()!.openPreferredPath(fullPath, undefined, { workspaceRoot: owner.executionCwd, ...(targetId ? { targetId } : {}) });
          if (result.openInEditor) openInApp();
        }
        return;
      }
      const blob = await fetchConversationBrowserOriginal(owner, entry.path, signal);
      if (!ownerIsCurrent()) return;
      if (action === "attach") {
        uploadComposerFiles([new File([blob], entry.name, { type: blob.type })]);
        return;
      }
      await saveBlobFile(blob, entry.name);
    } catch (reason) {
      if (!signal.aborted) pushToast(reason instanceof Error ? reason.message : String(reason), "error");
    }
  };

  const renderEntries = (entries: ConversationBrowserEntry[], depth = 0): React.ReactNode => entries.map((entry) => {
    const open = expanded.has(entry.path);
    return <div key={entry.path}>
      <div role="treeitem" tabIndex={connected ? 0 : -1} aria-level={depth + 1} aria-expanded={entry.is_dir ? open : undefined}
        aria-selected={selected === entry.path} aria-disabled={!connected} className="tree-node-hover" data-browser-path={entry.path}
        title={absolutePath(entry)} onClick={() => { if (connected) { setSelected(entry.path); entry.is_dir ? toggle(entry) : openFile(entry); } }}
        onKeyDown={(event) => {
          if (!connected) return;
          if (["Enter", " "].includes(event.key)) { event.preventDefault(); setSelected(entry.path); entry.is_dir ? toggle(entry) : openFile(entry); }
          if (entry.is_dir && (event.key === "ArrowRight" && !open || event.key === "ArrowLeft" && open)) { event.preventDefault(); toggle(entry); }
        }}
        onContextMenu={(event) => { event.preventDefault(); if (connected) setMenu({ entry, x: event.clientX, y: event.clientY }); }}
        style={{ display: "flex", alignItems: "center", gap: 6, height: ROW_HEIGHT.compact, margin: "1px 7px", padding: "0 8px", paddingLeft: 8 + depth * 15,
          cursor: "pointer", color: "var(--text-primary)", background: selected === entry.path ? "var(--surface-active)" : "transparent", borderRadius: 8 }}>
        {entry.is_dir ? <ChevronRight size={14} aria-hidden="true" style={{ transform: open ? "rotate(90deg)" : undefined, transition: "transform var(--transition-fast)" }} /> : <span style={{ width: 14 }} />}
        <span aria-hidden="true">{entry.is_dir ? folderIcon(open, 18, entry.name) : fileIcon(entry.name, { size: 18 })}</span>
        <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontFamily: "var(--font-ui)", fontSize: "var(--text-sm)", lineHeight: "20px" }}>{entry.name}</span>
        {loading.has(entry.path) && <LoaderCircle size={14} className="animate-spin" aria-label={t("正在加载 {v0}", { v0: entry.name })} />}
      </div>
      {entry.is_dir && open && <div role="group">{renderEntries(directories[entry.path]?.entries ?? [], depth + 1)}</div>}
    </div>;
  });

  return <section aria-label={t("文件资源管理器")}>
    <div title={root} style={{ display: "flex", alignItems: "center", gap: 6, padding: "7px 14px", color: "var(--text-secondary)", fontFamily: "var(--font-ui)", fontSize: "var(--text-sm)" }}>
      {folderIcon(true, 18)}<span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{root.split(/[\\/]/).filter(Boolean).at(-1) ?? root}</span>
    </div>
    {loading.has(".") && !directories["."] && <div role="status" aria-label={t("正在加载文件…")}><LoaderCircle size={16} className="animate-spin" /></div>}
    <div role="tree" aria-label={t("文件")}>{renderEntries(directories["."]?.entries ?? [])}</div>
    {directories["."]?.entries.length === 0 && <p style={{ margin: "4px 14px", color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>{t("未找到文件。")}</p>}
    {error && <div role="alert" style={{ margin: "4px 14px", color: "var(--text-secondary)", fontSize: "var(--text-xs)" }}>{error.message}
      <button type="button" className="btn-ghost" disabled={!connected} onClick={() => void loadDirectory(error.path, controller.current!.signal)}>{t("重试")}</button></div>}
    {menu && <ContextMenu position={{ x: menu.x, y: menu.y }} onClose={() => setMenu(null)} items={[
      { label: t("打开"), onClick: () => { menu.entry.is_dir ? toggle(menu.entry) : openFile(menu.entry); } },
      ...(!menu.entry.is_dir ? [{ label: t("在 {v0} 中打开", { v0: fileTarget(menu.entry) === "system" ? t("默认应用") : preferences?.fileOpenTargets.find(target => target.id === fileTarget(menu.entry))?.label ?? "MiniCode" }), onClick: () => void runFileAction(menu.entry, "open-in") },
        ...(preferences?.fileOpenTargets.some(target => !target.hidden) ? [{ label: t("文件打开方式"), submenu: preferences.fileOpenTargets.filter(target => isNativeExternalFile(menu.entry.path) ? target.id === "system" || target.kind === "fileManager" : !target.hidden).map(target => ({
          label: target.label, icon: <FileOpenTargetIcon target={target} />, onClick: () => void runFileAction(menu.entry, "open-in", target.id),
        })) }] : []),
        { separator: true, label: "" },
        { label: t("另存为…"), onClick: () => void runFileAction(menu.entry, "save") }] : []),
      { label: t("复制路径"), onClick: () => void copyText(absolutePath(menu.entry), t("路径")) },
      ...(!menu.entry.is_dir ? [{ label: t("添加到聊天"), onClick: () => void runFileAction(menu.entry, "attach") }] : []),
    ]} />}
  </section>;
}
