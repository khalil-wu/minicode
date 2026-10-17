import { Component, useEffect, useRef, useState, type ReactNode } from "react";
import { AnnotationMode, GlobalWorkerOptions, PasswordResponses, getDocument, type PDFDocumentProxy, type PDFPageProxy } from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { useI18n } from "../../i18n";
import { ChevronLeft, ChevronRight } from "../../lib/icons";
import { Tip } from "../../components/Tooltip";

GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

export interface DiffPdfPreviewProps {
  oldUrl?: string;
  newUrl?: string;
  oldLoading?: boolean;
  newLoading?: boolean;
  status?: "new" | "deleted" | "changed";
}

/** Source isolates native PDF rendering in its PdfPreviewPanel boundary. */
class PdfPreviewBoundary extends Component<{ children: ReactNode; fallback: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() { return this.state.failed ? this.props.fallback : this.props.children; }
}

/** Source PDF canvas: preserve printed annotations without interactive layers. */
function PdfPage({ document, pageNumber, width }: { document: PDFDocumentProxy; pageNumber: number; width: number | null }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [page, setPage] = useState<PDFPageProxy | null>(null);
  const [pageError, setPageError] = useState(false);

  useEffect(() => {
    let active = true;
    setPage(null);
    setPageError(false);
    document.getPage(pageNumber).then((next) => {
      if (active) setPage(next);
    }, (reason: unknown) => {
      if (active) { console.warn(reason); setPageError(true); }
    });
    return () => { active = false; };
  }, [document, pageNumber]);

  useEffect(() => {
    if (page === null) return;
    const canvas = canvasRef.current!;
    const natural = page.getViewport({ scale: 1 });
    const scale = width !== null && width > 0 ? width / natural.width : 1;
    const viewport = page.getViewport({ scale });
    const pixels = page.getViewport({ scale: scale * window.devicePixelRatio });
    page.cleanup();
    canvas.width = pixels.width;
    canvas.height = pixels.height;
    canvas.style.width = `${Math.floor(viewport.width)}px`;
    canvas.style.height = `${Math.floor(viewport.height)}px`;
    canvas.style.visibility = "hidden";
    const renderTask = page.render({ canvasContext: canvas.getContext("2d", { alpha: false })!, viewport: pixels,
      annotationMode: AnnotationMode.ENABLE });
    let active = true;
    renderTask.promise.then(() => {
      if (active) canvas.style.visibility = "";
    }, (reason: unknown) => {
      if (active) console.warn(reason);
    });
    return () => { active = false; renderTask.cancel(); canvas.width = 0; canvas.height = 0; };
  }, [page, width]);

  return <div className="relative overflow-hidden rounded-sm bg-white shadow-sm" data-page-number={pageNumber}>
    {page === null && <div>{pageError ? "Failed to load the page." : "Loading page…"}</div>}
    <canvas ref={canvasRef} dir="ltr" className={page === null ? "hidden" : "block select-none"} />
  </div>;
}

