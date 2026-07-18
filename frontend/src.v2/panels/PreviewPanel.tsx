import { Check, ChevronDown, ClipboardCopy, Globe, RefreshCw, FileText, X, ExternalLink, Play, Square, Maximize2, Minimize2 } from "lucide-react";
import { useState, useEffect, useRef, type MutableRefObject } from "react";
import { useAppStore } from "../stores";
import type { PreviewLaunchConfigInfo, PreviewLaunchProcessInfo, PreviewVerificationInfo } from "../stores/types";
import { getWebSocket } from "../hooks/useWebSocket";
import { openExternal as openExternalTarget } from "../desktop/runtime";
import { openWebInPreview } from "../chat/openWebInPreview";

const isLikelyJson = (content: string) => {
  const trimmed = content.trim();
  return (trimmed.startsWith("{") && trimmed.endsWith("}")) || (trimmed.startsWith("[") && trimmed.endsWith("]"));
};

const prettyContent = (content: string) => {
  if (!isLikelyJson(content)) return content;
  try {
    return JSON.stringify(JSON.parse(content), null, 2);
  } catch {
    return content;
  }
};

const isLocalPreviewUrl = (url: string | null): boolean => {
  if (!url) return true;
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase();
    return host === "localhost" || host === "127.0.0.1" || host === "0.0.0.0" || host === "::1" || host.endsWith(".localhost");
  } catch {
    return false;
  }
};

const previewSandboxForUrl = (url: string | null): string =>
  isLocalPreviewUrl(url)
    ? "allow-scripts allow-forms allow-same-origin allow-popups allow-modals"
    : "allow-scripts allow-forms allow-popups allow-modals";

const PREVIEW_REFERRER_POLICY = "no-referrer";
const ARTIFACT_FRAME_SANDBOX = "allow-scripts allow-forms";

const isSafePreviewImageUrl = (url: string): boolean => {
  const trimmed = url.trim();
  if (/^data:image\/(?:png|jpe?g|gif|webp|avif);base64,[a-z0-9+/=\s]+$/i.test(trimmed)) return true;
  try {
    const parsed = new URL(trimmed);
    return ["http:", "https:", "file:", "blob:"].includes(parsed.protocol);
  } catch {
    return false;
  }
};

const isSafePreviewImageArtifact = (mediaType?: string, url?: string): boolean =>
  Boolean(
    mediaType?.startsWith("image/") &&
    mediaType.toLowerCase() !== "image/svg+xml" &&
    url &&
    isSafePreviewImageUrl(url),
  );

type PreviewMode = "live" | "artifact";
type PreviewZoom = "fit" | number;

const ZOOM_OPTIONS: { value: PreviewZoom; label: string }[] = [
  { value: "fit", label: "Fit" },
  { value: 0.5, label: "50%" },
  { value: 0.75, label: "75%" },
  { value: 1, label: "100%" },
  { value: 1.25, label: "125%" },
  { value: 1.5, label: "150%" },
];

const zoomOptionValue = (zoom: PreviewZoom): string => String(zoom);
const zoomLabel = (zoom: PreviewZoom): string =>
  ZOOM_OPTIONS.find((option) => zoomOptionValue(option.value) === zoomOptionValue(zoom))?.label
  ?? (zoom === "fit" ? "Fit" : `${Math.round(zoom * 100)}%`);

