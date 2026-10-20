import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, LoaderCircle, Trash2, X } from "../lib/icons";
import { useI18n } from "../i18n";
import { useFocusTrap } from "../hooks/useFocusTrap";
import type { PluginEntry, PluginMarketplace } from "./PluginsTab";
import { MarketplaceIcon } from "./MarketplaceIcon";

export const marketplaceName = (source: PluginMarketplace) => source.interface?.displayName || source.name;
const isGit = (source: PluginMarketplace) => ["git", "github"].includes(source.source.source);

export function MarketplaceSettings({ marketplaces, search, loading, error, busy, upgrading, onRetry, onUpgrade, onRemove }: {
  marketplaces: PluginMarketplace[]; search: string; loading: boolean; error: string; busy: boolean;
  upgrading?: string; onRetry: () => void; onUpgrade: (source: PluginMarketplace) => void; onRemove: (source: PluginMarketplace) => void;
}) {
  const { t } = useI18n();
  const query = search.trim().toLocaleLowerCase();
  const sources = marketplaces.filter(source => !source.isBuiltIn && `${source.name} ${marketplaceName(source)} ${source.materialized_path || ""}`.toLocaleLowerCase().includes(query));
  return <section className="plugin-marketplace-settings" aria-label={t("插件市场")}>
    {loading && marketplaces.length === 0 && <div className="settings-mcp-empty">{t("正在加载插件市场…")}</div>}
    {error && <div className="plugin-load-error" role="alert"><div><strong>{t("无法加载插件市场")}</strong><code>{error}</code></div><button type="button" disabled={loading} onClick={onRetry}>{t("重试")}</button></div>}
    {!error && sources.map(source => <article className="plugin-marketplace-row" key={source.name}>
      <MarketplaceIcon />
      <div className="plugin-marketplace-copy"><strong>{marketplaceName(source)}</strong><span>{t("{count} 个插件", { count: source.plugins.length })}</span>
        {source.materialized_path && <span className="plugin-marketplace-path" title={source.materialized_path}>{source.materialized_path}</span>}
        {source.error && <code>{source.error}</code>}
      </div>
      <button type="button" className="settings-action-button" disabled={busy || !isGit(source)}
        title={isGit(source) ? t("升级插件市场") : t("只有 Git 插件市场可以升级")}
        aria-busy={upgrading === source.name} aria-label={t("升级插件市场 {name}", { name: marketplaceName(source) })} onClick={() => onUpgrade(source)}>{upgrading === source.name && <LoaderCircle size={13} className="settings-spin" />}{t("升级")}</button>
      <button type="button" className="settings-icon-button" disabled={busy} title={t("移除插件市场")}
        aria-label={t("移除插件市场 {name}", { name: marketplaceName(source) })} onClick={() => onRemove(source)}><Trash2 size={16} /></button>
    </article>)}
    {!loading && !error && sources.length === 0 && <div className="settings-mcp-empty">{t("没有找到插件市场")}</div>}
  </section>;
}