function PdfSide({ url, side, loading }: { url: string | undefined; side: "before" | "after"; loading: boolean }) {
  const { locale, t } = useI18n();
  const widthRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState<number | null>(null);
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [documentError, setDocumentError] = useState(false);
  const [pageNumber, setPageNumber] = useState(1);
  const empty = locale === "zh-CN" ? "无 PDF 预览" : "No PDF preview";
  const error = locale === "zh-CN" ? "无法渲染 PDF 预览" : "Unable to render PDF preview";

  useEffect(() => {
    if (url === undefined) return;
    const observer = new ResizeObserver(([entry]) => { setWidth(Math.floor(entry.contentRect.width)); });
    observer.observe(widthRef.current!);
    return () => observer.disconnect();
  }, [url]);

  useEffect(() => {
    if (url === undefined) return;
    const task = getDocument({ url });
    task.onPassword = (updatePassword: (password: string) => void, reason: number) => {
      if (reason === PasswordResponses.NEED_PASSWORD) updatePassword(window.prompt("Enter the password to open this PDF file.") as string);
      else if (reason === PasswordResponses.INCORRECT_PASSWORD) updatePassword(window.prompt("Invalid password. Please try again.") as string);
    };
    let active = true;
    task.promise.then((next) => {
      if (active) { setDocument(next); setPageNumber((current) => current > next.numPages ? next.numPages : current); }
    }, (reason: unknown) => {
      if (active) { console.warn(reason); setDocumentError(true); }
    });
    return () => { active = false; void task.destroy(); };
  }, [url]);

  const totalPages = document?.numPages ?? null;
  const showPager = totalPages !== null && totalPages > 1;
  return <div className="group/pdf-preview relative min-w-0" data-diff-pdf-side={side}>
    <div className="flex min-h-40 min-w-0 flex-col">
      <div className="flex flex-1 items-center justify-center overflow-auto p-3 text-sm">
        {loading ? <span className="text-text-secondary">{locale === "zh-CN" ? "正在加载预览…" : "Loading preview…"}</span>
          : url === undefined ? <span className="text-text-secondary">{empty}</span>
          : <div ref={widthRef} className="w-full max-w-full">
            {documentError ? <span className="block text-center text-text-secondary">{error}</span>
              : document !== null ? <PdfPreviewBoundary fallback={<span className="block text-center text-text-secondary">{error}</span>}>
                <PdfPage document={document} pageNumber={pageNumber} width={width} />
              </PdfPreviewBoundary> : null}
          </div>}
      </div>
    </div>
    {showPager && <div className="pointer-events-none absolute right-0.5 top-0.5 z-20 flex items-center gap-0.5 rounded-full px-0.5 py-0.5 opacity-0 shadow-sm ring-1 ring-border-soft transition-opacity group-hover/pdf-preview:pointer-events-auto group-hover/pdf-preview:opacity-100 group-focus-within/pdf-preview:pointer-events-auto group-focus-within/pdf-preview:opacity-100"
      style={{ backgroundColor: "color-mix(in srgb, var(--surface-soft) 90%, transparent)" }}>
      <Tip content={t("上一页")}><button type="button" className="inline-flex h-6 w-6 items-center justify-center rounded-full text-text-secondary hover:bg-surface-hover disabled:opacity-40"
        aria-label={t("上一页")} disabled={pageNumber <= 1} onClick={() => setPageNumber((current) => current > 1 ? current - 1 : current)}>
        <ChevronLeft size={12} />
      </button></Tip>
      <span className="px-0.5 text-center text-sm tabular-nums text-text-secondary">{pageNumber}/{totalPages}</span>
      <Tip content={t("下一页")}><button type="button" className="inline-flex h-6 w-6 items-center justify-center rounded-full text-text-secondary hover:bg-surface-hover disabled:opacity-40"
        aria-label={t("下一页")} disabled={totalPages === null || pageNumber >= totalPages}
        onClick={() => setPageNumber((current) => totalPages !== null && current < totalPages ? current + 1 : current)}>
        <ChevronRight size={12} />
      </button></Tip>
    </div>}
  </div>;
}

/** The caller owns versioned resource loading; unavailable sides stay unavailable. */
export function DiffPdfPreview({ oldUrl, newUrl, oldLoading = false, newLoading = false, status = "changed" }: DiffPdfPreviewProps) {
  const afterOnly = status === "new";
  return <div className="relative overflow-auto">
    <div className={`grid gap-px ${afterOnly ? "grid-cols-1" : "grid-cols-2"}`}>
      {!afterOnly && <PdfSide key={`before:${oldUrl ?? "none"}`} url={oldUrl} loading={oldLoading} side="before" />}
      <PdfSide key={`after:${newUrl ?? "none"}`} url={newUrl} loading={newLoading} side="after" />
    </div>
  </div>;
}
