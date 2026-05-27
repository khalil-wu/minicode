import { AlertCircle, Camera, Check, ChevronDown, ChevronRight, Copy, ExternalLink, Globe, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
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
  const [endpoint, setEndpoint] = useState(() => readStoredValue(ENDPOINT_STORAGE_KEY) || DEFAULT_ENDPOINT);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<BrowserDiscoveryResult | null>(null);
  const [selectedTargetId, setSelectedTargetId] = useState<string | null>(() => readStoredValue(TARGET_STORAGE_KEY));
  const [screenshot, setScreenshot] = useState<BrowserScreenshotResult | null>(null);
  const [screenshotLoading, setScreenshotLoading] = useState(false);
  const [screenshotError, setScreenshotError] = useState("");
  const [navigateUrl, setNavigateUrl] = useState("");
  const [selector, setSelector] = useState("");
  const [inputText, setInputText] = useState("");
  const [actionLoading, setActionLoading] = useState<null | "navigate" | "click" | "type">(null);
  const [actionError, setActionError] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const refresh = async () => {
    if (!isDesktop()) return;
    setLoading(true);
    try {
      const next = await browserDiscover(endpoint);
      setResult(next);
    } finally {
      setLoading(false);
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

  const selectTarget = (targetId: string) => {
    setSelectedTargetId(targetId);
    writeStoredValue(TARGET_STORAGE_KEY, targetId);
    setActionError("");
    setActionMessage("");
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
        next = await browserNavigate(endpoint, selectedTargetId, navigateUrl.trim());
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
      <div style={emptyWrapStyle}>
        <Globe size={20} style={{ opacity: 0.7 }} />
        <div>Browser panel is desktop-only.</div>
      </div>
    );
  }

  return (
    <div style={panelStyle}>
      <div style={toolbarStyle}>
        <button
          type="button"
          title="Refresh browser targets"
          aria-label="Refresh browser targets"
          onClick={() => void refresh()}
          style={iconButtonStyle}
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
          style={inputStyle}
        />
        <button type="button" onClick={() => void refresh()} style={primaryButtonStyle}>
          {loading ? "Checking..." : "Connect"}
        </button>
      </div>

      <div style={bodyStyle}>
        <div style={statusCardStyle}>
          <div style={statusHeaderStyle}>
            <div style={{ minWidth: 0 }}>
              <div style={sectionTitleStyle}>Browser</div>
              <div style={browserNameStyle}>{result?.browser || "External Chrome"}</div>
            </div>
            <span
              style={{
                ...statusPillStyle,
                color:
                  result?.status === "connected"
                    ? "var(--accent-primary)"
                    : result?.status === "error"
                      ? "var(--state-warning)"
                      : "var(--text-muted)",
              }}
            >
              {result?.status === "connected" ? "Connected" : result?.status === "error" ? "Unavailable" : "Idle"}
            </span>
          </div>
          <div style={compactInfoGridStyle}>
            <InfoRow label="Endpoint" value={result?.endpoint ?? endpoint} mono />
            <InfoRow label="Pages" value={String(pageTargets.length)} />
            <InfoRow label="Targets" value={String(result?.targets.length ?? 0)} />
          </div>
        </div>

        {selectedTarget ? (
          <div style={currentPageStyle}>
            <div style={currentPageHeaderStyle}>
              <Globe size={16} style={{ color: "var(--accent-primary)", flexShrink: 0 }} />
              <div style={{ minWidth: 0, flex: 1 }}>
                <div style={currentPageTitleStyle}>{selectedTarget.title || "Untitled page"}</div>
                <div title={selectedTarget.url || ""} style={currentPageUrlStyle}>
                  {selectedTarget.url || "--"}
                </div>
              </div>
              <span style={targetTypeBadgeStyle}>{selectedTarget.type}</span>
            </div>
            <div style={primaryActionsStyle}>
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
              <div style={primaryNavigateStyle}>
                <input
                  type="text"
                  value={navigateUrl}
                  onChange={(event) => setNavigateUrl(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") void runAction("navigate");
                  }}
                  spellCheck={false}
                  placeholder="https://example.com"
                  style={fieldInputStyle}
                />
                <button
                  type="button"
                  onClick={() => void runAction("navigate")}
                  disabled={actionLoading != null}
                  style={secondaryButtonStyle}
                >
                  {actionLoading === "navigate" ? "Opening..." : "Navigate"}
                </button>
              </div>
            )}
          </div>
        ) : (
          <InfoCard>
            <div style={sectionTitleStyle}>Current Page</div>
            <div style={hintStyle}>No browser page selected.</div>
          </InfoCard>
        )}

        {result?.error && (
          <div style={errorStyle}>
            <AlertCircle size={14} />
            <span>{result.error}</span>
          </div>
        )}

        {screenshotError && (
          <div style={errorStyle}>
            <AlertCircle size={14} />
            <span>{screenshotError}</span>
          </div>
        )}

        {actionError && (
          <div style={errorStyle}>
            <AlertCircle size={14} />
            <span>{actionError}</span>
          </div>
        )}

        {actionMessage && (
          <div style={successStyle}>
            <Check size={14} />
            <span>{actionMessage}</span>
          </div>
        )}

        {selectedScreenshot && (
          <InfoCard>
            <div style={sectionTitleStyle}>Latest Screenshot</div>
            <div style={hintStyle}>
              {new Date(selectedScreenshot.capturedAt).toLocaleString()}
              {selectedScreenshot.width && selectedScreenshot.height ? ` · ${selectedScreenshot.width}×${selectedScreenshot.height}` : ""}
            </div>
            <div style={imageWrapStyle}>
              <img
                src={`data:${selectedScreenshot.mimeType};base64,${selectedScreenshot.data}`}
                alt={selectedScreenshot.title || selectedScreenshot.url || "Browser screenshot"}
                style={imageStyle}
              />
            </div>
          </InfoCard>
        )}

        <div style={advancedWrapStyle}>
          <button
            type="button"
            onClick={() => setAdvancedOpen((current) => !current)}
            style={advancedToggleStyle}
            aria-expanded={advancedOpen}
          >
            {advancedOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            <span>Advanced</span>
          </button>
          {advancedOpen && (
            <div style={advancedContentStyle}>
              {selectedTarget?.type === "page" && (
                <InfoCard>
                  <div style={sectionTitleStyle}>Selector Actions</div>
                  <div style={actionGridStyle}>
                    <div style={actionSectionStyle}>
                      <div style={actionLabelStyle}>Selector</div>
                      <input
                        type="text"
                        value={selector}
                        onChange={(event) => setSelector(event.target.value)}
                        spellCheck={false}
                        placeholder="#app button.primary"
                        style={fieldInputStyle}
                      />
                    </div>
                    <div style={actionSectionStyle}>
                      <div style={actionLabelStyle}>Type text</div>
                      <textarea
                        value={inputText}
                        onChange={(event) => setInputText(event.target.value)}
                        placeholder="Hello from MiniCode"
                        style={textAreaStyle}
                      />
                    </div>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      <button
                        type="button"
                        onClick={() => void runAction("click")}
                        disabled={actionLoading != null}
                        style={secondaryButtonStyle}
                      >
                        {actionLoading === "click" ? "Clicking..." : "Click Selector"}
                      </button>
                      <button
                        type="button"
                        onClick={() => void runAction("type")}
                        disabled={actionLoading != null}
                        style={secondaryButtonStyle}
                      >
                        {actionLoading === "type" ? "Typing..." : "Type Into Selector"}
                      </button>
                    </div>
                  </div>
                </InfoCard>
              )}
              <InfoCard>
                <div style={sectionTitleStyle}>Connection</div>
                <InfoRow label="Mode" value="External Chrome / CDP" />
                <InfoRow label="Endpoint" value={result?.endpoint ?? endpoint} mono />
                <InfoRow label="WebSocket" value={selectedTarget?.webSocketDebuggerUrl || "--"} mono />
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 4 }}>
                  <TinyButton
                    icon={<Copy size={12} />}
                    label="Copy WS"
                    onClick={() => {
                      if (selectedTarget?.webSocketDebuggerUrl) void navigator.clipboard?.writeText(selectedTarget.webSocketDebuggerUrl);
                    }}
                    disabled={!selectedTarget?.webSocketDebuggerUrl}
                  />
                </div>
                <pre style={codeStyle}>chrome.exe --remote-debugging-port=9222</pre>
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
  <div style={sectionStyle}>
    <div style={sectionTitleStyle}>{title}</div>
    {targets.length === 0 ? (
      <div style={emptyLineStyle}>No targets available.</div>
    ) : (
      <div style={{ display: "grid", gap: 8 }}>
        {targets.map((target) => (
          <div
            key={target.id || `${target.type}-${target.url}`}
            style={{
              ...targetCardStyle,
              borderColor: selectedTargetId === target.id ? "var(--accent-primary)" : "var(--border-subtle)",
              boxShadow: selectedTargetId === target.id ? "inset 0 0 0 1px color-mix(in oklch, var(--accent-primary) 45%, transparent)" : "none",
            }}
          >
            <div style={{ minWidth: 0 }}>
              <div style={targetTitleStyle}>{target.title || target.url || "Untitled target"}</div>
              <div style={targetMetaStyle}>{target.type}</div>
              {target.url && <div style={monoLineStyle}>{target.url}</div>}
              {target.webSocketDebuggerUrl && <div style={monoMutedStyle}>{target.webSocketDebuggerUrl}</div>}
            </div>
            <div style={targetActionsStyle}>
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
    style={{
      display: "inline-flex",
      alignItems: "center",
      gap: 5,
      padding: "4px 7px",
      borderRadius: "var(--radius-sm, 4px)",
      border: "1px solid var(--border-subtle)",
      background: "var(--surface-page)",
      color: disabled ? "var(--text-muted)" : "var(--text-secondary)",
      cursor: disabled ? "not-allowed" : "pointer",
      fontSize: "var(--text-xs)",
      whiteSpace: "nowrap",
    }}
  >
    {icon}
    {label}
  </button>
);

const InfoCard = ({ children }: { children: ReactNode }) => <div style={infoCardStyle}>{children}</div>;

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
    <div style={{ display: "grid", gridTemplateColumns: "88px minmax(0, 1fr)", gap: 8, fontSize: "var(--text-xs)" }}>
      <span style={{ color: "var(--text-muted)" }}>{label}</span>
      <span
        title={value}
        style={{
          color,
          fontFamily: mono ? "var(--font-mono)" : undefined,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {value}
      </span>
    </div>
  );
};

const panelStyle: CSSProperties = {
  flex: 1,
  minHeight: 0,
  display: "flex",
  flexDirection: "column",
};

const toolbarStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  padding: "8px 10px",
  borderBottom: "1px solid var(--border-subtle)",
};

const bodyStyle: CSSProperties = {
  flex: 1,
  overflowY: "auto",
  display: "grid",
  gap: 10,
  padding: 12,
};

const statusCardStyle: CSSProperties = {
  display: "grid",
  gap: 8,
  padding: 10,
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
};

const statusHeaderStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 10,
};

const browserNameStyle: CSSProperties = {
  marginTop: 3,
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  fontWeight: 650,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const statusPillStyle: CSSProperties = {
  flexShrink: 0,
  padding: "3px 7px",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  fontSize: "var(--text-xs)",
  fontWeight: 700,
};

const compactInfoGridStyle: CSSProperties = {
  display: "grid",
  gap: 5,
};

const currentPageStyle: CSSProperties = {
  display: "grid",
  gap: 10,
  padding: 10,
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
};

const currentPageHeaderStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  minWidth: 0,
};

const currentPageTitleStyle: CSSProperties = {
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  fontWeight: 700,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const currentPageUrlStyle: CSSProperties = {
  marginTop: 3,
  color: "var(--text-muted)",
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const targetTypeBadgeStyle: CSSProperties = {
  flexShrink: 0,
  color: "var(--accent-primary)",
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: "2px 6px",
  fontSize: 10,
  fontWeight: 750,
  textTransform: "uppercase",
};

const primaryActionsStyle: CSSProperties = {
  display: "flex",
  gap: 6,
  flexWrap: "wrap",
};

const primaryNavigateStyle: CSSProperties = {
  display: "flex",
  gap: 6,
  alignItems: "center",
};

const advancedWrapStyle: CSSProperties = {
  display: "grid",
  gap: 8,
};

const advancedToggleStyle: CSSProperties = {
  width: "100%",
  display: "flex",
  alignItems: "center",
  gap: 5,
  padding: "6px 8px",
  borderRadius: "var(--radius-sm, 4px)",
  border: "1px solid var(--border-subtle)",
  background: "var(--surface-page)",
  color: "var(--text-secondary)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  fontWeight: 700,
};

const advancedContentStyle: CSSProperties = {
  display: "grid",
  gap: 10,
};

const iconButtonStyle: CSSProperties = {
  width: 24,
  height: 24,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  borderRadius: "var(--radius-sm, 4px)",
  border: "1px solid var(--border-subtle)",
  background: "var(--surface-page)",
  color: "var(--text-secondary)",
  cursor: "pointer",
  padding: 0,
};

const inputStyle: CSSProperties = {
  flex: 1,
  minWidth: 0,
  height: 26,
  padding: "0 8px",
  borderRadius: "var(--radius-sm, 4px)",
  border: "1px solid var(--border-subtle)",
  background: "var(--surface-base)",
  color: "var(--text-primary)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
  outline: "none",
};

const primaryButtonStyle: CSSProperties = {
  border: 0,
  padding: "5px 10px",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--accent-primary)",
  color: "var(--surface-base)",
  fontSize: "var(--text-xs)",
  fontWeight: 700,
  cursor: "pointer",
};

const secondaryButtonStyle: CSSProperties = {
  border: "1px solid var(--border-subtle)",
  padding: "5px 10px",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-page)",
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
  fontWeight: 600,
  cursor: "pointer",
};

const sectionStyle: CSSProperties = {
  display: "grid",
  gap: 8,
};

const sectionTitleStyle: CSSProperties = {
  fontSize: "var(--text-xs)",
  textTransform: "uppercase",
  fontWeight: 700,
  color: "var(--text-muted)",
};

const hintStyle: CSSProperties = {
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.5,
};

const targetCardStyle: CSSProperties = {
  display: "grid",
  gap: 8,
  padding: 8,
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
};

const targetTitleStyle: CSSProperties = {
  fontSize: "var(--text-xs)",
  fontWeight: 700,
  color: "var(--text-primary)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const targetMetaStyle: CSSProperties = {
  fontSize: "10px",
  color: "var(--accent-primary)",
  textTransform: "uppercase",
  marginTop: 2,
};

const targetActionsStyle: CSSProperties = {
  display: "flex",
  gap: 6,
  flexWrap: "wrap",
};

const monoLineStyle: CSSProperties = {
  marginTop: 6,
  fontSize: "var(--text-xs)",
  color: "var(--text-secondary)",
  fontFamily: "var(--font-mono)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const monoMutedStyle: CSSProperties = {
  marginTop: 4,
  fontSize: "10px",
  color: "var(--text-muted)",
  fontFamily: "var(--font-mono)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const errorStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  color: "var(--state-danger)",
  background: "var(--state-danger-soft)",
  border: "1px solid var(--state-danger)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: 8,
  fontSize: "var(--text-xs)",
};

const successStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  color: "var(--state-success, var(--accent-primary))",
  background: "color-mix(in oklch, var(--accent-primary) 12%, transparent)",
  border: "1px solid color-mix(in oklch, var(--accent-primary) 55%, transparent)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: 8,
  fontSize: "var(--text-xs)",
};

const emptyWrapStyle: CSSProperties = {
  flex: 1,
  display: "grid",
  placeItems: "center",
  gap: 8,
  color: "var(--text-muted)",
  fontSize: "var(--text-sm)",
};

const emptyLineStyle: CSSProperties = {
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
};

const codeStyle: CSSProperties = {
  margin: 0,
  padding: 8,
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
  color: "var(--text-secondary)",
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
};

const infoCardStyle: CSSProperties = {
  display: "grid",
  gap: 6,
  padding: 8,
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
};

const imageWrapStyle: CSSProperties = {
  marginTop: 6,
  borderRadius: "var(--radius-sm, 6px)",
  overflow: "hidden",
  border: "1px solid var(--border-subtle)",
  background: "var(--surface-page)",
};

const imageStyle: CSSProperties = {
  display: "block",
  width: "100%",
  height: "auto",
  maxHeight: 420,
  objectFit: "contain",
};

const actionGridStyle: CSSProperties = {
  display: "grid",
  gap: 8,
  marginTop: 8,
};

const actionSectionStyle: CSSProperties = {
  display: "grid",
  gap: 6,
};

const actionLabelStyle: CSSProperties = {
  fontSize: "10px",
  fontWeight: 700,
  letterSpacing: "0.04em",
  textTransform: "uppercase",
  color: "var(--text-muted)",
};

const fieldInputStyle: CSSProperties = {
  flex: 1,
  minWidth: 0,
  height: 28,
  padding: "0 8px",
  borderRadius: "var(--radius-sm, 4px)",
  border: "1px solid var(--border-subtle)",
  background: "var(--surface-page)",
  color: "var(--text-primary)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
  outline: "none",
};

const textAreaStyle: CSSProperties = {
  minHeight: 64,
  resize: "vertical",
  padding: 8,
  borderRadius: "var(--radius-sm, 4px)",
  border: "1px solid var(--border-subtle)",
  background: "var(--surface-page)",
  color: "var(--text-primary)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
  outline: "none",
};
