import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Laptop, Monitor, MoreHorizontal, Plus, Server, X } from "lucide-react";
import { desktop, type RemoteDesktopConnection } from "../desktop/runtime";
import { useI18n } from "../i18n";
import { ContextMenu } from "../components/ContextMenu";
import { SelectMenu } from "../components/SelectMenu";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { showConfirm } from "./DialogService";
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
  const [draft, setDraft] = useState<RemoteDesktopConnection | null>(null);
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
    void api.discover().then(items => { if (!cancelled) { setDiscovery(items); setError(""); } },
      (reason: Error) => { if (!cancelled) setError(reason.message); });
    return () => { cancelled = true; };
  }, [active, kind, api]);
  const operate = async (id: string, operation: () => Promise<unknown>) => {
    setBusy(id); setError("");
    try { await operation(); }
    catch (reason) { setError((reason as Error).message); }
    finally { setBusy(null); }
  };
  const add = () => { setError(""); setDraft({ id: crypto.randomUUID(), displayName: "", kind: "ssh", sshHost: "", sshPort: null, identity: "" }); };
  const remove = async (connection: RemoteDesktopConnection) => {
    if (await showConfirm({ title: tr("移除连接"), message: tr("移除“{name}”的连接配置？", { name: connection.displayName }), confirmLabel: tr("移除") })) {
      await operate(connection.id, () => api!.remove(connection.id));
    }
  };
  if (!api) return <p className="settings-note">{tr("请在 MiniCode 桌面端管理连接。")}</p>;
  const visible = connections.filter(connection => connection.kind === kind);
  const availableWsl = discovery?.wslDistributions.filter(name => !connections.some(connection => connection.kind === "wsl" && connection.distribution === name)) || [];
  return <>
    <nav className="settings-integration-tabs" aria-label={tr("连接类型")}>
      <button type="button" aria-pressed={kind === "ssh"} onClick={() => setKind("ssh")}>SSH</button>
      {desktop()?.platformInfo.platform === "win32" && <button type="button" aria-pressed={kind === "wsl"} onClick={() => setKind("wsl")}>WSL</button>}
    </nav>
    <section className="settings-group remote-connections">
      <header className="remote-connections-heading"><h3>{kind === "ssh" ? tr("此电脑的 SSH 连接") : tr("WSL 连接")}</h3>
        {kind === "ssh" && visible.length > 0 && <button type="button" className="settings-action-button" onClick={add}><Plus size={14} />{tr("添加")}</button>}</header>
      {loading ? <p className="settings-note">{tr("正在读取连接…")}</p> : visible.length === 0 && availableWsl.length === 0 && <div className="remote-connections-empty">
        <div className="remote-connections-empty-icon" aria-hidden="true"><Laptop size={24} /><MoreHorizontal size={14} /><Server size={24} /></div><p>{kind === "ssh" ? tr("通过 SSH 连接到远程设备") : tr("未发现 WSL 连接")}</p>
        {kind === "ssh" && <button type="button" className="settings-action-button" data-primary="true" onClick={add}>{tr("添加")}</button>}</div>}
      {visible.map(connection => <div className="settings-row remote-connection-row" key={connection.id}>
        <Monitor size={20} aria-hidden="true" /><div className="settings-row-copy"><div className="settings-row-title">{connection.displayName}</div>
          <div className="settings-row-description" title={connection.sshHost || connection.distribution}>{connection.kind === "ssh" ? `${connection.sshHost}${connection.sshPort ? `:${connection.sshPort}` : ""}` : connection.distribution}</div></div>
        <div className="settings-row-control remote-connection-actions"><span className="remote-connection-status">{connection.status === "connected" ? tr("已连接") : connection.status === "connecting" ? tr("正在连接") : tr("未连接")}</span>
          <button type="button" className="settings-action-button" disabled={busy === connection.id || connection.status === "connecting"} onClick={() => void operate(connection.id, () => api.open(connection.id))}>{busy === connection.id ? tr("正在连接") : connection.status === "connected" ? tr("打开") : tr("连接")}</button>
          <button type="button" className="mc-icon-btn" aria-label={tr("{name}的连接操作", { name: connection.displayName })} aria-haspopup="menu" onClick={event => setMenu({ connection, anchor: event.currentTarget })}><MoreHorizontal size={16} /></button></div>
      </div>)}
      {availableWsl.map(name => <div className="settings-row remote-connection-row" key={name}><Monitor size={20} /><div className="settings-row-copy"><div className="settings-row-title">{name}</div></div>
        <button type="button" className="settings-action-button" disabled={busy !== null} onClick={() => void operate(name, async () => {
          const connection = await api.save({ id: crypto.randomUUID(), displayName: name, kind: "wsl", distribution: name }); await api.open(connection.id);
        })}>{tr("连接")}</button></div>)}
      {error && !draft && <p className="settings-note remote-connection-error" role="alert">{error}</p>}
    </section>
    {menu && <ContextMenu anchor={menu.anchor} position={{ x: menu.anchor.getBoundingClientRect().left, y: menu.anchor.getBoundingClientRect().bottom }} onClose={() => setMenu(null)} items={[
      ...(menu.connection.status === "connected" ? [{ label: tr("断开连接"), onClick: () => void operate(menu.connection.id, () => api.disconnect(menu.connection.id)) }] : []),
      ...(menu.connection.kind === "ssh" ? [{ label: tr("编辑连接"), disabled: menu.connection.status !== "disconnected", onClick: () => { setError(""); setDraft({ ...menu.connection }); } }] : []),
      { label: tr("移除连接"), disabled: menu.connection.status !== "disconnected", onClick: () => void remove(menu.connection) },
    ]} />}
    {draft && <ConnectionEditor draft={draft} saving={busy === draft.id} error={error} onClose={() => setDraft(null)} onSave={connection => void operate(connection.id, async () => { await api.save(connection); setDraft(null); })} />}
  </>;
};

