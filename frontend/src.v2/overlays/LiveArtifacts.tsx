import { GitCompare, Image as ImageIcon, Layers, Paperclip, RotateCcw, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { fileIcon } from "../lib/file-icons";
import { useAppStore } from "../stores";
import type { ArtifactPreview, ChatMessage } from "../stores/types";
import type { ToolCallRecord } from "../lib/tool-call-reducer";
import { openArtifactPreview } from "../chat/openAttachmentPreview";
import { getWebSocket } from "../hooks/useWebSocket";
import { useEscapeKey, useFocusTrap } from "../hooks/useFocusTrap";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import { revealConversationMessage } from "../chat/revealConversationMessage";
import {
  artifactMediaTypeForProjection,
  artifactSummaryForRecord,
  canonicalArtifactKind,
  cleanArtifactLabel,
  isExecutionResultArtifact,
  isExecutionStatusLabel,
  isExecutionOutputMediaType,
  normalizeArtifactPreview,
} from "../lib/artifact-projection";
import {
  artifactResourceUrl,
  withPreviewCacheBust,
} from "../lib/artifact-resource";

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
  conversationId?: string;
  executionResult?: boolean;
  turnId?: string;
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
  const conversationId = useAppStore((s) => s.conversationId);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [recentOnly, setRecentOnly] = useState(false);
  const dialogRef = useFocusTrap(liveArtifactsOpen);
  useEscapeKey(toggleLiveArtifacts, liveArtifactsOpen, dialogRef);

  const artifacts = useMemo<ArtifactEntry[]>(() => {
    return collectLiveArtifacts(messages, conversationId?.trim() || undefined);
  }, [conversationId, messages]);
  const filtered = artifacts.filter((artifact) => `${artifact.summary} ${artifact.turnId || ""}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
    && (typeFilter === "all" || (typeFilter === "execution" ? artifact.executionResult : artifact.kind === typeFilter)));
  const visible = recentOnly ? filtered.slice(0, 12) : filtered;

  if (!liveArtifactsOpen) return null;

  const openArtifact = (artifact: ArtifactEntry) => {
    if (!artifact.conversationId) return;
    openArtifactPreview({
      artifactId: artifact.artifactId,
      name: artifact.summary,
      summary: artifact.summary,
      mediaType: artifact.mediaType,
      kind: artifact.kind,
      conversationId: artifact.conversationId,
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

        <div style={{ display: "flex", gap: 8, padding: "10px 18px", flexWrap: "wrap", alignItems: "center" }}>
          <input aria-label="搜索实时制品" placeholder="搜索文件名或轮次…" value={query} onChange={(event) => setQuery(event.target.value)} style={{ ...filterStyle, flex: "1 1 200px" }} />
          <select aria-label="实时制品类型" value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)} style={filterStyle}>
            <option value="all">全部类型</option>{Object.entries(KIND_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}<option value="execution">执行结果</option>
          </select>
          <label style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}><input type="checkbox" checked={recentOnly} onChange={(event) => setRecentOnly(event.target.checked)} />仅最近 12 项</label>
        </div>

        <div style={listWrapStyle}>
          {visible.length === 0 ? (
            <div style={{ padding: "40px 16px", textAlign: "center" }}>
              <div style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", marginBottom: 6 }}>{artifacts.length === 0 ? "暂无制品" : "没有匹配的制品"}</div>
              <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>{artifacts.length === 0 ? "当助手生成文件、差异或图像时，会出现在这里。" : "尝试其他名称或类型。"}</div>
            </div>
          ) : (
            <div style={gridStyle}>
              {visible.map((artifact) => {
                const Icon = artifact.kind === "diff" ? GitCompare : Paperclip;
                const sizeLabel = artifact.bytes
                  ? artifact.bytes > 1024
                    ? `${(artifact.bytes / 1024).toFixed(1)} KB`
                    : `${artifact.bytes} B`
                  : null;
                return (
                  <LiveArtifactCard
                    key={artifact.artifactId}
                    artifact={artifact}
                    icon={Icon}
                    sizeLabel={sizeLabel}
                    onOpen={() => openArtifact(artifact)}
                    onReturn={() => {
                      toggleLiveArtifacts();
                      revealConversationMessage(artifact.conversationId!, artifact.messageId);
                    }}
                  />
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export function collectLiveArtifacts(
  messages: ChatMessage[],
  ownerConversationId?: string,
): ArtifactEntry[] {
  const out: ArtifactEntry[] = [];
  const indexes = new Map<string, number>();
  const upsert = (entry: ArtifactEntry): void => {
    const existingIndex = indexes.get(entry.artifactId);
    if (existingIndex === undefined) {
      indexes.set(entry.artifactId, out.length);
      out.push(entry);
      return;
    }
    out[existingIndex] = mergeLiveArtifact(out[existingIndex], entry);
  };

  for (const message of messages) {
    for (const rawArtifact of message.artifacts ?? []) {
      const artifact = normalizeArtifactPreview(rawArtifact);
      const kind = canonicalArtifactKind(artifact.kind, artifact.mediaType);
      const mediaType = artifactMediaTypeForProjection(artifact.mediaType, kind);
      upsert({
        ...artifact,
        kind,
        summary: cleanArtifactLabel(artifact.summary) || (kind === "image" ? "生成图片" : "生成文件"),
        mediaType,
        url: artifact.url,
        messageId: message.id,
        turnId: message.turnId,
        timestamp: message.timestamp,
        conversationId: ownerConversationId,
      });
    }
    for (const record of getToolCallsFromMessage(message)) {
      const entry = liveArtifactFromToolRecord(
        message.id,
        message.timestamp,
        record,
        ownerConversationId,
      );
      if (entry) upsert({ ...entry, turnId: message.turnId });
    }
  }
  return out.reverse();
}

function liveArtifactFromToolRecord(
  messageId: string,
  timestamp: number,
  record: ToolCallRecord,
  conversationId?: string,
): ArtifactEntry | null {
  const artifactId = String(record.artifactId || "").trim();
  if (!artifactId) return null;
  const kind = canonicalArtifactKind(record.artifactKind, record.artifactMediaType, record);
  const mediaType = artifactMediaTypeForProjection(record.artifactMediaType, kind);
  return {
    artifactId,
    kind,
    summary: artifactSummaryForRecord(record),
    bytes: typeof record.artifactBytes === "number" ? record.artifactBytes : undefined,
    mediaType,
    url: undefined,
    messageId,
    timestamp,
    conversationId,
    executionResult: isExecutionResultArtifact(record),
  };
}

function mergeLiveArtifact(existing: ArtifactEntry, incoming: ArtifactEntry): ArtifactEntry {
  const kind = existing.kind === "image" || incoming.kind === "image"
    ? "image"
    : existing.kind === "file" && incoming.kind !== "file"
      ? incoming.kind
      : existing.kind;
  const executionResult = kind !== "image"
    && isExecutionOutputMediaType(existing.mediaType || incoming.mediaType)
    && Boolean(existing.executionResult || incoming.executionResult);
  return {
    ...existing,
    kind,
    summary: (isPlaceholderSummary(existing.summary) || (incoming.executionResult && isExecutionStatusLabel(existing.summary)))
      && !(incoming.executionResult && !executionResult)
      ? incoming.summary : existing.summary,
    mediaType: incoming.kind === "image"
      ? incoming.mediaType || existing.mediaType
      : existing.mediaType || incoming.mediaType,
    url: incoming.url || existing.url,
    bytes: existing.bytes ?? incoming.bytes,
    conversationId: existing.conversationId || incoming.conversationId,
    messageId: incoming.messageId,
    timestamp: incoming.timestamp,
    turnId: incoming.turnId || existing.turnId,
    executionResult,
  };
}

function isPlaceholderSummary(value: string): boolean {
  return !cleanArtifactLabel(value) || value === "生成文件" || value === "生成图片" || value === "未命名产物";
}

function LiveArtifactCard({
  artifact,
  icon: Icon,
  sizeLabel,
  onOpen,
  onReturn,
}: {
  artifact: ArtifactEntry;
  icon: typeof Paperclip;
  sizeLabel: string | null;
  onOpen: () => void;
  onReturn: () => void;
}) {
  const [reloadNonce, setReloadNonce] = useState(0);
  const [failed, setFailed] = useState(false);
  const sessionId = getWebSocket()?.sessionId?.trim() || "";
  const isConnected = useAppStore((s) => s.isConnected);
  const freshUrl = useMemo(() => artifactResourceUrl({
    artifactId: artifact.artifactId,
    conversationId: artifact.conversationId,
    sessionId,
    source: "artifact",
    originalUrl: artifact.url,
    isConnected,
  }), [artifact.artifactId, artifact.conversationId, artifact.url, isConnected, sessionId, reloadNonce]);
  useEffect(() => {
    setReloadNonce(0);
    setFailed(false);
  }, [artifact.artifactId, artifact.conversationId, artifact.url, isConnected, sessionId]);
  const imageUrl = withPreviewCacheBust(freshUrl, reloadNonce);
  const retry = () => {
    setFailed(false);
    setReloadNonce((value) => value + 1);
  };
  const fileName = artifact.kind === "json" ? "file.json"
      : artifact.kind === "text" ? "file.txt"
        : artifact.summary || "file";
  const media = !imageUrl || failed ? (
    <span
      style={thumbnailFallbackStyle}
      role={failed ? "status" : undefined}
      data-artifact-id={artifact.artifactId}
      data-artifact-conversation-id={artifact.conversationId || undefined}
    >
      <ImageIcon size={18} aria-hidden="true" />
      <span>{!artifact.conversationId
        ? "未关联会话"
        : !isConnected || !sessionId
          ? "连接恢复后载入"
          : "图片加载失败"}</span>
    </span>
  ) : (
    <img
      src={imageUrl}
      alt={artifact.summary}
      style={thumbStyle}
      data-artifact-id={artifact.artifactId}
      data-artifact-conversation-id={artifact.conversationId || undefined}
      onError={() => {
        if (reloadNonce === 0) setReloadNonce(1);
        else setFailed(true);
      }}
    />
  );

  return (
    <div style={cardRowStyle}>
      <button
        type="button"
        onClick={onOpen}
        style={cardOpenStyle}
        aria-label={`打开制品：${artifact.summary || "未命名"}`}
      >
        {artifact.kind === "image" ? media : (
          <span style={iconWrapStyle}>
            {["file", "json", "code", "text"].includes(artifact.kind)
              ? fileIcon(fileName, { size: 18, className: "live-artifact-file-icon" })
              : <Icon size={18} />}
          </span>
        )}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={summaryStyle} title={artifact.summary}>{artifact.summary || "（未命名）"}</div>
          <div style={metaRowStyle}>
            <span style={kindTagStyle}>{KIND_LABEL[artifact.kind] || artifact.kind}</span>
            {sizeLabel && <span style={{ color: "var(--text-muted)" }}>{sizeLabel}</span>}
          </div>
          <div style={{ ...metaRowStyle, flexWrap: "wrap", color: "var(--text-muted)" }} title={artifact.turnId || artifact.messageId}>
            {artifact.turnId ? `轮次 ${artifact.turnId.slice(-8)}` : `消息 ${artifact.messageId.slice(-8)}`} · {new Date(artifact.timestamp).toLocaleString()}
            {artifact.executionResult && <span>执行结果</span>}
          </div>
        </div>
      </button>
      {artifact.conversationId && <button type="button" onClick={onReturn} style={{ ...closeBtn, width: "auto", alignSelf: "center", marginRight: 7, padding: "3px 6px", whiteSpace: "nowrap", fontSize: "var(--text-xs)" }}>回到对话</button>}
      {failed && (
        <button
          type="button"
          onClick={retry}
          aria-label="重试图片"
          title="重试图片"
          style={thumbnailRetryButtonStyle}
        >
          <RotateCcw size={14} aria-hidden="true" />
        </button>
      )}
    </div>
  );
}

const backdropStyle: React.CSSProperties = { position: "fixed", inset: 0, background: "var(--backdrop-overlay)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: "var(--z-modal)", pointerEvents: "auto" };
const modalStyle: React.CSSProperties = { width: "min(760px, calc(100vw - 24px))", maxHeight: "84vh", background: "var(--surface-base)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-lg)", boxShadow: "var(--shadow-strong-overlay)", overflow: "hidden", display: "flex", flexDirection: "column", pointerEvents: "auto" };
const headerStyle: React.CSSProperties = { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "14px 18px", borderBottom: "1px solid var(--border-subtle)" };
const closeBtn: React.CSSProperties = { background: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 6px)", color: "var(--text-muted)", cursor: "pointer", width: 30, height: 30, padding: 0, display: "inline-flex", alignItems: "center", justifyContent: "center" };
const listWrapStyle: React.CSSProperties = { flex: 1, overflowY: "auto", padding: "14px 18px" };
const gridStyle: React.CSSProperties = { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(220px, 100%), 1fr))", gap: 10 };
const cardRowStyle: React.CSSProperties = { display: "flex", alignItems: "stretch", gap: 6, padding: 0, background: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 8px)", width: "100%", minWidth: 0 };
const cardOpenStyle: React.CSSProperties = { display: "flex", alignItems: "center", gap: 11, padding: "10px 12px", background: "transparent", border: 0, borderRadius: "var(--radius-sm, 8px)", cursor: "pointer", textAlign: "left", color: "var(--text-primary)", flex: 1, minWidth: 0 };
const iconWrapStyle: React.CSSProperties = { width: 38, height: 38, display: "inline-flex", alignItems: "center", justifyContent: "center", borderRadius: "var(--radius-sm, 6px)", background: "var(--surface-base)", color: "var(--text-muted)", border: "1px solid var(--border-subtle)", flexShrink: 0 };
const thumbStyle: React.CSSProperties = { width: 38, height: 38, objectFit: "cover", borderRadius: "var(--radius-sm, 6px)", border: "1px solid var(--border-subtle)", flexShrink: 0 };
const thumbnailFallbackStyle: React.CSSProperties = { ...iconWrapStyle, width: 38, height: 38, flexDirection: "column", gap: 2, fontSize: "var(--text-3xs)", textAlign: "center" };
const thumbnailRetryButtonStyle: React.CSSProperties = { alignSelf: "center", marginRight: 8, background: "var(--surface-base)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 6px)", color: "var(--accent-primary)", cursor: "pointer", width: 28, height: 28, padding: 0, display: "inline-flex", alignItems: "center", justifyContent: "center", flexShrink: 0 };
const summaryStyle: React.CSSProperties = { fontSize: "var(--text-sm)", fontWeight: "var(--fw-semibold)", color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const metaRowStyle: React.CSSProperties = { display: "flex", alignItems: "center", gap: 7, marginTop: 4, fontSize: "var(--text-xs)" };
const kindTagStyle: React.CSSProperties = { fontSize: "var(--text-3xs)", fontFamily: "var(--font-ui)", padding: "1px 6px", borderRadius: "var(--radius-sm, 4px)", background: "var(--surface-base)", color: "var(--text-muted)", border: "1px solid var(--border-subtle)" };
const filterStyle: React.CSSProperties = { minWidth: 0, border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", padding: "7px 9px", background: "var(--surface-base)", color: "var(--text-primary)", font: "inherit", fontSize: "var(--text-xs)" };
