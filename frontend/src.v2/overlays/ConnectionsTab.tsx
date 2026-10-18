import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Info, Laptop, Monitor, MoreHorizontal, Pencil, Plus, RefreshCw, Server, X } from "lucide-react";
import { desktop, type RemoteDesktopConnection } from "../desktop/runtime";
import { useI18n } from "../i18n";
import { ContextMenu } from "../components/ContextMenu";
import { SelectMenu } from "../components/SelectMenu";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { copyText } from "../lib/clipboard";
import { showConfirm } from "./DialogService";
import { pushToast } from "./ToastContainer";
import "./ConnectionsTab.css";

export const ConnectionsTab = ({ active }: { active: boolean }) => {
  const { t: tr } = useI18n();
  const api = desktop()?.connections;
  const [connections, setConnections] = useState<RemoteDesktopConnection[]>([]);
  const [kind, setKind] = useState<"ssh" | "wsl">("ssh");
  const [discovery, setDiscovery] = useState<{ sshAliases: string[]; wslDistributions: string[] }>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [editor, setEditor] = useState<{ draft: RemoteDesktopConnection; mode: "add" | "edit" } | null>(null);
  const [discoveryOpen, setDiscoveryOpen] = useState(false);
  const [selectedAliases, setSelectedAliases] = useState<string[]>([]);
  const [discovering, setDiscovering] = useState(false);
  const [savingAliases, setSavingAliases] = useState(false);
  const [detailsId, setDetailsId] = useState<string | null>(null);
  const [menu, setMenu] = useState<{ connection: RemoteDesktopConnection; anchor: HTMLButtonElement } | null>(null);
  useEffect(() => {
    if (!active || !api) return;
    let cancelled = false;
    setLoading(true);
    void api.list().then(items => { if (!cancelled) { setConnections(items); setError(""); } },
      (reason: Error) => { if (!cancelled) setError(reason.message); }).finally(() => { if (!cancelled) setLoading(false); });
    const unsubscribe = api.onChanged(setConnections);
    return () => { cancelled = true; unsubscribe(); };
  }, [active, api]);
  useEffect(() => {
    if (!active || kind !== "wsl" || !api) return;
    let cancelled = false;
    void api.discover("wsl").then(items => { if (!cancelled) { setDiscovery(items); setError(""); } },
      (reason: Error) => { if (!cancelled) setError(reason.message); });
    return () => { cancelled = true; };
  }, [active, kind, api]);
  const operate = async (id: string, operation: () => Promise<unknown>) => {
    setBusy(id); setError("");
    try { await operation(); }
    catch (reason) { setError((reason as Error).message); }
    finally { setBusy(null); }
  };
  const discoverSsh = async () => {
    setDiscovering(true); setError("");
    try {
      const items = await api!.discover("ssh");
      setDiscovery({ ...items, sshAliases: [...items.sshAliases].sort((left, right) => left.localeCompare(right)) });
    } finally { setDiscovering(false); }
  };
  const add = () => void discoverSsh().then(() => { setSelectedAliases([]); setDiscoveryOpen(true); }, reason => setError((reason as Error).message));
  const addManually = () => {
    setDiscoveryOpen(false); setError("");
    setEditor({ mode: "add", draft: { id: crypto.randomUUID(), displayName: "", kind: "ssh", source: "manual", sshHost: "", sshPort: null, identity: "" } });
  };
  const remove = async (connection: RemoteDesktopConnection) => {
    if (await showConfirm({ title: tr("移除连接"), message: tr("移除“{name}”的连接配置？", { name: connection.displayName }), confirmLabel: tr("移除") })) {
      await operate(connection.id, () => api!.remove(connection.id));
    }
  };
  if (!api) return <p className="settings-note">{tr("请在 MiniCode 桌面端管理连接。")}</p>;
  const visible = connections.filter(connection => connection.kind === kind);
  const availableSsh = discovery?.sshAliases.filter(alias => !connections.some(connection => connection.kind === "ssh" && connection.alias === alias)) || [];
  const adding = discovering || savingAliases;
  const availableWsl = kind === "wsl"
    ? discovery?.wslDistributions.filter(name => !connections.some(connection => connection.kind === "wsl" && connection.distribution === name)) || [] : [];
  const details = connections.find(connection => connection.id === detailsId);
  const saveDiscovered = async () => {
    setSavingAliases(true); setError("");
    try {
      const saved = await api.saveMany(availableSsh.filter(alias => selectedAliases.includes(alias)).map(alias => ({
        id: crypto.randomUUID(), displayName: alias, kind: "ssh", source: "discovered", alias, sshHost: alias, sshPort: null, identity: null,
      })));
      setSelectedAliases([]); setDiscoveryOpen(false);
      await Promise.all(saved.map(connection => api.connect(connection.id)));
    } catch (reason) { setError((reason as Error).message); }
    finally { setSavingAliases(false); }
  };
  return <>
    <nav className="settings-integration-tabs" aria-label={tr("连接类型")}>
      <button type="button" aria-pressed={kind === "ssh"} onClick={() => setKind("ssh")}>SSH</button>
      {desktop()?.platformInfo.platform === "win32" && <button type="button" aria-pressed={kind === "wsl"} onClick={() => setKind("wsl")}>WSL</button>}
    </nav>
    <section className="settings-group remote-connections">
      <header className="remote-connections-heading"><h3>{kind === "ssh" ? tr("此电脑的 SSH 连接") : tr("WSL 连接")}</h3>
        {kind === "ssh" && visible.length > 0 && <button type="button" className="settings-action-button" disabled={adding} aria-busy={discovering} onClick={add}><Plus size={14} />{tr("添加")}</button>}</header>
      {loading ? <p className="settings-note">{tr("正在读取连接…")}</p> : visible.length === 0 && availableWsl.length === 0 && <div className="remote-connections-empty">
        <div className="remote-connections-empty-icon" aria-hidden="true"><Laptop size={24} /><MoreHorizontal size={14} /><Server size={24} /></div><p>{kind === "ssh" ? tr("通过 SSH 连接到远程设备") : tr("未发现 WSL 连接")}</p>
        {kind === "ssh" && <button type="button" className="settings-action-button" data-primary="true" disabled={adding} aria-busy={discovering} onClick={add}>{tr("添加")}</button>}</div>}
      {[
      ...visible.map(connection => ({ displayName: connection.displayName, row: <div className="settings-row remote-connection-row" key={`connection:${connection.id}`}>
        <Monitor size={20} aria-hidden="true" /><div className="settings-row-copy"><div className="settings-row-title">{connection.displayName}</div>
          <div className="settings-row-description" title={connection.sshHost || connection.distribution}>{connection.kind === "ssh" ? `${connection.sshHost}${connection.sshPort ? `:${connection.sshPort}` : ""}` : connection.distribution}</div></div>
        <div className="settings-row-control remote-connection-actions"><span className="remote-connection-status">{connection.status === "connected" ? tr("已连接") : connection.status === "connecting" ? tr("正在连接") : tr("未连接")}</span>
          <button type="button" className="settings-action-button" disabled={busy === connection.id || connection.status === "connecting"} onClick={() => void operate(connection.id, () => api.open(connection.id))}>{busy === connection.id ? tr("正在连接") : connection.status === "connected" ? tr("打开") : tr("连接")}</button>
          <button type="button" className="mc-icon-btn" aria-label={tr("{name}的连接操作", { name: connection.displayName })} aria-haspopup="menu" onClick={event => setMenu({ connection, anchor: event.currentTarget })}><MoreHorizontal size={16} /></button></div>
      </div> })),
      ...availableWsl.map(name => ({ displayName: name, row: <div className="settings-row remote-connection-row" key={`wsl:${name}`}><Monitor size={20} /><div className="settings-row-copy"><div className="settings-row-title">{name}</div></div>
        <button type="button" className="settings-action-button" disabled={busy !== null} onClick={() => void operate(name, async () => {
          const connection = await api.save({ id: crypto.randomUUID(), displayName: name, kind: "wsl", distribution: name }); if (connection) await api.open(connection.id);
        })}>{tr("连接")}</button></div> })),
      ].sort((left, right) => left.displayName.localeCompare(right.displayName)).map(item => item.row)}
      {error && !editor && !discoveryOpen && <p className="settings-note remote-connection-error" role="alert">{error}</p>}
    </section>
    {menu && <ContextMenu anchor={menu.anchor} position={{ x: menu.anchor.getBoundingClientRect().left, y: menu.anchor.getBoundingClientRect().bottom }} onClose={() => setMenu(null)} items={[
      { label: tr("详情"), icon: <Info size={16} />, onClick: () => setDetailsId(menu.connection.id) },
      ...(menu.connection.status === "connected" ? [{ label: tr("断开连接"), onClick: () => void operate(menu.connection.id, () => api.disconnect(menu.connection.id)) }] : []),
      { label: tr("移除连接"), disabled: menu.connection.status !== "disconnected", onClick: () => void remove(menu.connection) },
    ]} />}
    {details && <ConnectionDetails connection={details} editDisabled={busy !== null || discovering || savingAliases} onClose={() => setDetailsId(null)} onEdit={() => {
      setDetailsId(null); setError(""); setEditor({ mode: "edit", draft: { ...details } });
    }} />}
    {discoveryOpen && <SshDiscovery aliases={availableSsh} selectedAliases={selectedAliases} refreshing={discovering} saving={savingAliases} error={error}
      onClose={() => setDiscoveryOpen(false)} onRefresh={() => void discoverSsh().catch(reason => setError((reason as Error).message))} onAddManually={addManually}
      onToggle={alias => setSelectedAliases(selected => selected.includes(alias) ? selected.filter(item => item !== alias) : [...selected, alias])}
      onSave={() => void saveDiscovered()} />}
    {editor && <ConnectionEditor draft={editor.draft} saving={busy === editor.draft.id} error={error} onClose={() => setEditor(null)} onSave={connection => void operate(connection.id, async () => {
      const saved = await api.save(connection); setEditor(null);
      if (editor.mode === "add" && saved) await api.connect(saved.id);
    })} />}
  </>;
};

