import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import type { AppBridge } from "@modelcontextprotocol/ext-apps/app-bridge";
import { Maximize2 } from "../../lib/icons";
import { isDesktop, openExternal } from "../../desktop/runtime";
import { showConfirm } from "../../overlays/DialogService";
import type { McpAppReceipt } from "../../protocol/mcp-app-types";
import { AppFrameTransport, appHostContext, appHostRequest, createAppBridge, moveAppContainer, type McpAppView } from "./app-host";
import "./mcp-apps.css";

export interface McpAppRendererProps {
  app: McpAppReceipt;
  conversationId: string;
  inlineContainer?: HTMLElement | null;
  /** Connected container in the existing right panel; null keeps the App inline. */
  fullscreenContainer?: HTMLElement | null;
  onDisplayModeChange?: (mode: "inline" | "fullscreen", app: McpAppReceipt) => void;
  /** Called only after the human accepts the visible follow-up preview. */
  onFollowUpMessage?: (text: string) => Promise<boolean>;
  onHostReady?: (host: HTMLDivElement | null, parking: HTMLDivElement | null) => void;
  fallback?: ReactNode;
}

export function McpAppRenderer(props: McpAppRendererProps) {
  const { app, conversationId, fullscreenContainer = null, inlineContainer = null } = props;
  const latest = useRef(props); latest.current = props;
  const inline = useRef<HTMLDivElement>(null);
  const live = useRef<{ host: HTMLDivElement; frame: HTMLIFrameElement; bridge: AppBridge; view: McpAppView }>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [closed, setClosed] = useState(false);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let observer: ResizeObserver | undefined;
    let theme: MutationObserver | undefined;
    let active: typeof live.current;
    let closePromise: Promise<void> | undefined;
    const failed = (reason: unknown) => {
      if (!disposed) { setError(reason instanceof Error ? reason.message : String(reason)); setLoading(false); }
    };
    const updateContext = () => {
      if (active) active.bridge.setHostContext(appHostContext(active.view, active.host.parentElement!, Boolean(latest.current.fullscreenContainer)));
    };
    const close = () => closePromise ||= (async () => {
      if (!active) return;
      if (timer) clearTimeout(timer);
      observer?.disconnect(); theme?.disconnect();
      try { await active.bridge.teardownResource({}, { timeout: 30_000 }); }
      finally {
        live.current = undefined;
        await active.bridge.close();
        active.host.remove();
        await appHostRequest(app.id, "close", { conversation_id: conversationId, view_id: active.view.viewId });
      }
    })();
    setError(undefined); setLoading(true); setClosed(false);
    void (async () => {
      const view = await appHostRequest<McpAppView>(app.id, "open", { conversation_id: conversationId });
      if (disposed) {
        await appHostRequest(app.id, "close", { conversation_id: conversationId, view_id: view.viewId });
        return;
      }
      const host = document.createElement("div");
      host.className = `mc-mcp-app-frame-host${view.prefersBorder ? " mc-mcp-app-bordered" : ""}`;
      host.dataset.mcpAppId = app.id;
      const frame = document.createElement("iframe");
      frame.title = app.title; frame.sandbox.add("allow-scripts", "allow-same-origin");
      frame.referrerPolicy = "no-referrer";
      frame.allow = "camera 'none'; microphone 'none'; geolocation 'none'";
      frame.style.height = "320px";
      host.appendChild(frame);
      inline.current!.appendChild(host);
      const bridge = createAppBridge(app, conversationId, view, appHostContext(view, host.parentElement!, false));
      active = { host, frame, bridge, view }; live.current = active;
      latest.current.onHostReady?.(host, inline.current);
      bridge.oninitialized = () => {
        if (timer) clearTimeout(timer);
        void (async () => {
          await bridge.sendToolInput({ arguments: view.toolInput });
          await bridge.sendToolResult(view.toolResult);
          if (!disposed) setLoading(false);
        })().catch(failed);
      };
      bridge.onsizechange = ({ height }) => { if (height !== undefined) frame.style.height = `${Math.max(1, Math.min(height, 600))}px`; };
      bridge.onrequestdisplaymode = async ({ mode }) => {
        const next = mode === "fullscreen" && latest.current.onDisplayModeChange ? "fullscreen" : "inline";
        latest.current.onDisplayModeChange?.(next, app);
        return { mode: next };
      };
      bridge.onopenlink = async ({ url }) => {
        const parsed = new URL(url);
        if (!["https:", "http:", "mailto:"].includes(parsed.protocol)) return { isError: true };
        const accepted = await showConfirm({ title: app.title, message: `打开此链接？\n${url}`, confirmLabel: "打开" });
        if (!accepted) return { isError: true };
        if (isDesktop()) return { isError: !(await openExternal(url)) };
        return { isError: !window.open(url, "_blank", "noopener,noreferrer") };
      };
      bridge.onmessage = async ({ content }) => {
        if (view.readOnly || !latest.current.onFollowUpMessage || content.some(block => block.type !== "text")) return { isError: true };
        const text = content.map(block => block.type === "text" ? block.text : "").join("\n");
        if (!await showConfirm({ title: app.title, message: text, confirmLabel: "发送消息" })) return { isError: true };
        return { isError: !await latest.current.onFollowUpMessage(text) };
      };
      bridge.ondownloadfile = async ({ contents }) => {
        if (!await showConfirm({ title: app.title, message: "允许此 App 下载文件？", confirmLabel: "下载" })) return { isError: true };
        for (const item of contents) {
          const resources = item.type === "resource" ? [item.resource] : (await appHostRequest<{ contents: Array<{ uri: string; mimeType?: string; text?: string; blob?: string }> }>(app.id, "request", {
            conversation_id: conversationId, view_id: view.viewId, method: "resources/read", params: { uri: item.uri },
          })).contents;
          for (const resource of resources) {
            const body = "blob" in resource && resource.blob !== undefined
              ? Uint8Array.from(atob(resource.blob), character => character.charCodeAt(0)) : "text" in resource ? resource.text || "" : "";
            const url = URL.createObjectURL(new Blob([body], { type: resource.mimeType || "application/octet-stream" }));
            const anchor = document.createElement("a"); anchor.href = url;
            anchor.download = item.type === "resource_link" ? item.name : resource.uri.split("/").at(-1) || "download";
            anchor.click(); URL.revokeObjectURL(url);
          }
        }
        return {};
      };
      bridge.onrequestteardown = () => { void close().then(() => { if (!disposed) setClosed(true); }).catch(failed); };
      bridge.onloggingmessage = params => console.info(`[MCP App ${app.server}]`, params.data);
      await bridge.connect(new AppFrameTransport(frame, view.frameOrigin));
      timer = setTimeout(() => failed(new Error("MCP App initialization timed out after 20 seconds")), 20_000);
      observer = new ResizeObserver(updateContext); observer.observe(host);
      theme = new MutationObserver(updateContext); theme.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "lang"] });
      frame.src = view.frameUrl;
      const target = latest.current.fullscreenContainer || latest.current.inlineContainer;
      if (target) { moveAppContainer(host, target); host.classList.toggle("mc-mcp-app-fullscreen", Boolean(latest.current.fullscreenContainer)); updateContext(); }
      if (!latest.current.fullscreenContainer && app.displayMode === "fullscreen") latest.current.onDisplayModeChange?.("fullscreen", app);
    })().catch(failed);
    return () => {
      disposed = true; latest.current.onHostReady?.(null, null);
      live.current = undefined; void close().catch(reason => console.error("MCP App teardown", reason));
    };
  }, [app.id, conversationId]);

  useLayoutEffect(() => {
    const current = live.current;
    if (!current) return;
    moveAppContainer(current.host, fullscreenContainer || inlineContainer || inline.current!);
    current.host.classList.toggle("mc-mcp-app-fullscreen", Boolean(fullscreenContainer));
    current.bridge.setHostContext(appHostContext(current.view, current.host.parentElement!, Boolean(fullscreenContainer)));
  }, [fullscreenContainer, inlineContainer]);

  return <div className="mc-mcp-app" data-app-id={app.id}>
    {loading && !closed && <div className="mc-mcp-app-status">正在加载 {app.title}…</div>}
    {error && <div className="mc-mcp-app-status">{app.title} 无法显示：{error}</div>}
    {error && props.fallback}
    {!loading && !closed && !error && <div className="mc-mcp-app-attribution">{app.server} · {app.title}</div>}
    {!closed && !error && !fullscreenContainer && props.onDisplayModeChange && <button type="button" className="mc-mcp-app-expand" title="在侧面板打开" aria-label={`在侧面板打开 ${app.title}`} onClick={() => props.onDisplayModeChange?.("fullscreen", app)}><Maximize2 size={14} /></button>}
    <div ref={inline} className="mc-mcp-app-inline" hidden={Boolean(error) || closed || Boolean(fullscreenContainer)} />
  </div>;
}
