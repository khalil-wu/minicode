import { useEffect, useState } from "react";
import { SquarePen, Trash2 } from "../lib/icons";
import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { showConfirm } from "./DialogService";
import { SettingsSourceIcon } from "./SettingsSourceIcon";
import "./WorktreesTab.css";

type Preferences = { worktree_root: string; default_worktree_root: string; auto_cleanup_enabled: boolean; keep_count: number; upstream_refresh_mode: "best-effort" | "never" };
type ManagedWorktree = { path: string; repo_root: string; branch: string; pinned: boolean; conversation_ids: string[]; can_remove: boolean; protection_reason: string };
export function WorktreesTab({ active = true }: { active?: boolean }) {
  const { t } = useI18n();
  const conversations = useAppStore(state => state.conversations);
  const [preferences, setPreferences] = useState<Preferences | null>(null);
  const [trees, setTrees] = useState<ManagedWorktree[]>([]);
  const [root, setRoot] = useState<string | null>(null);
  const [count, setCount] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(active);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController(); setError("");
    setLoading(true);
    const reads = ["/api/settings/worktrees", "/api/settings/worktrees/managed"].map(async path => {
      const response = await fetchWithTimeout(new URL(path, apiBase()), { headers: authHeaders(), signal: controller.signal });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      return response.json();
    });
    void Promise.all(reads).then(([next, managed]) => { if (!controller.signal.aborted) { setPreferences(next as Preferences); setTrees((managed as { worktrees: ManagedWorktree[] }).worktrees); } })
      .catch(reason => { if (!controller.signal.aborted) setError(String(reason)); });
    void Promise.allSettled(reads).then(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [active, revision]);
  const save = async (patch: Partial<Preferences>) => {
    setBusy(true); setError("");
    try {
      const response = await fetchWithTimeout(new URL("/api/settings/worktrees", apiBase()), { method: "PUT", headers: authHeaders({ "content-type": "application/json" }), body: JSON.stringify(patch) });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      setPreferences(await response.json() as Preferences);
      if ("worktree_root" in patch) setRoot(null);
      if ("keep_count" in patch) setCount(null);
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };
  const remove = async (tree: ManagedWorktree) => {
    if (!await showConfirm({ title: t("删除工作树"), message: tree.path, confirmLabel: t("删除"), danger: true })) return;
    setBusy(true); setError("");
    try {
      const response = await fetchWithTimeout(new URL("/api/settings/worktrees/remove", apiBase()), { method: "POST", headers: authHeaders({ "content-type": "application/json" }), body: JSON.stringify({ path: tree.path }) });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      const result = await response.json() as { removed: boolean; error?: string };
      if (!result.removed) throw new Error(result.error || t("无法删除工作树。"));
      setRevision(value => value + 1);
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };
  const isEmpty = preferences !== null && trees.length === 0;
  return <>
    {error && <p role="alert">{error}</p>}
    <div className="settings-worktrees-content">
      <section className="settings-group"><div className="settings-card">
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("工作树根目录")}</div><div className="settings-row-description">{t("MiniCode 创建托管工作树的目录。留空则使用默认位置。")}</div></div><input className="settings-git-prefix" aria-label={t("工作树根目录")} disabled={!preferences || busy} placeholder={preferences?.default_worktree_root} value={root ?? preferences?.worktree_root ?? ""} onChange={event => setRoot(event.target.value)} onBlur={() => { if (root !== null && root !== preferences!.worktree_root) void save({ worktree_root: root }); }} /></div>
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("创建工作树前始终获取上游更新")}</div><div className="settings-row-description">{t("通常会在常规 Git 操作中获取分支更新。开启后还会在每次新建工作树前获取。")}</div></div><button type="button" className="settings-toggle" role="switch" aria-label={t("创建工作树前始终获取上游更新")} disabled={!preferences || busy} aria-checked={preferences?.upstream_refresh_mode === "best-effort"} data-active={preferences?.upstream_refresh_mode === "best-effort"} onClick={() => { void save({ upstream_refresh_mode: preferences!.upstream_refresh_mode === "best-effort" ? "never" : "best-effort" }); }}><span /></button></div>
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("自动删除旧工作树")}</div><div className="settings-row-description">{t("建议保持开启，以免旧工作树占用不必要的磁盘空间。")}</div></div><button type="button" className="settings-toggle" role="switch" aria-label={t("自动删除旧工作树")} disabled={!preferences || busy} aria-checked={preferences?.auto_cleanup_enabled ?? true} data-active={preferences?.auto_cleanup_enabled ?? true} onClick={async () => { if (preferences!.auto_cleanup_enabled && !await showConfirm({ title: t("停用工作树自动删除？"), message: t("关闭后，MiniCode 将停止自动清理旧工作树，需要自行管理磁盘空间。"), confirmLabel: t("停用自动删除"), cancelLabel: t("保持自动删除") })) return; void save({ auto_cleanup_enabled: !preferences!.auto_cleanup_enabled }); }}><span /></button></div>
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("自动删除数量上限")}</div><div className="settings-row-description">{t("保留的托管工作树数量。删除前会保存快照，以便恢复。")}</div></div><input type="number" min={1} step={1} aria-label={t("自动删除数量上限")} disabled={!preferences || busy || !preferences.auto_cleanup_enabled} value={count ?? preferences?.keep_count ?? ""} onChange={event => setCount(event.target.value)} onBlur={() => { if (count !== null && Number.isInteger(Number(count)) && Number(count) >= 1 && Number(count) !== preferences!.keep_count) void save({ keep_count: Number(count) }); }} /></div>
      </div></section>
      <section className="settings-group"><div className="settings-section-heading settings-section-heading-row"><h3 className="settings-group-title">{t(isEmpty ? "暂无工作树" : "工作树")}</h3><button type="button" className="settings-icon-button settings-refresh-button settings-worktrees-refresh" aria-label={t("刷新")} title={t("刷新")} disabled={loading} aria-busy={loading} onClick={() => setRevision(value => value + 1)}>
        {loading ? <span className="settings-refresh-spinner"><SettingsSourceIcon icon="spinner" size={12} /></span> : <SettingsSourceIcon icon="refresh" />}
      </button></div>
        {!preferences && !error && <p role="status">{t("正在获取工作树详情…")}</p>}
        <div className={isEmpty ? "settings-card settings-worktrees-empty-card" : "settings-card"}>
          {isEmpty && <p className="settings-worktrees-empty settings-section-description">{t("MiniCode 创建的工作树将显示在这里。")}</p>}
          {trees.map(tree => <details key={tree.path} className="settings-detail-block"><summary className="settings-row"><span className="settings-row-copy"><span className="settings-row-title">{tree.repo_root.split(/[\\/]/).pop()} · {tree.branch}</span><span className="settings-row-description">{tree.path}</span></span></summary><div className="settings-page-note"><div className="settings-row"><button type="button" className="settings-action-button" onClick={() => { const state = useAppStore.getState(); void state.createConversation({ bindWorkspace: true, workspaceRoot: tree.path, appMode: "cowork" }).then(created => { if (created) useAppStore.getState().toggleSettings(); }); }}><SquarePen size={15} />{t("在此工作树中新建聊天")}</button><button type="button" className="settings-action-button" disabled={busy || !tree.can_remove} title={tree.protection_reason} onClick={() => { void remove(tree); }}><Trash2 size={15} />{t("删除")}</button></div><h4>{t("已知聊天")}</h4>{tree.conversation_ids.map(id => <button type="button" className="settings-action-button" key={id} onClick={() => { const state = useAppStore.getState(); state.switchConversation(id); state.toggleSettings(); }}>{conversations.find(chat => chat.id === id)?.title || id}</button>)}</div></details>)}</div>
      </section>
    </div>
  </>;
}