function ConnectionDetails({ connection, editDisabled, onClose, onEdit }: { connection: RemoteDesktopConnection; editDisabled: boolean; onClose(): void; onEdit(): void }) {
  const { t: tr } = useI18n();
  const focus = useFocusTrap(true);
  const fields: Array<{ label: string; value: string | null | undefined }> = connection.kind === "wsl" ? [{ label: tr("发行版"), value: connection.distribution }] : [
    ...(connection.source === "discovered" && connection.alias?.trim() ? [{ label: tr("别名"), value: connection.alias.trim() }] : []),
    { label: tr("主机"), value: connection.sshHost },
    { label: tr("端口"), value: connection.sshPort == null ? null : String(connection.sshPort) },
    { label: tr("身份"), value: connection.identity },
  ];
  const version = connection.appServerVersion?.trim();
  if (connection.status === "connected" && version) fields.push({ label: tr("版本"), value: version });
  const status = connection.status === "connected" ? tr("已连接") : connection.status === "connecting" ? tr("正在连接") : tr("未连接");
  return createPortal(<div ref={focus} className="mc-dialog-backdrop remote-connection-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="mc-dialog-panel remote-connection-dialog remote-connection-details" role="dialog" aria-modal="true" aria-label={connection.displayName}
      onKeyDown={event => { if (event.key === "Escape") { event.stopPropagation(); onClose(); } }}>
      <header><h3>{connection.displayName}</h3><div className="remote-connection-details-actions">
        {connection.kind === "ssh" && <button type="button" className="mc-icon-btn" aria-label={tr("编辑连接")} disabled={editDisabled} onClick={onEdit}><Pencil size={16} /></button>}
        <button type="button" className="mc-icon-btn" aria-label={tr("关闭")} onClick={onClose}><X size={16} /></button>
      </div></header>
      <div className="remote-connection-details-kind"><span className="remote-connection-details-status" data-connected={connection.status === "connected"} role="img" aria-label={status} title={status} />{connection.kind === "wsl" ? "WSL" : "SSH"}</div>
      <dl className="remote-connection-details-fields">{fields.map(({ label, value }) => <div className="remote-connection-detail-row" key={label}>
        <dt>{label}</dt><dd>{value == null ? <span>—</span> : <button type="button" className="remote-connection-detail-value" aria-label={tr("复制 {label}", { label })} title={value} onClick={() => {
          void copyText(value, label).then(copied => { if (copied) pushToast(tr("已复制到剪贴板"), "info"); });
        }}>{value.trim() || "—"}</button>}</dd>
      </div>)}</dl>
    </section>
  </div>, document.body);
}

