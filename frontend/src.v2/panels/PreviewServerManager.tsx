import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { FileCog, Play, RefreshCw, RotateCcw, Server, ShieldCheck, Square, X } from "lucide-react";
import { useAppStore } from "../stores";
import { isDesktop } from "../desktop/runtime";
import { selectPreviewForConversation } from "../lib/preview-projection";
import {
  openPreviewLaunchConfiguration, previewServiceScopeIsCurrent, restartPreviewService,
  runPreviewServiceCommand, type PreviewServiceScope,
} from "../lib/preview-server-actions";
import type { PreviewLaunchProcessInfo } from "../protocol/events";
import { pushToast } from "../overlays/ToastContainer";
import { workspaceRootsEqual } from "../lib/workspace-path";
import "./PreviewServerManager.css";

const processLabel = (process?: PreviewLaunchProcessInfo): string => {
  if (!process) return "未启动";
  if (process.cleanup_pending) return "停止尚未完成";
  return ({ starting: "进程已启动，等待服务", running: "进程运行中", ready: "已提供服务网址", stopping: "正在停止", exited: "已停止", crashed: "进程异常退出", unhealthy: "服务状态异常" })[process.status];
};

export const PreviewServerManager = () => {
  const conversationId = useAppStore((state) => state.conversationId) || "";
  const workspaceRoot = useAppStore((state) => state.workingDirectory);
  const connected = useAppStore((state) => state.isConnected);
  const openRequest = useAppStore((state) => state.previewServiceManagerRequest);
  const processes = useAppStore((state) => selectPreviewForConversation(state, conversationId).previewLaunchProcesses);
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [position, setPosition] = useState({ left: 12, top: 12 });
  const scopeKey = JSON.stringify([conversationId, workspaceRoot]);
  const show = useCallback(() => {
    const rect = triggerRef.current!.getBoundingClientRect();
    setPosition({ left: Math.max(12, Math.min(rect.right - 420, window.innerWidth - 432)), top: Math.max(12, Math.min(rect.bottom + 6, window.innerHeight - 360)) });
    setOpen(true);
  }, []);
  useEffect(() => { setOpen(false); }, [scopeKey]);
  useEffect(() => {
    if (!openRequest) return;
    if (openRequest.conversationId === conversationId && workspaceRootsEqual(openRequest.workspaceRoot, workspaceRoot)) show();
    useAppStore.setState({ previewServiceManagerRequest: null });
  }, [openRequest, conversationId, workspaceRoot, show]);
  const close = useCallback(() => { setOpen(false); triggerRef.current?.focus(); }, []);
  const activeCount = processes.filter((process) => !["exited", "crashed"].includes(process.status) || process.cleanup_pending).length;
  return <>
    <button ref={triggerRef} type="button" className="mc-preview-services-trigger" aria-label="管理预览服务" title="管理预览服务"
      aria-haspopup="dialog" aria-expanded={open} onClick={() => open ? close() : show()}>
      <Server size={15} /><span>服务{activeCount ? ` ${activeCount}` : ""}</span>
    </button>
    {open && createPortal(<PreviewServicesMenu key={scopeKey} scope={{ conversation_id: conversationId, workspace_root: workspaceRoot }} connected={connected}
      position={position} trigger={triggerRef.current!} onClose={close} />, document.body)}
  </>;
};