export function MarketplaceRemoveDialog({ marketplace, onLoadInstalled, onUninstall, onRefreshRuntime, onRemove, onClose }: {
  marketplace: PluginMarketplace; onLoadInstalled: (marketplace: string, signal: AbortSignal) => Promise<PluginEntry[]>;
  onUninstall: (plugin: PluginEntry) => Promise<string[]>; onRefreshRuntime: () => Promise<void>;
  onRemove: () => Promise<void>; onClose: () => void;
}) {
  const { t } = useI18n();
  const dialog = useFocusTrap(true);
  const loadInstalled = useRef(onLoadInstalled).current;
  const [plugins, setPlugins] = useState<PluginEntry[]>();
  const [loadError, setLoadError] = useState("");
  const [error, setError] = useState("");
  const [completed, setCompleted] = useState(0);
  const [running, setRunning] = useState(false);
  const [runtimePending, setRuntimePending] = useState(false);
  const loadController = useRef<AbortController>();
  const removalController = useRef<AbortController>();
  const load = async () => {
    loadController.current?.abort();
    const controller = new AbortController(); loadController.current = controller;
    setLoadError(""); setPlugins(undefined);
    try {
      const inventory = await loadInstalled(marketplace.name, controller.signal);
      if (!controller.signal.aborted) setPlugins(inventory);
    } catch (cause) {
      if (!controller.signal.aborted) setLoadError(cause instanceof Error ? cause.message : String(cause));
    }
  };
  useEffect(() => { void load(); return () => { loadController.current?.abort(); removalController.current?.abort(); }; }, [marketplace.name, loadInstalled]);
  const remove = async () => {
    const controller = new AbortController(); removalController.current = controller;
    setRunning(true); setError("");
    let done = completed;
    try {
      if (runtimePending) { await onRefreshRuntime(); controller.signal.throwIfAborted(); setRuntimePending(false); }
      for (const plugin of plugins!.slice(done)) {
        const errors = await onUninstall(plugin);
        controller.signal.throwIfAborted();
        done += 1; setCompleted(done);
        if (errors.length) { setRuntimePending(true); throw new Error(errors.join("\n")); }
      }
      await onRemove(); controller.signal.throwIfAborted(); onClose();
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
    } finally { if (!controller.signal.aborted) setRunning(false); }
  };
  return createPortal(<div className="plugin-marketplace-backdrop" ref={dialog} onClick={() => { if (!running) onClose(); }}>
    <form className="plugin-marketplace-dialog plugin-marketplace-remove" role="dialog" aria-modal="true" aria-labelledby="marketplace-remove-title"
      onClick={event => event.stopPropagation()} onSubmit={event => { event.preventDefault(); void remove(); }}
      onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); if (!running) onClose(); } }}>
      <header><h2 id="marketplace-remove-title">{t("移除“{name}”？", { name: marketplaceName(marketplace) })}</h2></header>
      {!running && <button type="button" className="plugin-marketplace-close" aria-label={t("关闭")} onClick={onClose}><X size={16} /></button>}
      {loadError ? <div className="plugin-marketplace-submit-error" role="alert"><p>{t("无法加载已安装插件")}</p><code>{loadError}</code><button type="button" className="settings-action-button" onClick={() => void load()}>{t("重试")}</button></div>
        : plugins === undefined ? <div role="status">{t("正在加载已安装插件…")}</div>
        : plugins.length === 0 ? <p>{t("除非再次添加此插件市场，否则其中的插件将不会出现或可供安装。")}</p>
        : <><p>{t("以下插件将被卸载，只有再次添加此插件市场后才能重新安装。")}</p>
          <ul className="plugin-marketplace-removal-list" aria-live="polite">{plugins.map((plugin, index) => <li key={plugin.id || plugin.name}>
            <span>{plugin.displayName || plugin.name}</span>{index < completed ? <span><Check size={14} />{t("已卸载")}</span>
              : running && index === completed ? <span><LoaderCircle size={14} className="settings-spin" />{t("正在卸载")}</span> : null}
          </li>)}</ul></>}
      {error && <div className="plugin-marketplace-submit-error" role="alert"><p>{runtimePending ? t("插件已卸载，运行时尚未完成刷新。重试以继续。")
        : completed < (plugins?.length || 0) ? t("无法卸载 {name}。重试以继续。", { name: plugins![completed].displayName || plugins![completed].name }) : t("无法移除插件市场。重试以继续。")}</p><code>{error}</code></div>}
      <footer><button type="button" className="settings-action-button" disabled={running} onClick={onClose}>{t("取消")}</button>
        <button type="submit" className="plugin-primary-button" disabled={running || plugins === undefined || Boolean(loadError)}>{error ? t("重试") : plugins?.length ? t("卸载并移除") : t("移除")}</button></footer>
    </form>
  </div>, document.body);
}