function SshDiscovery({ aliases, selectedAliases, refreshing, saving, error, onClose, onRefresh, onAddManually, onToggle, onSave }: {
  aliases: string[]; selectedAliases: string[]; refreshing: boolean; saving: boolean; error: string;
  onClose(): void; onRefresh(): void; onAddManually(): void; onToggle(alias: string): void; onSave(): void;
}) {
  const { t: tr } = useI18n();
  const focus = useFocusTrap(true);
  return createPortal(<div ref={focus} className="mc-dialog-backdrop remote-connection-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <form className="mc-dialog-panel remote-connection-dialog remote-connection-discovery" role="dialog" aria-modal="true" aria-label={tr("添加 SSH 连接")}
      onKeyDown={event => { if (event.key === "Escape") { event.stopPropagation(); onClose(); } }} onSubmit={event => { event.preventDefault(); onSave(); }}>
      <header><h3>{tr("添加 SSH 连接")}</h3><button type="button" className="mc-icon-btn" aria-label={tr("关闭")} onClick={onClose}><X size={16} /></button></header>
      <div className="remote-connection-discovery-list">
        {aliases.length === 0 ? <p className="remote-connection-discovery-empty">{tr("未找到 SSH 连接")}</p> : aliases.map(alias => <label className="remote-connection-discovery-row" key={alias}>
          <Monitor size={20} aria-hidden="true" /><span>{alias}</span><input type="checkbox" aria-label={tr("选择 {name}", { name: alias })} checked={selectedAliases.includes(alias)} disabled={saving} onChange={() => onToggle(alias)} />
        </label>)}
      </div>
      {error && <p className="settings-note remote-connection-error" role="alert">{error}</p>}
      <footer><button type="button" className="mc-icon-btn" aria-label={tr("刷新")} disabled={refreshing || saving} aria-busy={refreshing} onClick={onRefresh}><RefreshCw size={16} className={refreshing ? "remote-connection-refreshing" : undefined} /></button>
        <button type="button" className="remote-connection-add-manually" onClick={onAddManually}>{tr("手动添加")}</button>
        <button type="submit" className="settings-action-button" data-primary="true" disabled={saving || !aliases.some(alias => selectedAliases.includes(alias))}>{tr("添加")}</button></footer>
    </form>
  </div>, document.body);
}

