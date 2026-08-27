import { Braces, Code2, FileText, FileType, GitCompare, Image as ImageIcon, Layers, Paperclip, X } from "lucide-react";
import { useMemo } from "react";
import { useAppStore } from "../stores";
import type { ArtifactPreview } from "../stores/types";
import { openArtifactPreview } from "../chat/openAttachmentPreview";
import { useEscapeKey, useFocusTrap } from "../hooks/useFocusTrap";

const KIND_ICON = {
  file: FileText,
  diff: GitCompare,
  image: ImageIcon,
  json: Braces,
  code: Code2,
  text: FileText,
  pdf: FileType,
} as const;

const KIND_LABEL: Record<string, string> = {
  file: "文件",
  diff: "差异",
  image: "图像",
  json: "JSON",
  code: "代码",
  text: "文本",
  pdf: "PDF",
};

interface ArtifactEntry extends ArtifactPreview {
  messageId: string;
  timestamp: number;
}

/**
 * Live artifacts browser: aggregates every artifact produced across the active
 * conversation into one scrollable gallery. Opening an entry routes it to the
 * preview panel (same path as ArtifactCard) and closes the overlay.
 */
export const LiveArtifacts = () => {
  const liveArtifactsOpen = useAppStore((s) => s.liveArtifactsOpen);
  const toggleLiveArtifacts = useAppStore((s) => s.toggleLiveArtifacts);
  const messages = useAppStore((s) => s.messages);
  const dialogRef = useFocusTrap(liveArtifactsOpen);
  useEscapeKey(toggleLiveArtifacts, liveArtifactsOpen);

  const artifacts = useMemo<ArtifactEntry[]>(() => {
    const seen = new Set<string>();
    const out: ArtifactEntry[] = [];
    for (const message of messages) {
      for (const artifact of message.artifacts ?? []) {
        if (seen.has(artifact.artifactId)) continue;
        seen.add(artifact.artifactId);
        out.push({ ...artifact, messageId: message.id, timestamp: message.timestamp });
      }
    }
    return out.reverse();
  }, [messages]);

  if (!liveArtifactsOpen) return null;

  const openArtifact = (artifact: ArtifactEntry) => {
    const store = useAppStore.getState();
    openArtifactPreview({
      artifactId: artifact.artifactId,
      name: artifact.summary,
      summary: artifact.summary,
      mediaType: artifact.mediaType,
      kind: artifact.kind,
      conversationId: store.conversationId || undefined,
    });
    toggleLiveArtifacts();
  };

  return (
    <div className="overlay-backdrop" onClick={toggleLiveArtifacts} style={backdropStyle}>
      <div
        ref={dialogRef}
        className="modal-content"
        role="dialog"
        aria-modal="true"
        aria-labelledby="live-artifacts-title"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        style={modalStyle}
      >
        <div style={headerStyle}>
          <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
            <Layers size={18} style={{ color: "var(--accent-primary)" }} />
            <div>
              <h2 id="live-artifacts-title" style={{ margin: 0, fontSize: "var(--text-lg)", color: "var(--text-primary)", fontWeight: "var(--fw-bold)" }}>实时制品</h2>
              <div style={{ marginTop: 2, fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
                当前对话生成的全部文件、差异与图像
              </div>
            </div>
          </div>
          <button type="button" onClick={toggleLiveArtifacts} aria-label="关闭实时制品" style={closeBtn}><X size={16} /></button>
        </div>

        <div style={listWrapStyle}>
          {artifacts.length === 0 ? (
            <div style={{ padding: "40px 16px", textAlign: "center" }}>
              <div style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", marginBottom: 6 }}>暂无制品</div>
              <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>当助手生成文件、差异或图像时，会出现在这里。</div>
            </div>
          ) : (
            <div style={gridStyle}>
              {artifacts.map((artifact) => {
                const Icon = KIND_ICON[artifact.kind] ?? Paperclip;
                const sizeLabel = artifact.bytes
                  ? artifact.bytes > 1024
                    ? `${(artifact.bytes / 1024).toFixed(1)} KB`
                    : `${artifact.bytes} B`
                  : null;
                return (
                  <button type="button" key={artifact.artifactId} onClick={() => openArtifact(artifact)} style={cardStyle}>
                    {artifact.kind === "image" && artifact.url ? (
                      <img src={artifact.url} alt={artifact.summary} style={thumbStyle} />
                    ) : (
                      <span style={iconWrapStyle}><Icon size={18} /></span>
                    )}
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={summaryStyle} title={artifact.summary}>{artifact.summary || "（未命名）"}</div>
                      <div style={metaRowStyle}>
                        <span style={kindTagStyle}>{KIND_LABEL[artifact.kind] || artifact.kind}</span>
                        {sizeLabel && <span style={{ color: "var(--text-muted)" }}>{sizeLabel}</span>}
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

const backdropStyle: React.CSSProperties = { position: "fixed", inset: 0, background: "var(--backdrop-overlay)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: "var(--z-modal)", pointerEvents: "auto" };
const modalStyle: React.CSSProperties = { width: "min(760px, 94vw)", maxHeight: "84vh", background: "var(--surface-base)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-lg)", boxShadow: "var(--shadow-strong-overlay)", overflow: "hidden", display: "flex", flexDirection: "column", pointerEvents: "auto" };
const headerStyle: React.CSSProperties = { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "14px 18px", borderBottom: "1px solid var(--border-subtle)" };
const closeBtn: React.CSSProperties = { background: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 6px)", color: "var(--text-muted)", cursor: "pointer", width: 30, height: 30, padding: 0, display: "inline-flex", alignItems: "center", justifyContent: "center" };
const listWrapStyle: React.CSSProperties = { flex: 1, overflowY: "auto", padding: "14px 18px" };
const gridStyle: React.CSSProperties = { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 10 };
const cardStyle: React.CSSProperties = { display: "flex", alignItems: "center", gap: 11, padding: "10px 12px", background: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 8px)", cursor: "pointer", textAlign: "left", color: "var(--text-primary)", width: "100%" };
const iconWrapStyle: React.CSSProperties = { width: 38, height: 38, display: "inline-flex", alignItems: "center", justifyContent: "center", borderRadius: "var(--radius-sm, 6px)", background: "var(--surface-base)", color: "var(--text-muted)", border: "1px solid var(--border-subtle)", flexShrink: 0 };
const thumbStyle: React.CSSProperties = { width: 38, height: 38, objectFit: "cover", borderRadius: "var(--radius-sm, 6px)", border: "1px solid var(--border-subtle)", flexShrink: 0 };
const summaryStyle: React.CSSProperties = { fontSize: "var(--text-sm)", fontWeight: "var(--fw-semibold)", color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const metaRowStyle: React.CSSProperties = { display: "flex", alignItems: "center", gap: 7, marginTop: 4, fontSize: "var(--text-xs)" };
const kindTagStyle: React.CSSProperties = { fontSize: "var(--text-3xs)", fontFamily: "var(--font-ui)", padding: "1px 6px", borderRadius: "var(--radius-sm, 4px)", background: "var(--surface-base)", color: "var(--text-muted)", border: "1px solid var(--border-subtle)" };