function ConnectionEditor({ draft, saving, error, onClose, onSave }: { draft: RemoteDesktopConnection; saving: boolean; error: string; onClose(): void; onSave(value: RemoteDesktopConnection): void }) {
  const { t: tr } = useI18n();
  const [name, setName] = useState(draft.displayName), [host, setHost] = useState(draft.sshHost || ""), [port, setPort] = useState(draft.sshPort?.toString() || "");
  const [identity, setIdentity] = useState(draft.identity || ""), [auth, setAuth] = useState(draft.identity ? "identity" : "default");
  const focus = useFocusTrap(true);
  return createPortal(<div ref={focus} className="mc-dialog-backdrop remote-connection-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !saving) onClose(); }}>
    <form className="mc-dialog-panel remote-connection-dialog" role="dialog" aria-modal="true" aria-label={tr("SSH 连接")} onKeyDown={event => { if (event.key === "Escape" && !saving) { event.stopPropagation(); onClose(); } }} onSubmit={event => {
      event.preventDefault(); onSave({ ...draft, displayName: name.trim(), sshHost: host.trim(), sshPort: port ? Number(port) : null, identity: auth === "identity" ? identity.trim() : null });
    }}>
      <header><h3>{tr("SSH 连接")}</h3><button type="button" className="mc-icon-btn" aria-label={tr("关闭")} disabled={saving} onClick={onClose}><X size={16} /></button></header>
      <label>{tr("显示名称")}<input required autoFocus value={name} onChange={event => setName(event.target.value)} /></label>
      <div className="remote-connection-host-fields"><label>{tr("主机名或 SSH 别名")}<input required value={host} onChange={event => setHost(event.target.value)} placeholder="user@hostname" /></label>
        <label>{tr("端口")}<input type="number" min={1} max={65535} value={port} placeholder="22" onChange={event => setPort(event.target.value)} /></label></div>
      <label>{tr("身份验证")}<SelectMenu value={auth} onValueChange={setAuth} ariaLabel={tr("身份验证")} className="settings-select"><option value="default">{tr("默认 SSH 配置")}</option><option value="identity">{tr("身份文件")}</option></SelectMenu></label>
      {auth === "identity" && <label>{tr("身份文件路径")}<input required value={identity} onChange={event => setIdentity(event.target.value)} /></label>}
      {error && <p className="settings-note" role="alert">{error}</p>}
      <footer><button type="button" className="settings-action-button" disabled={saving} onClick={onClose}>{tr("取消")}</button><button type="submit" className="settings-action-button" data-primary="true" disabled={saving}>{saving ? tr("正在保存…") : tr("保存")}</button></footer>
    </form>
  </div>, document.body);
}
