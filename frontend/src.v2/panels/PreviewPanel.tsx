import { ClipboardCopy, FileText, LoaderCircle, TriangleAlert } from "lucide-react";
import { lazy, Suspense, type CSSProperties, type ReactNode } from "react";
import { MarkdownRenderer } from "../chat/messages/MarkdownRenderer";
import { safeJsonParse } from "../lib/safe-parse";
import { useAppStore } from "../stores";

const PdfAttachmentPreview = lazy(() =>
  import("./PdfAttachmentPreview").then((module) => ({ default: module.PdfAttachmentPreview })),
);

const isLikelyJson = (content: string): boolean => {
  const trimmed = content.trim();
  return (trimmed.startsWith("{") && trimmed.endsWith("}"))
    || (trimmed.startsWith("[") && trimmed.endsWith("]"));
};

const prettyContent = (content: string): string => {
  if (!isLikelyJson(content)) return content;
  const parsed = safeJsonParse<unknown>(content, undefined);
  return parsed === undefined ? content : JSON.stringify(parsed, null, 2);
};

const isSafePreviewImageUrl = (url: string): boolean => {
  const trimmed = url.trim();
  if (/^data:image\/(?:png|jpe?g|gif|webp|avif);base64,[a-z0-9+/=\s]+$/i.test(trimmed)) return true;
  try {
    return ["http:", "https:", "blob:"].includes(new URL(trimmed).protocol);
  } catch {
    return false;
  }
};

const isSafePreviewDocumentUrl = (url: string): boolean => {
  try {
    return ["http:", "https:", "blob:"].includes(new URL(url.trim()).protocol);
  } catch {
    return false;
  }
};

const isSafePreviewImageArtifact = (mediaType?: string, url?: string): boolean =>
  Boolean(
    mediaType?.startsWith("image/")
      && mediaType.toLowerCase() !== "image/svg+xml"
      && url
      && isSafePreviewImageUrl(url),
  );

export const PreviewPanel = () => (
  <div className="flex flex-col flex-1 min-h-0 min-w-0" data-preview-mode="artifact">
    <ArtifactView />
  </div>
);

const ArtifactView = () => {
  const previewArtifact = useAppStore((state) => state.previewArtifact);

  if (!previewArtifact) {
    return <ArtifactState icon={<FileText size={18} />} label="在对话中打开文件后，可在这里查看完整内容。" />;
  }

  const artifactUrl = previewArtifact.url ?? "";
  const name = previewArtifact.name || "生成文件";
  const sizeLabel = formatArtifactSize(previewArtifact.sizeBytes, previewArtifact.content.length);
  const rawContent = String(previewArtifact.content || "");
  const warning = String(previewArtifact.warning || "").trim();
  const hasContent = rawContent.trim().length > 0;
  const isBinary = previewArtifact.kind === "binary"
    || String(previewArtifact.mediaType || "").toLowerCase() === "application/octet-stream";
  const contentIsDiagnostic = Boolean(warning && rawContent.trim() === warning);

  if (previewArtifact.loading) {
    return <ArtifactFrame name={name} sizeLabel="加载中"><ArtifactState icon={<LoaderCircle size={18} className="animate-spin" />} label="正在加载附件预览" /></ArtifactFrame>;
  }
  if (previewArtifact.error) {
    return <ArtifactFrame name={name} sizeLabel="无法预览"><ArtifactState icon={<TriangleAlert size={18} />} label={previewArtifact.error} danger /></ArtifactFrame>;
  }
  if (isSafePreviewImageArtifact(previewArtifact.mediaType, artifactUrl)) {
    return (
      <ArtifactFrame name={previewArtifact.name || "生成图片"} sizeLabel={sizeLabel || previewArtifact.mediaType || "图片"}>
        <div style={imageContentStyle}><img src={artifactUrl} alt={previewArtifact.name || "生成图片"} style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain" }} /></div>
      </ArtifactFrame>
    );
  }
  if (previewArtifact.url && previewArtifact.mediaType === "application/pdf" && isSafePreviewDocumentUrl(previewArtifact.url)) {
    return (
      <ArtifactFrame name={previewArtifact.name || "生成的 PDF"} sizeLabel={sizeLabel ? `PDF · ${sizeLabel}` : "PDF"}>
        <Suspense fallback={<ArtifactState icon={<LoaderCircle size={18} className="animate-spin" />} label="正在准备 PDF 预览" />}>
          <PdfAttachmentPreview key={previewArtifact.url} url={previewArtifact.url} name={previewArtifact.name || "生成的 PDF"} />
        </Suspense>
      </ArtifactFrame>
    );
  }

  const content = prettyContent(rawContent);
  const richExtracted = isRichExtractedPreview(previewArtifact.mediaType);
  const canCopy = hasContent && !isBinary && !contentIsDiagnostic;
  const header = <ArtifactHeader artifactId={name} sizeLabel={previewTypeLabel(previewArtifact.mediaType, previewArtifact.kind, sizeLabel)} onCopy={canCopy ? () => void navigator.clipboard?.writeText(rawContent) : undefined} />;

  if (isBinary) {
    return <ArtifactFrame header={header}><ArtifactWarning message={warning} /><ArtifactState icon={<FileText size={18} />} label="不支持应用内预览。此文件是二进制文件，无法提取可显示文本。" /></ArtifactFrame>;
  }
  if (!hasContent || contentIsDiagnostic) {
    return <ArtifactFrame header={header}><ArtifactWarning message={warning} danger /><ArtifactState icon={<FileText size={18} />} label={warning ? "附件内容暂时无法提取或预览。" : "没有可显示的文本内容。"} danger={Boolean(warning)} /></ArtifactFrame>;
  }

  return (
    <ArtifactFrame header={header}>
      {warning && <ArtifactWarning message={warning} />}
      {previewArtifact.preview && previewArtifact.preview !== rawContent && <div style={artifactNoticeStyle}>{previewArtifact.preview}</div>}
      {previewArtifact.truncated && <div style={artifactNoticeStyle}>文件较大，仅显示前 {rawContent.length.toLocaleString()} 个字符。</div>}
      {richExtracted ? <div style={artifactRichContentStyle}><MarkdownRenderer content={content} /></div> : <pre style={textContentStyle}>{content}</pre>}
    </ArtifactFrame>
  );
};

