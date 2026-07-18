import { AlertCircle, Camera, Check, ChevronDown, ChevronRight, Copy, ExternalLink, Globe, MapPin, MessageSquare, RefreshCw, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import {
  browserCaptureScreenshot,
  browserClick,
  browserDiscover,
  browserNavigate,
  browserType,
  isDesktop,
  openExternal,
  type BrowserActionResult,
  type BrowserDiscoveryResult,
  type BrowserScreenshotResult,
  type BrowserTargetInfo,
} from "../desktop/runtime";
import { assessNetworkTargetUrl } from "../lib/network-target";
import { useAppStore } from "../stores";

const DEFAULT_ENDPOINT = "http://127.0.0.1:9222";
const ENDPOINT_STORAGE_KEY = "minicode.browser.endpoint";
const TARGET_STORAGE_KEY = "minicode.browser.target";

const readStoredValue = (key: string): string | null => {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
};

const writeStoredValue = (key: string, value: string | null) => {
  try {
    if (value == null || !value.trim()) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // noop
  }
};

export const BrowserPanel = () => {
  const permissionMode = useAppStore((s) => s.permissionMode);
  const browserAnnotations = useAppStore((s) => s.browserAnnotations);
  const addBrowserAnnotation = useAppStore((s) => s.addBrowserAnnotation);
  const removeBrowserAnnotation = useAppStore((s) => s.removeBrowserAnnotation);
  const [endpoint, setEndpoint] = useState(() => readStoredValue(ENDPOINT_STORAGE_KEY) || DEFAULT_ENDPOINT);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<BrowserDiscoveryResult | null>(null);
  const [discoveryError, setDiscoveryError] = useState("");
  const discoverEpochRef = useRef(0);
  const [selectedTargetId, setSelectedTargetId] = useState<string | null>(() => readStoredValue(TARGET_STORAGE_KEY));
  const [screenshot, setScreenshot] = useState<BrowserScreenshotResult | null>(null);
  const [screenshotLoading, setScreenshotLoading] = useState(false);
  const [screenshotError, setScreenshotError] = useState("");
  const [navigateUrl, setNavigateUrl] = useState("");
  const [selector, setSelector] = useState("");
  const [inputText, setInputText] = useState("");
  const [noteSelector, setNoteSelector] = useState("");
  const [noteText, setNoteText] = useState("");
  const [noteAnchor, setNoteAnchor] = useState<{ xPercent: number; yPercent: number } | null>(null);
  const [actionLoading, setActionLoading] = useState<null | "navigate" | "click" | "type">(null);
  const [actionError, setActionError] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const refresh = async () => {
    if (!isDesktop()) return;
    const epoch = ++discoverEpochRef.current;
    const requestedEndpoint = endpoint;
    setLoading(true);
    setDiscoveryError("");
    try {
      const next = await browserDiscover(requestedEndpoint);
      if (epoch !== discoverEpochRef.current) return;
      if (!next) {
        setDiscoveryError(`Could not connect to Chrome at ${requestedEndpoint}.`);
        return;
      }
      setResult(next);
    } finally {
      if (epoch === discoverEpochRef.current) setLoading(false);
    }
  };

  useEffect(() => {
    if (!isDesktop()) return;
    void refresh();
  }, []);

  useEffect(() => {
    writeStoredValue(ENDPOINT_STORAGE_KEY, endpoint);
  }, [endpoint]);

  const pageTargets = useMemo(
    () => (result?.targets ?? []).filter((target) => target.type === "page"),
    [result],
  );
  const backgroundTargets = useMemo(
    () => (result?.targets ?? []).filter((target) => target.type !== "page"),
    [result],
  );
  const allTargets = result?.targets ?? [];
  const selectedTarget = useMemo(
    () => allTargets.find((target) => target.id === selectedTargetId) ?? null,
    [allTargets, selectedTargetId],
  );
  const selectedScreenshot = screenshot?.targetId === selectedTargetId ? screenshot : null;
  const selectedAnnotations = useMemo(
    () =>
      selectedTarget
        ? browserAnnotations.filter((annotation) =>
            annotation.targetId === selectedTarget.id || annotation.url === selectedTarget.url,
          )
        : [],
    [browserAnnotations, selectedTarget],
  );

  useEffect(() => {
    if (allTargets.length === 0) {
      setSelectedTargetId(null);
      writeStoredValue(TARGET_STORAGE_KEY, null);
      return;
    }
    if (selectedTargetId && allTargets.some((target) => target.id === selectedTargetId)) return;
    const next = allTargets.find((target) => target.type === "page") ?? allTargets[0] ?? null;
    if (!next) return;
    setSelectedTargetId(next.id);
    writeStoredValue(TARGET_STORAGE_KEY, next.id);
  }, [allTargets, selectedTargetId]);

  useEffect(() => {
    if (!selectedTarget) return;
    setNavigateUrl(selectedTarget.url || "");
  }, [selectedTarget?.id, selectedTarget?.url]);

  useEffect(() => {
    setNoteAnchor(null);
  }, [selectedTarget?.id, selectedScreenshot?.capturedAt]);

  const selectTarget = (targetId: string) => {
    setSelectedTargetId(targetId);
    writeStoredValue(TARGET_STORAGE_KEY, targetId);
    setActionError("");
    setActionMessage("");
  };

  const addPageNote = () => {
    if (!selectedTarget || !noteText.trim()) return;
    addBrowserAnnotation({
      id: `browser-note-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
      targetId: selectedTarget.id,
      url: selectedTarget.url,
      title: selectedTarget.title,
      selector: noteSelector.trim() || undefined,
      xPercent: noteAnchor?.xPercent,
      yPercent: noteAnchor?.yPercent,
      note: noteText.trim(),
      createdAt: Date.now(),
      screenshotCapturedAt: selectedScreenshot?.capturedAt,
      screenshotWidth: selectedScreenshot?.width,
      screenshotHeight: selectedScreenshot?.height,
    });
    setNoteText("");
    setNoteAnchor(null);
  };

  const selectScreenshotAnchor = (event: MouseEvent<HTMLImageElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    setNoteAnchor({
      xPercent: clampPercent(((event.clientX - rect.left) / rect.width) * 100),
      yPercent: clampPercent(((event.clientY - rect.top) / rect.height) * 100),
    });
  };

  const capture = async (target: BrowserTargetInfo | null) => {
    if (!target) return;
    setActionMessage("");
    setScreenshotError("");
    setScreenshotLoading(true);
    try {
      const next = await browserCaptureScreenshot(endpoint, target.id);
      if (!next) {
        throw new Error("Screenshot failed.");
      }
      setScreenshot(next);
    } catch (error) {
      setScreenshotError(error instanceof Error ? error.message : "Screenshot failed.");
    } finally {
      setScreenshotLoading(false);
    }
  };

  const applyActionResult = (next: BrowserActionResult) => {
    setActionMessage(`${next.action} complete`);
    setActionError("");
    setScreenshot(next.screenshot);
    setResult((current) => {
      if (!current) return current;
      return {
        ...current,
        targets: current.targets.map((target) =>
          target.id === next.targetId
            ? {
                ...target,
                title: next.title,
                url: next.url,
              }
            : target,
        ),
      };
    });
    setNavigateUrl(next.url);
  };

  const runAction = async (action: "navigate" | "click" | "type") => {
    if (!selectedTargetId) return;
    setActionLoading(action);
    setActionError("");
    setActionMessage("");
    try {
      let next: BrowserActionResult | null = null;
      if (action === "navigate") {
        if (!navigateUrl.trim()) {
          throw new Error("URL is required.");
        }
        const target = assessNetworkTargetUrl(navigateUrl.trim());
        if (target.risk === "invalid") {
          throw new Error(target.reason);
        }
        if (permissionMode !== "bypass" && target.requiresReview) {
          const { showConfirm } = await import("../overlays/DialogService");
          const ok = await showConfirm({
            title: target.risk === "local" ? "Open local browser target" : "Open private network target",
            message: `${target.host} is a ${target.risk} address. Continue with Chrome navigation?`,
            confirmLabel: "Open",
          });
          if (!ok) return;
        }
        next = await browserNavigate(endpoint, selectedTargetId, target.normalizedUrl);
      } else if (action === "click") {
        if (!selector.trim()) {
          throw new Error("CSS selector is required.");
        }
        next = await browserClick(endpoint, selectedTargetId, selector.trim());
      } else {
        if (!selector.trim()) {
          throw new Error("CSS selector is required.");
        }
        if (!inputText) {
          throw new Error("Text is required.");
        }
        next = await browserType(endpoint, selectedTargetId, selector.trim(), inputText);
      }
      if (!next) {
        throw new Error(`${action} failed.`);
      }
      applyActionResult(next);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : `${action} failed.`);
    } finally {
      setActionLoading(null);
    }
  };

  if (!isDesktop()) {
    return (
      <div className="flex-1 grid place-items-center gap-2 text-[var(--text-muted)] text-sm">
        <Globe size={20} style={{ opacity: 0.7 }} />
        <div>Browser Control is desktop-only.</div>
      </div>
    );
  }

  return (
    <div className="flex-1 min-h-0 flex flex-col">
      <div className="flex items-center gap-1.5 px-2.5 py-2 border-b border-[var(--border-subtle)]">
        <button
          type="button"
          title="Refresh Chrome targets"
          aria-label="Refresh Chrome targets"
          onClick={() => void refresh()}
          disabled={loading}
          className="w-6 h-6 inline-flex items-center justify-center rounded border border-[var(--border-subtle)] bg-[var(--surface-page)] text-[var(--text-secondary)] cursor-pointer p-0"
        >
          <RefreshCw size={13} />
        </button>
        <input
          type="text"
          value={endpoint}
          onChange={(event) => setEndpoint(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void refresh();
          }}
          spellCheck={false}
          placeholder={DEFAULT_ENDPOINT}
          className="flex-1 min-w-0 h-[26px] px-2 rounded border border-[var(--border-subtle)] bg-[var(--surface-base)] text-[var(--text-primary)] text-xs font-mono outline-none"
        />
        <button type="button" onClick={() => void refresh()} disabled={loading} className="border-0 px-2.5 py-1 rounded bg-[var(--accent-primary)] text-[var(--surface-base)] text-xs font-bold cursor-pointer disabled:opacity-60">
          {loading ? "Checking..." : "Connect"}
        </button>
      </div>

      <div className="flex-1 overflow-y-auto grid gap-2.5 p-3">
        {discoveryError && (
          <div role="alert" className="flex items-center gap-2 p-2.5 border rounded-[var(--radius-sm,6px)] text-xs" style={{ color: "var(--state-danger)", borderColor: "var(--state-danger)", background: "var(--surface-soft)" }}>
            <AlertCircle size={15} />
            <span>{discoveryError}</span>
          </div>
        )}
        <div className="grid gap-2 p-2.5 bg-[var(--surface-soft)] border border-[var(--border-subtle)] rounded-[var(--radius-sm,6px)]">
          <div className="flex items-center justify-between gap-2.5">
            <div className="min-w-0">
              <div className="text-xs uppercase font-bold text-[var(--text-muted)]">Browser Control</div>
              <div className="mt-0.5 text-[var(--text-primary)] text-sm font-semibold overflow-hidden text-ellipsis whitespace-nowrap">{result?.browser || "External Chrome"}</div>
            </div>
            <span
              className="shrink-0 px-2 py-0.5 rounded-[var(--radius-sm,4px)] bg-[var(--surface-page)] border border-[var(--border-subtle)] text-xs font-bold"
              style={{
                color:
                  result?.status === "connected"
                    ? "var(--accent-primary)"
                    : result?.status === "error"
                      ? "var(--state-warning)"
                      : "var(--text-muted)",
              }}
            >
              {result?.status === "connected" ? "Connected" : result?.status === "error" || discoveryError ? "Unavailable" : "Idle"}
            </span>
          </div>
          <div className="grid gap-1">
            <InfoRow label="Endpoint" value={result?.endpoint ?? endpoint} mono />
            <InfoRow label="Pages" value={String(pageTargets.length)} />
            <InfoRow label="Targets" value={String(result?.targets.length ?? 0)} />
          </div>
        </div>

        {!discoveryError && selectedTarget ? (
          <div className="grid gap-2.5 p-2.5 bg-[var(--surface-soft)] border border-[var(--border-subtle)] rounded-[var(--radius-sm,6px)]">
            <div className="flex items-center gap-2 min-w-0">
              <Globe size={16} style={{ color: "var(--accent-primary)", flexShrink: 0 }} />
              <div className="min-w-0 flex-1">
                <div className="text-[var(--text-primary)] text-sm font-bold overflow-hidden text-ellipsis whitespace-nowrap">{selectedTarget.title || "Untitled page"}</div>
                <div title={selectedTarget.url || ""} className="mt-0.5 text-[var(--text-muted)] font-mono text-xs overflow-hidden text-ellipsis whitespace-nowrap">
                  {selectedTarget.url || "--"}
                </div>
              </div>
              <span className="shrink-0 text-[var(--accent-primary)] bg-[var(--surface-page)] border border-[var(--border-subtle)] rounded-[var(--radius-sm,4px)] px-1.5 py-0.5 text-[10px] font-extrabold uppercase">{selectedTarget.type}</span>
            </div>
            <div className="flex gap-1.5 flex-wrap">
              <TinyButton
                icon={<Camera size={12} />}
                label={screenshotLoading ? "Capturing..." : "Capture"}
                onClick={() => void capture(selectedTarget)}
                disabled={screenshotLoading || selectedTarget.type !== "page" || !selectedTarget.webSocketDebuggerUrl}
              />
              <TinyButton
                icon={<ExternalLink size={12} />}
                label="Open"
                onClick={() => {
                  if (selectedTarget.url) void openExternal(selectedTarget.url);
                }}
                disabled={!selectedTarget.url}
              />
              <TinyButton
                icon={<Copy size={12} />}
                label="Copy URL"
                onClick={() => {
                  if (selectedTarget.url) void navigator.clipboard?.writeText(selectedTarget.url);
                }}
                disabled={!selectedTarget.url}
              />
            </div>
            {selectedTarget.type === "page" && (
              <div className="flex gap-1.5 items-center">
                <input
                  type="text"
                  value={navigateUrl}
                  onChange={(event) => setNavigateUrl(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") void runAction("navigate");
                  }}
                  spellCheck={false}
                  placeholder="https://example.com"
                  className="flex-1 min-w-0 h-7 px-2 rounded-[var(--radius-sm,4px)] border border-[var(--border-subtle)] bg-[var(--surface-page)] text-[var(--text-primary)] text-xs font-mono outline-none"
                />
                <button
                  type="button"
                  onClick={() => void runAction("navigate")}
                  disabled={actionLoading != null}
                  className="border border-[var(--border-subtle)] px-2.5 py-1 rounded-[var(--radius-sm,4px)] bg-[var(--surface-page)] text-[var(--text-secondary)] text-xs font-semibold cursor-pointer"
                >
                  {actionLoading === "navigate" ? "Opening..." : "Navigate"}
                </button>
              </div>
            )}
          </div>
        ) : !discoveryError ? (
          <InfoCard>
            <div className="text-xs uppercase font-bold text-[var(--text-muted)]">Current Page</div>
            <div className="text-[var(--text-secondary)] text-xs leading-relaxed">No Chrome page selected.</div>
          </InfoCard>
        ) : null}

        {selectedTarget?.type === "page" && (
          <InfoCard>
            <div className="flex items-center justify-between gap-2">
              <div>
                <div className="text-xs uppercase font-bold text-[var(--text-muted)]">Page Notes</div>
                <div className="text-[var(--text-secondary)] text-xs leading-relaxed">Anchor comments to this browser target.</div>
              </div>
              <span className="shrink-0 px-1.5 py-0.5 rounded-[var(--radius-sm,4px)] bg-[var(--surface-page)] border border-[var(--border-subtle)] text-[10px] font-bold text-[var(--text-muted)]">
                {selectedAnnotations.length}
              </span>
            </div>
            <div className="grid gap-1.5 mt-1.5">
              <input
                type="text"
                value={noteSelector}
                onChange={(event) => setNoteSelector(event.target.value)}
                spellCheck={false}
                placeholder="Optional CSS selector, e.g. #app button.primary"
                className="h-7 px-2 rounded-[var(--radius-sm,4px)] border border-[var(--border-subtle)] bg-[var(--surface-page)] text-[var(--text-primary)] text-xs font-mono outline-none"
              />
              <textarea
                value={noteText}
                onChange={(event) => setNoteText(event.target.value)}
                placeholder="Comment on the current page state"
                className="min-h-16 resize-y p-2 rounded-[var(--radius-sm,4px)] border border-[var(--border-subtle)] bg-[var(--surface-page)] text-[var(--text-primary)] text-xs outline-none"
              />
              <div className="flex gap-1.5 flex-wrap">
                <TinyButton
                  icon={<MessageSquare size={12} />}
                  label="Add Note"
                  onClick={addPageNote}
                  disabled={!noteText.trim()}
                />
                <TinyButton
                  icon={<Copy size={12} />}
                  label="Use Selector"
                  onClick={() => setNoteSelector(selector)}
                  disabled={!selector.trim()}
                />
                {noteAnchor && (
                  <TinyButton
                    icon={<X size={12} />}
                    label={formatCoordinateLabel(noteAnchor)}
                    onClick={() => setNoteAnchor(null)}
                  />
                )}
              </div>
            </div>
            {selectedAnnotations.length > 0 && (
              <div className="grid gap-1.5 mt-1">
                {selectedAnnotations.slice(0, 6).map((annotation) => (
                  <div key={annotation.id} className="grid gap-1.5 p-2 rounded-[var(--radius-sm,4px)] border border-[var(--border-subtle)] bg-[var(--surface-page)]">
                    <div className="flex items-start gap-2">
                      <MessageSquare size={13} style={{ color: "var(--accent-primary)", flexShrink: 0, marginTop: 1 }} />
                      <div className="min-w-0 flex-1">
                        <div className="text-xs text-[var(--text-primary)] leading-relaxed whitespace-pre-wrap break-words">{annotation.note}</div>
                        <div className="mt-1 text-[10px] text-[var(--text-muted)] font-mono overflow-hidden text-ellipsis whitespace-nowrap">
                          {annotation.selector || formatCoordinateLabel(annotation) || "page"} · {new Date(annotation.createdAt).toLocaleTimeString()}
                        </div>
                      </div>
                      <button
                        type="button"
                        title="Remove page note"
                        aria-label="Remove page note"
                        onClick={() => removeBrowserAnnotation(annotation.id)}
                        className="w-6 h-6 inline-flex items-center justify-center rounded border border-[var(--border-subtle)] bg-transparent text-[var(--text-muted)] cursor-pointer p-0"
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>
                    {annotation.selector && (
                      <div className="flex gap-1.5 flex-wrap">
                        <TinyButton
                          icon={<Check size={12} />}
                          label="Set Selector"
                          onClick={() => {
                            setSelector(annotation.selector || "");
                            setNoteSelector(annotation.selector || "");
                            setAdvancedOpen(true);
                          }}
                        />
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </InfoCard>
        )}

        {result?.error && (
          <div className="flex items-center gap-2 text-[var(--state-danger)] bg-[var(--state-danger-soft)] border border-[var(--state-danger)] rounded-[var(--radius-sm,4px)] p-2 text-xs">
            <AlertCircle size={14} />
            <span>{result.error}</span>
          </div>
        )}

        {screenshotError && (
          <div className="flex items-center gap-2 text-[var(--state-danger)] bg-[var(--state-danger-soft)] border border-[var(--state-danger)] rounded-[var(--radius-sm,4px)] p-2 text-xs">
            <AlertCircle size={14} />
            <span>{screenshotError}</span>
          </div>
        )}

        {actionError && (
          <div className="flex items-center gap-2 text-[var(--state-danger)] bg-[var(--state-danger-soft)] border border-[var(--state-danger)] rounded-[var(--radius-sm,4px)] p-2 text-xs">
            <AlertCircle size={14} />
            <span>{actionError}</span>
          </div>
        )}

        {actionMessage && (
          <div className="flex items-center gap-2 text-xs p-2 rounded-[var(--radius-sm,4px)]" style={{
            color: "var(--state-success, var(--accent-primary))",
            background: "color-mix(in oklch, var(--accent-primary) 12%, transparent)",
            border: "1px solid color-mix(in oklch, var(--accent-primary) 55%, transparent)",
          }}>
            <Check size={14} />
            <span>{actionMessage}</span>
          </div>
        )}

        {selectedScreenshot && (
          <InfoCard>
            <div className="text-xs uppercase font-bold text-[var(--text-muted)]">Latest Screenshot</div>
            <div className="text-[var(--text-secondary)] text-xs leading-relaxed">
              {new Date(selectedScreenshot.capturedAt).toLocaleString()}
              {selectedScreenshot.width && selectedScreenshot.height ? ` · ${selectedScreenshot.width}×${selectedScreenshot.height}` : ""}
              {noteAnchor ? ` · ${formatCoordinateLabel(noteAnchor)}` : ""}
            </div>
            <div className="relative mt-1.5 rounded-[var(--radius-sm,6px)] overflow-hidden border border-[var(--border-subtle)] bg-[var(--surface-page)]">
              <img
                src={`data:${selectedScreenshot.mimeType};base64,${selectedScreenshot.data}`}
                alt={selectedScreenshot.title || selectedScreenshot.url || "Browser Control screenshot"}
                title="Click to anchor a page note"
                onClick={selectScreenshotAnchor}
                className="block w-full h-auto max-h-[420px] object-contain cursor-crosshair"
              />
              {selectedAnnotations
                .filter((annotation) => annotation.screenshotCapturedAt === selectedScreenshot.capturedAt)
                .filter(hasCoordinateAnchor)
                .map((annotation) => (
                  <ScreenshotPin
                    key={annotation.id}
                    xPercent={annotation.xPercent}
                    yPercent={annotation.yPercent}
                    title={annotation.note}
                  />
                ))}
              {noteAnchor && (
                <ScreenshotPin
                  xPercent={noteAnchor.xPercent}
                  yPercent={noteAnchor.yPercent}
                  active
                  title={formatCoordinateLabel(noteAnchor)}
                />
              )}
            </div>
          </InfoCard>
        )}

        <div className="grid gap-2">
          <button
            type="button"
            onClick={() => setAdvancedOpen((current) => !current)}
            className="w-full flex items-center gap-1 px-2 py-1.5 rounded-[var(--radius-sm,4px)] border border-[var(--border-subtle)] bg-[var(--surface-page)] text-[var(--text-secondary)] cursor-pointer text-xs font-bold"
            aria-expanded={advancedOpen}
          >
            {advancedOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            <span>Advanced</span>
          </button>
          {advancedOpen && (
            <div className="grid gap-2.5">{selectedTarget?.type === "page" && (
                <InfoCard>
                  <div className="text-xs uppercase font-bold text-[var(--text-muted)]">Selector Actions</div>
                  <div className="grid gap-2 mt-2">
                    <div className="grid gap-1.5">
                      <div className="text-[10px] font-bold tracking-wide uppercase text-[var(--text-muted)]">Selector</div>
                      <input
                        type="text"
                        value={selector}
                        onChange={(event) => setSelector(event.target.value)}
                        spellCheck={false}
                        placeholder="#app button.primary"
                        className="flex-1 min-w-0 h-7 px-2 rounded-[var(--radius-sm,4px)] border border-[var(--border-subtle)] bg-[var(--surface-page)] text-[var(--text-primary)] text-xs font-mono outline-none"
                      />
                    </div>
                    <div className="grid gap-1.5">
                      <div className="text-[10px] font-bold tracking-wide uppercase text-[var(--text-muted)]">Type text</div>
                      <textarea
                        value={inputText}
                        onChange={(event) => setInputText(event.target.value)}
                        placeholder="Hello from MiniCode"
                        className="min-h-16 resize-y p-2 rounded-[var(--radius-sm,4px)] border border-[var(--border-subtle)] bg-[var(--surface-page)] text-[var(--text-primary)] text-xs font-mono outline-none"
                      />
                    </div>
                    <div className="flex gap-1.5 flex-wrap">
                      <button
                        type="button"
                        onClick={() => void runAction("click")}
                        disabled={actionLoading != null}
                        className="border border-[var(--border-subtle)] px-2.5 py-1 rounded-[var(--radius-sm,4px)] bg-[var(--surface-page)] text-[var(--text-secondary)] text-xs font-semibold cursor-pointer"
                      >
                        {actionLoading === "click" ? "Clicking..." : "Click Selector"}
                      </button>
                      <button
                        type="button"
                        onClick={() => void runAction("type")}
                        disabled={actionLoading != null}
                        className="border border-[var(--border-subtle)] px-2.5 py-1 rounded-[var(--radius-sm,4px)] bg-[var(--surface-page)] text-[var(--text-secondary)] text-xs font-semibold cursor-pointer"
                      >
                        {actionLoading === "type" ? "Typing..." : "Type Into Selector"}
                      </button>
                    </div>
                  </div>
                </InfoCard>
              )}
              <InfoCard>
                <div className="text-xs uppercase font-bold text-[var(--text-muted)]">Connection</div>
                <InfoRow label="Mode" value="External Chrome / CDP" />
                <InfoRow label="Endpoint" value={result?.endpoint ?? endpoint} mono />
                <InfoRow label="WebSocket" value={selectedTarget?.webSocketDebuggerUrl || "--"} mono />
                <div className="flex gap-1.5 flex-wrap mt-1">
                  <TinyButton
                    icon={<Copy size={12} />}
                    label="Copy WS"
                    onClick={() => {
                      if (selectedTarget?.webSocketDebuggerUrl) void navigator.clipboard?.writeText(selectedTarget.webSocketDebuggerUrl);
                    }}
                    disabled={!selectedTarget?.webSocketDebuggerUrl}
                  />
                </div>
                <pre className="m-0 p-2 text-xs font-mono text-[var(--text-secondary)] bg-[var(--surface-page)] border border-[var(--border-subtle)] rounded-[var(--radius-sm,4px)] whitespace-pre-wrap break-words">chrome.exe --remote-debugging-port=9222</pre>
              </InfoCard>
              <TargetSection
                title={`Pages (${pageTargets.length})`}
                targets={pageTargets}
                selectedTargetId={selectedTargetId}
                onSelect={selectTarget}
                onCapture={capture}
                captureLoading={screenshotLoading}
              />
              <TargetSection
                title={`Other Targets (${backgroundTargets.length})`}
                targets={backgroundTargets}
                selectedTargetId={selectedTargetId}
                onSelect={selectTarget}
                onCapture={capture}
                captureLoading={screenshotLoading}
              />
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, Number(value.toFixed(2))));
}

function hasCoordinateAnchor<T extends { xPercent?: number; yPercent?: number }>(
  annotation: T,
): annotation is T & { xPercent: number; yPercent: number } {
  return typeof annotation.xPercent === "number" &&
    typeof annotation.yPercent === "number" &&
    Number.isFinite(annotation.xPercent) &&
    Number.isFinite(annotation.yPercent);
}

function formatCoordinateLabel(anchor: { xPercent?: number; yPercent?: number } | null): string {
  if (!anchor || !hasCoordinateAnchor(anchor)) return "";
  return `Point ${Math.round(anchor.xPercent)}%, ${Math.round(anchor.yPercent)}%`;
}

function ScreenshotPin({
  xPercent,
  yPercent,
  active = false,
  title,
}: {
  xPercent: number;
  yPercent: number;
  active?: boolean;
  title?: string;
}) {
  return (
    <span
      title={title}
      aria-hidden="true"
      className="pointer-events-none absolute inline-flex items-center justify-center rounded-full border shadow-sm"
      style={{
        left: `${xPercent}%`,
        top: `${yPercent}%`,
        width: active ? 18 : 14,
        height: active ? 18 : 14,
        transform: "translate(-50%, -50%)",
        color: active ? "var(--surface-base)" : "var(--accent-primary)",
        background: active ? "var(--accent-primary)" : "var(--surface-base)",
        borderColor: "var(--accent-primary)",
      }}
    >
      <MapPin size={active ? 12 : 10} />
    </span>
  );
}

const TargetSection = ({
  title,
  targets,
  selectedTargetId,
  onSelect,
  onCapture,
  captureLoading,
}: {
  title: string;
  targets: BrowserTargetInfo[];
  selectedTargetId: string | null;
  onSelect: (targetId: string) => void;
  onCapture: (target: BrowserTargetInfo) => void;
  captureLoading: boolean;
}) => (
  <div className="grid gap-2">
    <div className="text-xs uppercase font-bold text-[var(--text-muted)]">{title}</div>
    {targets.length === 0 ? (
      <div className="text-[var(--text-muted)] text-xs">No targets available.</div>
    ) : (
      <div className="grid gap-2">
        {targets.map((target) => (
          <div
            key={target.id || `${target.type}-${target.url}`}
            className="grid gap-2 p-2 bg-[var(--surface-soft)] border rounded-[var(--radius-sm,6px)]"
            style={{
              borderColor: selectedTargetId === target.id ? "var(--accent-primary)" : "var(--border-subtle)",
              boxShadow: selectedTargetId === target.id ? "inset 0 0 0 1px color-mix(in oklch, var(--accent-primary) 45%, transparent)" : "none",
            }}
          >
            <div className="min-w-0">
              <div className="text-xs font-bold text-[var(--text-primary)] overflow-hidden text-ellipsis whitespace-nowrap">{target.title || target.url || "Untitled target"}</div>
              <div className="text-[10px] text-[var(--accent-primary)] uppercase mt-0.5">{target.type}</div>
              {target.url && <div className="mt-1.5 text-xs text-[var(--text-secondary)] font-mono overflow-hidden text-ellipsis whitespace-nowrap">{target.url}</div>}
              {target.webSocketDebuggerUrl && <div className="mt-1 text-[10px] text-[var(--text-muted)] font-mono overflow-hidden text-ellipsis whitespace-nowrap">{target.webSocketDebuggerUrl}</div>}
            </div>
            <div className="flex gap-1.5 flex-wrap">
              <TinyButton
                icon={<Check size={12} />}
                label={selectedTargetId === target.id ? "Selected" : "Select"}
                onClick={() => onSelect(target.id)}
              />
              <TinyButton
                icon={<Camera size={12} />}
                label="Capture"
                onClick={() => onCapture(target)}
                disabled={captureLoading || target.type !== "page" || !target.webSocketDebuggerUrl}
              />
              <TinyButton
                icon={<ExternalLink size={12} />}
                label="Open"
                onClick={() => {
                  if (target.url) void openExternal(target.url);
                }}
                disabled={!target.url}
              />
              <TinyButton
                icon={<Copy size={12} />}
                label="Copy WS"
                onClick={() => {
                  if (target.webSocketDebuggerUrl) void navigator.clipboard?.writeText(target.webSocketDebuggerUrl);
                }}
                disabled={!target.webSocketDebuggerUrl}
              />
            </div>
          </div>
        ))}
      </div>
    )}
  </div>
);

const TinyButton = ({
  icon,
  label,
  onClick,
  disabled,
}: {
  icon: ReactNode;
  label: string;
  onClick: () => void;
  disabled?: boolean;
}) => (
  <button
    type="button"
    disabled={disabled}
    onClick={onClick}
    className="inline-flex items-center gap-1 px-2 py-1 rounded-[var(--radius-sm,4px)] border border-[var(--border-subtle)] bg-[var(--surface-page)] text-xs whitespace-nowrap"
    style={{
      color: disabled ? "var(--text-muted)" : "var(--text-secondary)",
      cursor: disabled ? "not-allowed" : "pointer",
    }}
  >
    {icon}
    {label}
  </button>
);

const InfoCard = ({ children }: { children: ReactNode }) => <div className="grid gap-1.5 p-2 bg-[var(--surface-soft)] border border-[var(--border-subtle)] rounded-[var(--radius-sm,6px)]">{children}</div>;

const InfoRow = ({
  label,
  value,
  mono,
  tone = "default",
}: {
  label: string;
  value: string;
  mono?: boolean;
  tone?: "default" | "muted" | "accent" | "warning";
}) => {
  const color =
    tone === "accent"
      ? "var(--accent-primary)"
      : tone === "warning"
        ? "var(--state-warning)"
        : tone === "muted"
          ? "var(--text-muted)"
          : "var(--text-secondary)";
  return (
    <div className="grid gap-2 text-xs" style={{ gridTemplateColumns: "88px minmax(0, 1fr)" }}>
      <span className="text-[var(--text-muted)]">{label}</span>
      <span
        title={value}
        className="overflow-hidden text-ellipsis whitespace-nowrap"
        style={{
          color,
          fontFamily: mono ? "var(--font-mono)" : undefined,
        }}
      >
        {value}
      </span>
    </div>
  );
};