export const PreviewPanel = () => {
  const previewArtifact = useAppStore((s) => s.previewArtifact);
  const livePreviewUrl = useAppStore((s) => s.livePreviewUrl);
  const previewServers = useAppStore((s) => s.previewServers);
  const previewLaunchConfigs = useAppStore((s) => s.previewLaunchConfigs);
  const previewLaunchProcesses = useAppStore((s) => s.previewLaunchProcesses);
  const previewVerification = useAppStore((s) => s.previewVerification);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const setLivePreviewUrl = useAppStore((s) => s.setLivePreviewUrl);

  const [mode, setMode] = useState<PreviewMode>(livePreviewUrl ? "live" : previewArtifact ? "artifact" : "live");
  const [urlInput, setUrlInput] = useState(livePreviewUrl ?? "");
  const [logsOpen, setLogsOpen] = useState(false);
  const [iframeKey, setIframeKey] = useState(0);
  const [refreshFlash, setRefreshFlash] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [zoom, setZoom] = useState<PreviewZoom>("fit");
  const [ctrlPressed, setCtrlPressed] = useState(false);
  const iframeRef = useRef<HTMLIFrameElement | null>(null);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Control") setCtrlPressed(true);
    };
    const handleKeyUp = (e: KeyboardEvent) => {
      if (e.key === "Control") setCtrlPressed(false);
    };
    const handleBlur = () => setCtrlPressed(false);

    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener("keyup", handleKeyUp);
    window.addEventListener("blur", handleBlur);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("keyup", handleKeyUp);
      window.removeEventListener("blur", handleBlur);
    };
  }, []);

  useEffect(() => {
    if (livePreviewUrl) {
      setUrlInput(livePreviewUrl);
      setMode("live");
    } else if (previewArtifact) {
      setMode("artifact");
    }
  }, [livePreviewUrl, previewArtifact]);

  useEffect(() => {
    getWebSocket()?.send({ type: "preview.launch.config", workspace_root: workingDirectory || undefined });
  }, [workingDirectory]);

  useEffect(() => {
    if (!expanded) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setExpanded(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [expanded]);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const onAutoRefresh = () => {
      if (!livePreviewUrl) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        setIframeKey((k) => k + 1);
        setRefreshFlash(true);
        setTimeout(() => setRefreshFlash(false), 600);
        setTimeout(() => {
          getWebSocket()?.send({ type: "preview.verify", url: livePreviewUrl });
        }, 1500);
      }, 500);
    };
    window.addEventListener("preview:auto-refresh", onAutoRefresh);
    return () => {
      window.removeEventListener("preview:auto-refresh", onAutoRefresh);
      if (timer) clearTimeout(timer);
    };
  }, [livePreviewUrl]);

  const handleNavigate = (url: string) => {
    const normalized = url.trim();
    if (!normalized) {
      setLivePreviewUrl(null);
      return;
    }
    const withProtocol = /^https?:\/\//i.test(normalized) ? normalized : `http://${normalized}`;
    openWebInPreview(withProtocol);
  };

  const handleDetect = () => {
    useAppStore.getState().setPreviewServers([]);
    getWebSocket()?.send({ type: "preview.detect" });
  };

  const handleLaunch = (name?: string) => {
    getWebSocket()?.send({ type: "preview.launch.start", name, workspace_root: workingDirectory || undefined });
  };

  const handleStopLaunch = (name?: string) => {
    getWebSocket()?.send({ type: "preview.launch.stop", name });
  };

  const handleRefresh = () => {
    setIframeKey((k) => k + 1);
    getWebSocket()?.send({ type: "preview.refresh", url: livePreviewUrl ?? undefined });
    if (livePreviewUrl) getWebSocket()?.send({ type: "preview.verify", url: livePreviewUrl });
  };

  const openExternal = () => {
    if (!livePreviewUrl) return;
    const opened = openExternalTarget(livePreviewUrl);
    if (!opened) {
      window.open(livePreviewUrl, "_blank", "noopener,noreferrer");
      return;
    }
    void opened.catch(() => undefined);
  };

  return (
    <div className="flex flex-col flex-1 min-h-0 min-w-0">
      <ModeBar mode={mode} setMode={setMode} hasArtifact={!!previewArtifact} hasUrl={!!livePreviewUrl} />
      {mode === "live" ? (
        <LiveView
          urlInput={urlInput}
          setUrlInput={setUrlInput}
          livePreviewUrl={livePreviewUrl}
          previewServers={previewServers}
          previewLaunchConfigs={previewLaunchConfigs}
          previewLaunchProcesses={previewLaunchProcesses}
          previewVerification={previewVerification}
          iframeKey={iframeKey}
          iframeRef={iframeRef}
          onNavigate={handleNavigate}
          onDetect={handleDetect}
          onLaunch={handleLaunch}
          onStopLaunch={handleStopLaunch}
          onRefresh={handleRefresh}
          onOpenExternal={openExternal}
          onClear={() => {
            setLivePreviewUrl(null);
            setUrlInput("");
            setExpanded(false);
          }}
          logsOpen={logsOpen}
          onToggleLogs={() => setLogsOpen((open) => !open)}
          refreshFlash={refreshFlash}
          onExpand={() => setExpanded(true)}
          zoom={zoom}
          onZoom={setZoom}
          ctrlPressed={ctrlPressed}
        />
      ) : (
        <ArtifactView />
      )}
      {expanded && mode === "live" && (
        <ExpandedLivePreview
          livePreviewUrl={livePreviewUrl}
          urlInput={urlInput}
          setUrlInput={setUrlInput}
          iframeKey={iframeKey}
          onNavigate={handleNavigate}
          onRefresh={handleRefresh}
          onOpenExternal={openExternal}
          onClose={() => setExpanded(false)}
          zoom={zoom}
          onZoom={setZoom}
          ctrlPressed={ctrlPressed}
        />
      )}
    </div>
  );
};

const ModeBar = ({
  mode,
  setMode,
  hasArtifact,
  hasUrl,
}: {
  mode: PreviewMode;
  setMode: (m: PreviewMode) => void;
  hasArtifact: boolean;
  hasUrl: boolean;
}) => (
  <div
    className="flex gap-1 px-2 py-1 border-b text-xs"
    style={{
      borderColor: "var(--border-subtle)",
      background: "var(--surface-page)",
      fontSize: "var(--text-xs)",
    }}
  >
    <ModeTab active={mode === "live"} onClick={() => setMode("live")} icon={<Globe size={12} />} label="App" badge={hasUrl ? "active" : undefined} />
    <ModeTab active={mode === "artifact"} onClick={() => setMode("artifact")} icon={<FileText size={12} />} label="文件" badge={hasArtifact ? "可查看" : undefined} />
  </div>
);