const ArtifactFrame = ({
  name,
  sizeLabel,
  header,
  children,
}: {
  name?: string;
  sizeLabel?: string;
  header?: ReactNode;
  children: ReactNode;
}) => (
  <div style={frameStyle}>
    {header ?? <ArtifactHeader artifactId={name || "生成文件"} sizeLabel={sizeLabel || ""} />}
    {children}
  </div>
);

const ArtifactWarning = ({ message, danger = false }: { message: string; danger?: boolean }) =>
  message ? <div role="status" style={{ ...artifactNoticeStyle, color: danger ? "var(--state-danger)" : "var(--state-warning)" }}>{message}</div> : null;

const ArtifactState = ({ icon, label, danger = false }: { icon: ReactNode; label: string; danger?: boolean }) => (
  <div style={{ ...stateStyle, color: danger ? "var(--state-danger)" : "var(--text-muted)" }}>{icon}<span>{label}</span></div>
);

const ArtifactHeader = ({ artifactId, sizeLabel, onCopy }: { artifactId: string; sizeLabel: string; onCopy?: () => void }) => (
  <div style={headerStyle}>
    <span title={artifactId} style={headerNameStyle}>{artifactId}</span>
    <span style={{ color: "var(--text-muted)" }}>{sizeLabel}</span>
    {onCopy && <button type="button" title="复制文件内容" aria-label="复制文件内容" onClick={onCopy} style={copyButtonStyle}><ClipboardCopy size={14} /></button>}
  </div>
);

const formatArtifactSize = (sizeBytes?: number, contentLength = 0): string => {
  const bytes = Math.max(0, Number(sizeBytes || 0)) || Math.max(0, contentLength);
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

const isRichExtractedPreview = (mediaType?: string): boolean => {
  const media = String(mediaType || "").toLowerCase();
  return media === "text/markdown" || media.includes("wordprocessingml") || media.includes("msword") || media.includes("spreadsheetml") || media.includes("presentation") || media === "application/zip";
};

const previewTypeLabel = (mediaType?: string, kind?: string, size = ""): string => {
  const media = String(mediaType || "").toLowerCase();
  let label = "文本";
  if (kind === "binary" || media === "application/octet-stream") label = "二进制 · 不支持预览";
  else if (media.includes("word")) label = "Word · 提取文本";
  else if (media.includes("spreadsheet")) label = "Excel · 提取文本";
  else if (media.includes("presentation") || kind === "presentation") label = "演示文稿 · 提取文本";
  else if (media === "application/zip" || kind === "archive") label = "ZIP · 内容预览";
  else if (media === "text/markdown") label = "Markdown";
  else if (media.includes("json")) label = "JSON";
  return size ? `${label} · ${size}` : label;
};

const frameStyle: CSSProperties = { flex: 1, minHeight: 0, display: "flex", flexDirection: "column" };
const imageContentStyle: CSSProperties = { flex: 1, minHeight: 0, overflow: "auto", display: "grid", placeItems: "center", background: "var(--surface-base)" };
const textContentStyle: CSSProperties = { flex: 1, minHeight: 0, margin: 0, overflow: "auto", padding: 12, background: "var(--surface-base)", color: "var(--text-primary)", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", lineHeight: 1.55, whiteSpace: "pre-wrap", wordBreak: "break-word" };
const stateStyle: CSSProperties = { flex: 1, minHeight: 0, display: "grid", placeContent: "center", justifyItems: "center", gap: 8, padding: 20, fontSize: "var(--text-sm)", textAlign: "center" };
const headerStyle: CSSProperties = { display: "flex", alignItems: "center", gap: 8, padding: "6px 10px", borderBottom: "1px solid var(--border-subtle)", background: "var(--surface-page)", fontSize: "var(--text-xs)" };
const headerNameStyle: CSSProperties = { flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontFamily: "var(--font-mono)", color: "var(--text-secondary)" };
const copyButtonStyle: CSSProperties = { width: 26, height: 24, border: 0, borderRadius: "var(--radius-sm, 4px)", background: "transparent", color: "var(--text-muted)", display: "inline-flex", alignItems: "center", justifyContent: "center", cursor: "pointer" };
const artifactNoticeStyle: CSSProperties = { padding: "6px 10px", borderBottom: "1px solid var(--border-subtle)", background: "var(--surface-soft)", color: "var(--text-muted)", fontSize: "var(--text-xs)" };
const artifactRichContentStyle: CSSProperties = { flex: 1, minHeight: 0, overflow: "auto", padding: "14px 16px", background: "var(--surface-base)", color: "var(--text-primary)" };