const PreviewServicesMenu = ({ scope, connected, position, trigger, onClose }: {
  scope: PreviewServiceScope; connected: boolean; position: { left: number; top: number }; trigger: HTMLElement; onClose: () => void;
}) => {
  const configs = useAppStore((state) => selectPreviewForConversation(state, scope.conversation_id).previewLaunchConfigs);
  const processes = useAppStore((state) => selectPreviewForConversation(state, scope.conversation_id).previewLaunchProcesses);
  const verification = useAppStore((state) => selectPreviewForConversation(state, scope.conversation_id).previewVerification);
  const [loading, setLoading] = useState(Boolean(scope.conversation_id && scope.workspace_root && connected));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [selectedName, setSelectedName] = useState("");
  const [stoppedLogs, setStoppedLogs] = useState<Record<string, PreviewLaunchProcessInfo>>({});
  const menuRef = useRef<HTMLElement>(null);
  const scoped = Boolean(scope.conversation_id && scope.workspace_root);
  const reload = useCallback(async () => {
    setLoading(true); setError("");
    try { await runPreviewServiceCommand({ type: "preview.launch.config", ...scope }); }
    catch (failure) { if (previewServiceScopeIsCurrent(scope)) setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { setLoading(false); }
  }, [scope.conversation_id, scope.workspace_root]);
  useEffect(() => { if (scoped && connected) void reload(); }, [scoped, connected, reload]);
  useLayoutEffect(() => { menuRef.current!.focus(); }, []);
  useEffect(() => {
    const outside = (event: PointerEvent) => { if (!menuRef.current!.contains(event.target as Node) && !trigger.contains(event.target as Node)) onClose(); };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [trigger, onClose]);
  const rows = useMemo(() => [
    ...configs.map((config) => ({ config, process: processes.find((process) => process.name === config.name), name: config.name })),
    ...processes.filter((process) => !configs.some((config) => config.name === process.name)).map((process) => ({ config: undefined, process, name: process.name })),
  ], [configs, processes]);
  const selected = rows.find((row) => row.name === selectedName) || rows[0];
  const logProcess = selected && (selected.process || stoppedLogs[selected.name]);
  const logLines = logProcess?.output_tail?.length ? logProcess.output_tail : (logProcess?.stderr_tail || []).map((line) => ({ stream: "stderr" as const, line }));
  const rememberStopped = (stopped: PreviewLaunchProcessInfo[] = []) => setStoppedLogs((previous) => ({ ...previous, ...Object.fromEntries(stopped.map((process) => [process.name, process])) }));
  const act = async (action: "start" | "stop" | "restart" | "verify", name: string, process?: PreviewLaunchProcessInfo) => {
    setBusy(`${action}:${name}`); setError(""); setSelectedName(name);
    try {
      if (action === "restart") {
        if (process) rememberStopped([process]);
        await restartPreviewService(scope, name);
      } else {
        const result = await runPreviewServiceCommand(action === "verify"
          ? { type: "preview.verify", ...scope, url: process!.url }
          : { type: action === "stop" ? "preview.launch.stop" : "preview.launch.start", ...scope, name });
        if (action === "stop") rememberStopped(result.data?.stopped);
      }
      if (previewServiceScopeIsCurrent(scope) && action !== "verify") await reload();
    } catch (failure) { if (previewServiceScopeIsCurrent(scope)) setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { setBusy(""); }
  };
  const editConfiguration = async () => {
    setBusy("config"); setError("");
    try {
      const draft = await openPreviewLaunchConfiguration(scope, configs);
      if (previewServiceScopeIsCurrent(scope)) {
        if (draft) pushToast("配置草稿已打开，请保存后刷新服务列表。", "info");
        onClose();
      }
    } catch (failure) { if (previewServiceScopeIsCurrent(scope)) setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { setBusy(""); }
  };
  const openService = (process: PreviewLaunchProcessInfo) => {
    if (isDesktop()) useAppStore.getState().openLivePreview(process.url, scope.conversation_id);
    else window.open(process.url, "_blank", "noopener,noreferrer");
    onClose();
  };
  return <section ref={menuRef} role="dialog" aria-label="预览服务管理" tabIndex={-1} className="mc-preview-services-menu" style={{ ...position, maxHeight: `calc(100vh - ${position.top + 12}px)` }}
    onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); } }}>
    <header><div><strong>预览服务</strong><span>{scope.workspace_root || "先打开项目工作区"}</span></div><button type="button" aria-label="关闭服务菜单" onClick={onClose}><X size={16} /></button></header>
    <div className="mc-preview-services-toolbar">
      <button type="button" disabled={!scoped || !connected || loading || Boolean(busy)} onClick={() => void reload()}><RefreshCw size={14} className={loading ? "mc-browser-spin" : undefined} />刷新服务</button>
      <button type="button" disabled={!scoped || Boolean(busy)} onClick={() => void editConfiguration()}><FileCog size={14} />编辑 launch.json</button>
    </div>
    {!scoped ? <p className="mc-preview-services-note">选择项目会话后即可管理服务。</p> : !connected ? <p className="mc-preview-services-note">后端尚未连接，连接后可读取和管理服务。</p> : null}
    {loading && <p role="status" className="mc-preview-services-note">正在读取配置与进程状态…</p>}
    {error && <p role="alert" className="mc-preview-services-error">{error}</p>}
    {!loading && scoped && connected && !rows.length && !error && <p className="mc-preview-services-note">此项目尚无服务配置。打开配置草稿，填写真实启动命令并保存。</p>}
    <div className="mc-preview-services-list">{rows.map(({ config, process, name }) => {
      const active = Boolean(process && (!["exited", "crashed"].includes(process.status) || process.cleanup_pending));
      const checked = process && verification?.url === process.url ? verification : null;
      const readyUrl = Boolean(process?.url && (process.status === "ready" || checked?.ok) && active);
      return <article key={name} data-selected={selected?.name === name}>
        <button type="button" className="mc-preview-service-copy" onClick={() => setSelectedName(name)} aria-label={`查看 ${name} 日志`}>
          <strong>{name}</strong><span>{processLabel(process)}{process?.exit_code != null ? ` · 退出码 ${process.exit_code}` : ""}</span>
          <code>{process?.command || config!.command}</code>{config?.source && <span>配置来源：{config.source}</span>}
        </button>
        {process?.url && <button type="button" className="mc-preview-service-url" disabled={!readyUrl} onClick={() => openService(process)} title={process.url}>{process.url}</button>}
        {process && <span className="mc-preview-service-http" data-ok={checked?.ok ?? false}>{checked ? checked.ok ? `HTTP 已就绪${checked.status_code ? ` · ${checked.status_code}` : ""}` : `HTTP 尚未就绪${checked.status_code ? ` · ${checked.status_code}` : ""}：${checked.error || "未响应"}` : "HTTP 尚未检测"}</span>}
        {(process?.last_error || process?.cleanup_pending) && <p className="mc-preview-services-error">{process.last_error || "旧进程的退出尚未确认，请重试停止后再启动。"}</p>}
        <div className="mc-preview-service-actions">
          {!active && config && <button type="button" disabled={!connected || loading || Boolean(busy)} onClick={() => void act("start", name)}><Play size={13} />{busy === `start:${name}` ? "正在启动…" : "启动"}</button>}
          {active && <button type="button" disabled={!connected || loading || Boolean(busy)} onClick={() => void act("stop", name, process)}><Square size={13} />{busy === `stop:${name}` ? "正在停止…" : "停止"}</button>}
          {active && config && <button type="button" disabled={!connected || loading || Boolean(busy)} onClick={() => void act("restart", name, process)}><RotateCcw size={13} />{busy === `restart:${name}` ? "正在重启…" : "重启"}</button>}
          {active && process?.url && <button type="button" disabled={!connected || loading || Boolean(busy)} onClick={() => void act("verify", name, process)}><ShieldCheck size={13} />{busy === `verify:${name}` ? "检测中…" : "检测 HTTP"}</button>}
        </div>
      </article>;
    })}</div>
    {selected && <section className="mc-preview-service-logs" aria-label={`${selected.name} 服务日志`}><strong>{selected.name} · stdout / stderr</strong>
      {logLines.length ? <pre>{logLines.map((line, index) => <span key={index} data-stream={line.stream}>[{line.stream}] {line.line}{"\n"}</span>)}</pre> : <p className="mc-preview-services-note">暂无输出。启动后会显示实际进程日志。</p>}
    </section>}
  </section>;
};