const ModeTab = ({
  active,
  onClick,
  icon,
  label,
  badge,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  badge?: string;
}) => (
  <button
    type="button"
    onClick={onClick}
    className="inline-flex items-center gap-1 px-2 py-0.5 border-0 cursor-pointer text-xs"
    style={{
      borderRadius: "var(--radius-sm, 4px)",
      background: active ? "var(--surface-soft)" : "transparent",
      color: active ? "var(--text-primary)" : "var(--text-muted)",
      fontSize: "var(--text-xs)",
    }}
  >
    {icon}
    {label}
    {badge && <span style={{ color: "var(--accent-primary)" }}>{badge}</span>}
  </button>
);

const ZoomPicker = ({
  zoom,
  onZoom,
  align = "left",
  compactHeight = 24,
}: {
  zoom: PreviewZoom;
  onZoom: (zoom: PreviewZoom) => void;
  align?: "left" | "right";
  compactHeight?: number;
}) => {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return (
    <div ref={ref} style={{ position: "relative", flexShrink: 0 }}>
      <button
        type="button"
        aria-label={`Preview zoom: ${zoomLabel(zoom)}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        title="Preview zoom"
        onClick={() => setOpen((current) => !current)}
        style={zoomTriggerStyle(open, compactHeight)}
      >
        <span>{zoomLabel(zoom)}</span>
        <ChevronDown size={12} style={{ color: "var(--text-muted)" }} />
      </button>
      {open && (
        <div role="listbox" aria-label="Preview zoom" style={zoomMenuStyle(align)}>
          {ZOOM_OPTIONS.map((option) => {
            const active = zoomOptionValue(option.value) === zoomOptionValue(zoom);
            return (
              <button
                key={zoomOptionValue(option.value)}
                type="button"
                role="option"
                aria-selected={active}
                onClick={() => {
                  onZoom(option.value);
                  setOpen(false);
                }}
                style={zoomOptionStyle(active)}
              >
                <span style={{ width: 16, display: "inline-flex", justifyContent: "center", color: active ? "var(--accent-primary)" : "transparent" }}>
                  <Check size={12} />
                </span>
                <span>{option.label}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
};

const LiveView = ({
  urlInput,
  setUrlInput,
  livePreviewUrl,
  previewServers,
  previewLaunchConfigs,
  previewLaunchProcesses,
  previewVerification,
  iframeKey,
  iframeRef,
  onNavigate,
  onDetect,
  onLaunch,
  onStopLaunch,
  onRefresh,
  onOpenExternal,
  onClear,
  logsOpen,
  onToggleLogs,
  refreshFlash,
  onZoom,
  ctrlPressed,
  zoom,
  onExpand,
}: {
  urlInput: string;
  setUrlInput: (s: string) => void;
  livePreviewUrl: string | null;
  previewServers: { port: number; url: string; name: string; framework?: string }[];
  previewLaunchConfigs: PreviewLaunchConfigInfo[];
  previewLaunchProcesses: PreviewLaunchProcessInfo[];
  previewVerification: PreviewVerificationInfo | null;
  iframeKey: number;
  iframeRef: MutableRefObject<HTMLIFrameElement | null>;
  onNavigate: (u: string) => void;
  onDetect: () => void;
  onLaunch: (name?: string) => void;
  onStopLaunch: (name?: string) => void;
  onRefresh: () => void;
  onOpenExternal: () => void;
  onClear: () => void;
  logsOpen: boolean;
  onToggleLogs: () => void;
  refreshFlash: boolean;
  onExpand: () => void;
  zoom: "fit" | number;
  onZoom: (z: PreviewZoom) => void;
  ctrlPressed: boolean;
}) => {
  const visibleVerification = previewVerification?.url === livePreviewUrl ? previewVerification : null;
  const activeLaunchProcess = previewLaunchProcesses.find((process) => process.url === livePreviewUrl)
    ?? previewLaunchProcesses.find((process) => process.status === "ready" || process.status === "running" || process.status === "starting")
    ?? previewLaunchProcesses[0];
  const outputTail = activeLaunchProcess?.output_tail ?? [];
  const localPreview = isLocalPreviewUrl(livePreviewUrl);

  const handleWheel = (e: React.WheelEvent) => {
    if (!ctrlPressed && !e.ctrlKey) return;
    e.preventDefault();
    const currentZoom = zoom === "fit" ? 1.0 : zoom;
    const zoomStep = 0.08;
    let nextZoom = currentZoom - Math.sign(e.deltaY) * zoomStep;
    nextZoom = Math.min(Math.max(nextZoom, 0.25), 3.0);
    nextZoom = Math.round(nextZoom * 100) / 100;
    onZoom(nextZoom);
  };
  return (
  <>
    <div
      className="flex items-center flex-wrap gap-1.5 px-2 py-1.5 border-b overflow-hidden"
      style={{
        borderColor: "var(--border-subtle)",
        background: "var(--surface-page)",
      }}
    >
      <button
        type="button"
        title="Refresh app preview"
        aria-label="Refresh app preview"
        onClick={onRefresh}
        disabled={!livePreviewUrl}
        style={iconBtnStyle(!livePreviewUrl)}
      >
        <RefreshCw size={13} />
      </button>
      <input
        type="text"
        aria-label="Preview URL"
        placeholder="http://localhost:3000"
        value={urlInput}
        onChange={(e) => setUrlInput(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") onNavigate(urlInput);
        }}
        spellCheck={false}
        className="flex-1 min-w-0 h-6 px-2 border outline-none text-xs"
        style={{
          flexBasis: "150px",
          borderColor: "var(--border-subtle)",
          borderRadius: "var(--radius-sm, 4px)",
          background: "var(--surface-base)",
          color: "var(--text-primary)",
          fontSize: "var(--text-xs)",
          fontFamily: "var(--font-mono)",
        }}
      />
      <button type="button" onClick={() => onNavigate(urlInput)} style={primaryBtnStyle}>
        Go
      </button>
      <ZoomPicker zoom={zoom} onZoom={onZoom} />
      <button type="button" title="Detect dev servers" aria-label="Detect dev servers" onClick={onDetect} style={secondaryBtnStyle}>
        Detect
      </button>
      <button
        type="button"
        title="Start configured app preview server"
        aria-label="Start configured app preview server"
        onClick={() => onLaunch(previewLaunchConfigs[0]?.name)}
        disabled={previewLaunchConfigs.length === 0}
        style={previewLaunchConfigs.length === 0 ? disabledSecondaryBtnStyle : secondaryBtnStyle}
      >
        Start
      </button>
      <button
        type="button"
        title="Open externally"
        aria-label="Open externally"
        onClick={onOpenExternal}
        disabled={!livePreviewUrl}
        style={iconBtnStyle(!livePreviewUrl)}
      >
        <ExternalLink size={13} />
      </button>
      <button
        type="button"
        title="Expand app preview"
        aria-label="Expand app preview"
        onClick={onExpand}
        disabled={!livePreviewUrl}
        style={iconBtnStyle(!livePreviewUrl)}
      >
        <Maximize2 size={13} />
      </button>
      <button
        type="button"
        title="Check preview"
        aria-label="Check preview"
        onClick={() => livePreviewUrl && getWebSocket()?.send({ type: "preview.verify", url: livePreviewUrl })}
        disabled={!livePreviewUrl}
        style={!livePreviewUrl ? disabledSecondaryBtnStyle : secondaryBtnStyle}
      >
        Verify
      </button>
      <button
        type="button"
        title="Show server logs"
        aria-label="Show server logs"
        onClick={onToggleLogs}
        disabled={!activeLaunchProcess}
        style={!activeLaunchProcess ? disabledSecondaryBtnStyle : secondaryBtnStyle}
      >
        Logs{outputTail.length ? ` ${outputTail.length}` : ""}
      </button>
      <button
        type="button"
        title="Clear app preview"
        aria-label="Clear app preview"
        onClick={onClear}
        disabled={!livePreviewUrl}
        style={iconBtnStyle(!livePreviewUrl)}
      >
        <X size={13} />
      </button>
    </div>

    {visibleVerification && (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "5px 8px",
          borderBottom: "1px solid var(--border-subtle)",
          background: visibleVerification.ok
            ? "color-mix(in oklch, var(--state-success) 9%, var(--surface-page))"
            : "color-mix(in oklch, var(--state-warning) 10%, var(--surface-page))",
          color: visibleVerification.ok ? "var(--state-success)" : "var(--state-warning)",
          fontSize: "var(--text-xs)",
        }}
      >
        <span style={{ fontWeight: 600 }}>{visibleVerification.ok ? "Verified" : "Check failed"}</span>
        <span style={{ color: "var(--text-secondary)", fontFamily: "var(--font-mono)" }}>
          {visibleVerification.status_code ?? "no response"} / {visibleVerification.elapsed_ms}ms
        </span>
        {visibleVerification.error && (
          <span
            title={visibleVerification.error}
            style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--text-muted)" }}
          >
            {visibleVerification.error}
          </span>
        )}
      </div>
    )}

    {livePreviewUrl && (
      <div style={previewBoundaryStyle}>
        <span style={{ color: "var(--text-secondary)", fontWeight: 600 }}>
          {localPreview ? "App preview" : "External preview"}
        </span>
        <span style={{ color: "var(--text-muted)" }}>
          {localPreview ? "Console output belongs to the running app." : "Loaded with stricter frame isolation; host shell status stays separate."}
        </span>
      </div>
    )}

    {(previewLaunchConfigs.length > 0 || previewLaunchProcesses.length > 0) && (
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 6,
          padding: "6px 8px",
          borderBottom: "1px solid var(--border-subtle)",
          background: "var(--surface-page)",
        }}
      >
        {previewLaunchConfigs.map((config) => {
          const running = previewLaunchProcesses.find((process) => process.name === config.name || process.id === config.name);
          return (
            <div
              key={config.name}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                minWidth: 0,
                fontSize: "var(--text-xs)",
              }}
            >
              <span
                title={config.command}
                style={{
                  flex: 1,
                  minWidth: 0,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                  fontFamily: "var(--font-mono)",
                  color: "var(--text-secondary)",
                }}
              >
                {running ? running.status : "configured"} / {config.name} / {config.command}
              </span>
              <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                :{config.port}
              </span>
              {running ? (
                <>
                  <button type="button" onClick={() => onNavigate(running.url)} style={chipBtnStyle}>
                    Open
                  </button>
                  <button
                    type="button"
                    title="Stop preview server"
                    aria-label="Stop preview server"
                    onClick={() => onStopLaunch(running.id)}
                    style={iconBtnStyle(false)}
                  >
                    <Square size={12} />
                  </button>
                </>
              ) : (
                <button type="button" onClick={() => onLaunch(config.name)} style={chipBtnStyle}>
                  <Play size={12} style={{ marginRight: 4, verticalAlign: -2 }} />
                  Start
                </button>
              )}
              {running?.status === "crashed" && running.stderr_tail && running.stderr_tail.length > 0 && (
                <span
                  title={running.stderr_tail.join("\n")}
                  style={{
                    minWidth: 80,
                    maxWidth: 240,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                    color: "var(--state-danger, var(--state-warning))",
                    fontFamily: "var(--font-mono)",
                  }}
                >
                  {running.stderr_tail[running.stderr_tail.length - 1]}
                </span>
              )}
            </div>
          );
        })}
      </div>
    )}

    {logsOpen && activeLaunchProcess && (
      <div
        style={{
          maxHeight: 140,
          minHeight: 72,
          overflow: "auto",
          padding: "6px 8px",
          borderBottom: "1px solid var(--border-subtle)",
          background: "var(--surface-base)",
          color: "var(--text-secondary)",
          fontFamily: "var(--font-mono)",
          fontSize: "var(--text-xs)",
          lineHeight: 1.45,
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 8,
            marginBottom: 4,
            color: "var(--text-muted)",
          }}
        >
          <span title={activeLaunchProcess.command}>
            {activeLaunchProcess.name} / pid {activeLaunchProcess.pid ?? "?"}
          </span>
          <span>{activeLaunchProcess.status}</span>
        </div>
        {outputTail.length > 0 ? outputTail.map((entry, index) => (
          <div
            key={`${entry.timestamp ?? index}-${index}`}
            style={{
              display: "grid",
              gridTemplateColumns: "44px minmax(0, 1fr)",
              gap: 8,
              color: entry.stream === "stderr" ? "var(--state-warning)" : "var(--text-secondary)",
            }}
          >
            <span style={{ color: "var(--text-muted)" }}>{entry.stream}</span>
            <span style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{entry.line}</span>
          </div>
        )) : (
          <div style={{ color: "var(--text-muted)" }}>No server output yet.</div>
        )}
      </div>
    )}

    {previewServers.length > 0 && (
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 6,
          padding: "6px 8px",
          borderBottom: "1px solid var(--border-subtle)",
          background: "var(--surface-soft)",
        }}
      >
        <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", marginRight: 4 }}>Detected:</span>
        {previewServers.map((server) => (
          <button
            key={server.port}
            type="button"
            onClick={() => onNavigate(server.url)}
            style={{
              ...chipBtnStyle,
              borderColor: livePreviewUrl === server.url ? "var(--accent-primary)" : "var(--border-subtle)",
              color: livePreviewUrl === server.url ? "var(--accent-primary)" : "var(--text-primary)",
            }}
          >
            {server.framework ? `${server.framework} / ` : ""}
            {server.name || `:${server.port}`}
          </button>
        ))}
      </div>
    )}

    {livePreviewUrl ? (
      <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", position: "relative" }}>
        {refreshFlash && (
          <div
            className="preview-refresh-flash"
            style={{
              position: "absolute",
              top: 0,
              left: 0,
              right: 0,
              height: 2,
              background: "var(--accent-primary)",
              zIndex: 10,
            }}
          />
        )}
        <div
          onWheel={handleWheel}
          onMouseEnter={() => {
            try {
              window.focus();
            } catch (e) {
              /* window focus safety */
            }
          }}
          style={{
            flex: 1,
            minWidth: 0,
            display: "flex",
            flexDirection: "column",
            overflow: zoom === "fit" ? "hidden" : "auto",
            position: "relative",
            background: "var(--surface-base)",
          }}
        >
          {ctrlPressed && (
            <div
              style={{
                position: "absolute",
                inset: 0,
                zIndex: 5,
                background: "transparent",
                cursor: "zoom-in",
              }}
            />
          )}
          <div
            style={{
              position: "absolute",
              bottom: 12,
              right: 12,
              zIndex: 10,
              padding: "4px 8px",
              borderRadius: "4px",
              background: "rgba(0, 0, 0, 0.72)",
              color: "white",
              fontSize: "10px",
              fontFamily: "var(--font-mono)",
              pointerEvents: "none",
              display: "flex",
              alignItems: "center",
              gap: 5,
              opacity: 0.85,
            }}
          >
            <span>Zoom: {zoom === "fit" ? "Fit" : `${Math.round(zoom * 100)}%`}</span>
            {ctrlPressed && <span style={{ color: "var(--accent-primary)", fontWeight: "bold" }}>● Ctrl Zooming</span>}
          </div>
          <iframe
            ref={(node) => {
              iframeRef.current = node;
            }}
            key={`${iframeKey}-${livePreviewUrl}`}
            src={livePreviewUrl}
            title="App Preview"
            sandbox={previewSandboxForUrl(livePreviewUrl)}
            referrerPolicy={PREVIEW_REFERRER_POLICY}
            style={{
              ...(zoom === "fit"
                ? { width: "100%", height: "100%" }
                : {
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: `${100 / zoom}%`,
                    height: `${100 / zoom}%`,
                    transform: `scale(${zoom})`,
                    transformOrigin: "0 0",
                  }),
              border: 0,
              background: "var(--surface-base)",
              display: "block",
            }}
          />
        </div>
      </div>
    ) : (
      <div
        style={{
          flex: 1,
          display: "grid",
          placeItems: "center",
          padding: 16,
          color: "var(--text-muted)",
          fontSize: "var(--text-sm)",
          textAlign: "center",
        }}
      >
        <div style={{ display: "grid", justifyItems: "center", gap: 8 }}>
          <Globe size={28} style={{ opacity: 0.4, marginBottom: 8 }} />
          <div>Enter a local app URL above or click Detect to find running dev servers.</div>
          <div style={{ marginTop: 6, fontSize: "var(--text-xs)" }}>Common ports: 3000, 5173, 8080, 4200</div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "center", marginTop: 4 }}>
            <button type="button" onClick={onDetect} style={secondaryBtnStyle}>
              Detect
            </button>
            <button
              type="button"
              onClick={() => onLaunch(previewLaunchConfigs[0]?.name)}
              disabled={previewLaunchConfigs.length === 0}
              style={previewLaunchConfigs.length === 0 ? disabledSecondaryBtnStyle : primaryBtnStyle}
            >
              Start
            </button>
          </div>
        </div>
      </div>
    )}
  </>
  );
};

const ExpandedLivePreview = ({
  livePreviewUrl,
  urlInput,
  setUrlInput,
  iframeKey,
  onNavigate,
  onRefresh,
  onOpenExternal,
  onClose,
  zoom,
  onZoom,
  ctrlPressed,
}: {
  livePreviewUrl: string | null;
  urlInput: string;
  setUrlInput: (s: string) => void;
  iframeKey: number;
  onNavigate: (u: string) => void;
  onRefresh: () => void;
  onOpenExternal: () => void;
  onClose: () => void;
  zoom: "fit" | number;
  onZoom: (z: PreviewZoom) => void;
  ctrlPressed: boolean;
}) => {
  const handleWheel = (e: React.WheelEvent) => {
    if (!ctrlPressed && !e.ctrlKey) return;
    e.preventDefault();
    const currentZoom = zoom === "fit" ? 1.0 : zoom;
    const zoomStep = 0.08;
    let nextZoom = currentZoom - Math.sign(e.deltaY) * zoomStep;
    nextZoom = Math.min(Math.max(nextZoom, 0.25), 3.0);
    nextZoom = Math.round(nextZoom * 100) / 100;
    onZoom(nextZoom);
  };

  return (
    <div
      role="dialog"
      aria-label="Expanded app preview"
      style={{
        position: "fixed",
        inset: 18,
        insetInline: "clamp(8px, 2vw, 18px)",
        insetBlock: "clamp(8px, 2vh, 18px)",
        zIndex: "var(--z-modal)",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        background: "var(--surface-page)",
        border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-sm, 6px)",
        boxShadow: "var(--shadow-strong, 0 24px 60px rgba(0,0,0,0.45))",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          minHeight: 38,
          padding: "6px 8px",
          borderBottom: "1px solid var(--border-subtle)",
          background: "var(--surface-raised)",
        }}
      >
        <button type="button" title="Refresh app preview" aria-label="Refresh app preview" onClick={onRefresh} disabled={!livePreviewUrl} style={iconBtnStyle(!livePreviewUrl)}>
          <RefreshCw size={14} />
        </button>
        <input
          type="text"
          aria-label="Preview URL"
          value={urlInput}
          onChange={(e) => setUrlInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") onNavigate(urlInput);
            if (e.key === "Escape") onClose();
          }}
          placeholder="http://localhost:3000"
          spellCheck={false}
          autoFocus
          style={{
            flex: 1,
            minWidth: 0,
            height: 26,
            padding: "0 9px",
            border: "1px solid var(--border-subtle)",
            borderRadius: "var(--radius-sm, 4px)",
            background: "var(--surface-base)",
            color: "var(--text-primary)",
            fontSize: "var(--text-xs)",
            fontFamily: "var(--font-mono)",
            outline: "none",
          }}
        />
        <button type="button" onClick={() => onNavigate(urlInput)} style={primaryBtnStyle}>
          Go
        </button>
        <ZoomPicker zoom={zoom} onZoom={onZoom} align="right" compactHeight={26} />
        <button type="button" title="Open externally" aria-label="Open externally" onClick={onOpenExternal} disabled={!livePreviewUrl} style={iconBtnStyle(!livePreviewUrl)}>
          <ExternalLink size={14} />
        </button>
        <button type="button" title="Close expanded app preview" aria-label="Close expanded app preview" onClick={onClose} style={iconBtnStyle(false)}>
          <Minimize2 size={14} />
        </button>
      </div>
      {livePreviewUrl ? (
        <div
          onWheel={handleWheel}
          onMouseEnter={() => {
            try {
              window.focus();
            } catch (e) {
              /* window focus safety */
            }
          }}
          style={{
            flex: 1,
            minWidth: 0,
            display: "flex",
            flexDirection: "column",
            overflow: zoom === "fit" ? "hidden" : "auto",
            position: "relative",
            background: "var(--surface-base)",
          }}
        >
          {ctrlPressed && (
            <div
              style={{
                position: "absolute",
                inset: 0,
                zIndex: 5,
                background: "transparent",
                cursor: "zoom-in",
              }}
            />
          )}
          <div
            style={{
              position: "absolute",
              bottom: 12,
              right: 12,
              zIndex: 10,
              padding: "4px 8px",
              borderRadius: "4px",
              background: "rgba(0, 0, 0, 0.72)",
              color: "white",
              fontSize: "10px",
              fontFamily: "var(--font-mono)",
              pointerEvents: "none",
              display: "flex",
              alignItems: "center",
              gap: 5,
              opacity: 0.85,
            }}
          >
            <span>Zoom: {zoom === "fit" ? "Fit" : `${Math.round(zoom * 100)}%`}</span>
            {ctrlPressed && <span style={{ color: "var(--accent-primary)", fontWeight: "bold" }}>● Ctrl Zooming</span>}
          </div>
          <iframe
            key={`expanded-${iframeKey}-${livePreviewUrl}`}
            src={livePreviewUrl}
            title="Expanded App Preview"
            sandbox={previewSandboxForUrl(livePreviewUrl)}
            referrerPolicy={PREVIEW_REFERRER_POLICY}
            style={{
              ...(zoom === "fit"
                ? { width: "100%", height: "100%" }
                : {
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: `${100 / zoom}%`,
                    height: `${100 / zoom}%`,
                    transform: `scale(${zoom})`,
                    transformOrigin: "0 0",
                  }),
              border: 0,
              background: "var(--surface-base)",
              display: "block",
            }}
          />
        </div>
      ) : (
        <div
          style={{
            flex: 1,
            display: "grid",
            placeItems: "center",
            color: "var(--text-muted)",
            fontSize: "var(--text-sm)",
          }}
        >
          Enter a URL to open the expanded app preview.
        </div>
      )}
    </div>
  );
};

const ArtifactView = () => {
  const previewArtifact = useAppStore((s) => s.previewArtifact);

  if (!previewArtifact) {
    return (
      <div
        style={{
          flex: 1,
          display: "grid",
          placeItems: "center",
          padding: 16,
          color: "var(--text-muted)",
          fontSize: "var(--text-sm)",
          textAlign: "center",
        }}
      >
        在对话中打开文件后，可在这里查看完整内容。
      </div>
    );
  }

  const artifactUrl = previewArtifact.url ?? "";

  if (isSafePreviewImageArtifact(previewArtifact.mediaType, artifactUrl)) {
    return (
      <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
        <ArtifactHeader artifactId={previewArtifact.name || "生成图片"} sizeLabel={previewArtifact.mediaType ?? "Image"} />
        <div style={{ flex: 1, minHeight: 0, overflow: "auto", display: "grid", placeItems: "center", background: "var(--surface-base)" }}>
          <img src={artifactUrl} alt={previewArtifact.name || "生成图片"} style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain" }} />
        </div>
      </div>
    );
  }

  if (previewArtifact.url && previewArtifact.mediaType === "application/pdf") {
    return (
      <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
        <ArtifactHeader artifactId={previewArtifact.name || "生成的 PDF"} sizeLabel="PDF" />
        <iframe
          title={previewArtifact.name || "生成的 PDF"}
          src={previewArtifact.url}
          sandbox={ARTIFACT_FRAME_SANDBOX}
          referrerPolicy={PREVIEW_REFERRER_POLICY}
          style={{ flex: 1, minHeight: 0, border: 0, background: "var(--surface-base)" }}
        />
      </div>
    );
  }

  const content = prettyContent(previewArtifact.content);

  return (
    <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
      <ArtifactHeader
        artifactId={previewArtifact.name || "生成文件"}
        sizeLabel={`${(previewArtifact.content.length / 1024).toFixed(1)} KB`}
        onCopy={() => void navigator.clipboard?.writeText(previewArtifact.content)}
      />
      {previewArtifact.preview && previewArtifact.preview !== previewArtifact.content && (
        <div
          style={{
            padding: "6px 10px",
            borderBottom: "1px solid var(--border-subtle)",
            background: "var(--surface-soft)",
            color: "var(--text-muted)",
            fontSize: "var(--text-xs)",
            fontFamily: "var(--font-mono)",
            whiteSpace: "pre-wrap",
            maxHeight: 88,
            overflow: "auto",
          }}
        >
          {previewArtifact.preview}
        </div>
      )}
      <pre
        style={{
          flex: 1,
          minHeight: 0,
          margin: 0,
          overflow: "auto",
          padding: 12,
          background: "var(--surface-base)",
          color: "var(--text-primary)",
          fontFamily: "var(--font-mono)",
          fontSize: "var(--text-xs)",
          lineHeight: 1.55,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        }}
      >
        {content}
      </pre>
    </div>
  );
};

const ArtifactHeader = ({
  artifactId,
  sizeLabel,
  onCopy,
}: {
  artifactId: string;
  sizeLabel: string;
  onCopy?: () => void;
}) => (
  <div
    style={{
      display: "flex",
      alignItems: "center",
      gap: 8,
      padding: "6px 10px",
      borderBottom: "1px solid var(--border-subtle)",
      background: "var(--surface-page)",
      fontSize: "var(--text-xs)",
    }}
  >
    <span
      title={artifactId}
      style={{
        flex: 1,
        minWidth: 0,
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
        fontFamily: "var(--font-mono)",
        color: "var(--text-secondary)",
      }}
    >
      {artifactId}
    </span>
    <span style={{ color: "var(--text-muted)" }}>{sizeLabel}</span>
    {onCopy && (
      <button
        title="复制文件内容"
        aria-label="复制文件内容"
        onClick={onCopy}
        style={{
          width: 26,
          height: 24,
          border: 0,
          borderRadius: "var(--radius-sm, 4px)",
          background: "transparent",
          color: "var(--text-muted)",
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          cursor: "pointer",
        }}
      >
        <ClipboardCopy size={14} />
      </button>
    )}
  </div>
);

const previewBoundaryStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  flexWrap: "wrap",
  padding: "5px 8px",
  borderBottom: "1px solid var(--border-subtle)",
  background: "var(--surface-soft)",
  fontSize: "var(--text-xs)",
};

const iconBtnStyle = (disabled: boolean): React.CSSProperties => ({
  width: 24,
  height: 24,
  border: 0,
  borderRadius: "var(--radius-sm, 4px)",
  background: "transparent",
  color: disabled ? "var(--text-disabled, var(--text-muted))" : "var(--text-muted)",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  cursor: disabled ? "not-allowed" : "pointer",
  flexShrink: 0,
});

const primaryBtnStyle: React.CSSProperties = {
  height: 24,
  padding: "0 10px",
  border: 0,
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--accent-primary)",
  color: "var(--accent-on-primary, white)",
  fontSize: "var(--text-xs)",
  cursor: "pointer",
  flexShrink: 0,
};

const secondaryBtnStyle: React.CSSProperties = {
  height: 24,
  padding: "0 10px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "transparent",
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
  cursor: "pointer",
  flexShrink: 0,
};

const disabledSecondaryBtnStyle: React.CSSProperties = {
  ...secondaryBtnStyle,
  color: "var(--text-disabled, var(--text-muted))",
  cursor: "not-allowed",
};

const chipBtnStyle: React.CSSProperties = {
  height: 22,
  padding: "0 8px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-base)",
  color: "var(--text-primary)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
  cursor: "pointer",
};

const zoomTriggerStyle = (open: boolean, height: number): React.CSSProperties => ({
  height,
  minWidth: 68,
  padding: "0 7px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: open ? "var(--surface-soft)" : "var(--surface-base)",
  color: "var(--text-secondary)",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 6,
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-ui)",
  cursor: "pointer",
});

const zoomMenuStyle = (align: "left" | "right"): React.CSSProperties => ({
  position: "absolute",
  top: "calc(100% + 5px)",
  left: align === "left" ? 0 : "auto",
  right: align === "right" ? 0 : "auto",
  zIndex: 30,
  minWidth: 112,
  padding: 5,
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 8px)",
  background: "var(--surface-raised)",
  boxShadow: "var(--shadow-strong, var(--shadow-md))",
});

const zoomOptionStyle = (active: boolean): React.CSSProperties => ({
  width: "100%",
  minHeight: 28,
  padding: "0 7px",
  border: 0,
  borderRadius: "var(--radius-sm, 5px)",
  background: active ? "color-mix(in oklch, var(--accent-primary) 9%, var(--surface-page))" : "transparent",
  color: active ? "var(--text-primary)" : "var(--text-secondary)",
  display: "flex",
  alignItems: "center",
  gap: 6,
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-ui)",
  cursor: "pointer",
  textAlign: "left",
});