function ConnectionEditor({ draft, saving, error, onClose, onSave }: { draft: RemoteDesktopConnection; saving: boolean; error: string; onClose(): void; onSave(value: RemoteDesktopConnection): void }) {
  const { t: tr } = useI18n();
  const discovered = draft.source === "discovered";
  const [name, setName] = useState(draft.displayName), [host, setHost] = useState((discovered ? draft.alias : draft.sshHost) || ""), [port, setPort] = useState(draft.sshPort?.toString() || "");
  const [identity, setIdentity] = useState(draft.identity || ""), [auth, setAuth] = useState(draft.identity ? "identity" : "default");
  const focus = useFocusTrap(true);
  return createPortal(<div ref={focus} className="mc-dialog-backdrop remote-connection-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !saving) onClose(); }}>
    <form className="mc-dialog-panel remote-connection-dialog" role="dialog" aria-modal="true" aria-label={tr("SSH 连接")} onKeyDown={event => { if (event.key === "Escape" && !saving) { event.stopPropagation(); onClose(); } }} onSubmit={event => {
      event.preventDefault(); onSave(discovered ? { ...draft, displayName: name.trim() } : { ...draft, source: "manual", alias: undefined, displayName: name.trim(), sshHost: host.trim(), sshPort: port ? Number(port) : null, identity: auth === "identity" ? identity.trim() : null });
    }}>
      <header><h3>{tr("SSH 连接")}</h3><button type="button" className="mc-icon-btn" aria-label={tr("关闭")} disabled={saving} onClick={onClose}><X size={16} /></button></header>
      <label>{tr("显示名称")}<input required autoFocus disabled={saving} value={name} onChange={event => setName(event.target.value)} /></label>
      <div className="remote-connection-host-fields"><label>{tr(discovered ? "SSH 别名" : "主机名或 SSH 别名")}<input required disabled={saving || discovered} value={host} onChange={event => setHost(event.target.value)} placeholder="user@hostname" /></label>
        {!discovered && <label>{tr("端口")}<input type="number" min={1} max={65535} disabled={saving} value={port} placeholder="22" onChange={event => setPort(event.target.value)} /></label>}</div>
      {!discovered && <label>{tr("身份验证")}<SelectMenu value={auth} onValueChange={setAuth} disabled={saving} ariaLabel={tr("身份验证")} className="settings-select"><option value="default">{tr("默认 SSH 配置")}</option><option value="identity">{tr("身份文件")}</option></SelectMenu></label>}
      {!discovered && auth === "identity" && <label>{tr("身份文件路径")}<input required disabled={saving} value={identity} onChange={event => setIdentity(event.target.value)} /></label>}
      {error && <p className="settings-note" role="alert">{error}</p>}
      <footer><button type="button" className="settings-action-button" disabled={saving} onClick={onClose}>{tr("取消")}</button><button type="submit" className="settings-action-button" data-primary="true" disabled={saving}>{saving ? tr("正在保存…") : tr("保存")}</button></footer>
    </form>
  </div>, document.body);
}
