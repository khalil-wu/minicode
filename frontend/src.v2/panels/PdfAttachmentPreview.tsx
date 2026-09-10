import { ChevronLeft, ChevronRight, Minus, Plus, RotateCw, ScanLine, TriangleAlert } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import * as pdfjs from "pdfjs-dist";
import { EventBus, LinkTarget, PDFLinkService, PDFViewer } from "pdfjs-dist/web/pdf_viewer.mjs";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import "pdfjs-dist/web/pdf_viewer.css";
import "./PdfAttachmentPreview.css";

pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

type PdfStatus = "loading" | "ready" | "error";

export const PdfAttachmentPreview = ({ url, name }: { url: string; name: string }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<HTMLDivElement>(null);
  const pdfViewerRef = useRef<PDFViewer | null>(null);
  const [status, setStatus] = useState<PdfStatus>("loading");
  const [error, setError] = useState("");
  const [pageNumber, setPageNumber] = useState(1);
  const [pageInput, setPageInput] = useState("1");
  const [pageCount, setPageCount] = useState(0);
  const [scale, setScale] = useState("page-width");

  useEffect(() => {
    const container = containerRef.current;
    const viewerElement = viewerRef.current;
    if (!container || !viewerElement || !url) return undefined;

    setStatus("loading");
    setError("");
    setPageNumber(1);
    setPageInput("1");
    setPageCount(0);
    setScale("page-width");

    let disposed = false;
    let loadingTask: ReturnType<typeof pdfjs.getDocument> | undefined;
    const viewerLifetime = new AbortController();
    const eventBus = new EventBus();
    const linkService = new PDFLinkService({
      eventBus,
      externalLinkTarget: LinkTarget.BLANK,
      externalLinkRel: "noopener noreferrer",
    });
    // pdf.js 4.10 accepts abortSignal at runtime; its options declaration
    // omits it. Keep the runtime lifecycle option in the concrete object.
    const viewerOptions = {
      container,
      viewer: viewerElement,
      eventBus,
      linkService,
      removePageBorders: false,
      textLayerMode: 1,
      annotationMode: 1,
      maxCanvasPixels: 4096 * 4096,
      abortSignal: viewerLifetime.signal,
    };
    const pdfViewer = new PDFViewer(viewerOptions);
    linkService.setViewer(pdfViewer);
    pdfViewerRef.current = pdfViewer;
    // The sidebar opens and resizes independently of the browser window.
    // pdf.js tracks its height, but the host owns reapplying fit presets.
    const resizeObserver = new ResizeObserver(() => {
      // Hidden workspace tabs have no layout. Preserve their scale and page
      // until the host becomes visible instead of scrolling a detached layout.
      if (!container.clientWidth || !container.clientHeight) return;
      const preset = pdfViewer.currentScaleValue || "page-width";
      if (["page-width", "page-fit", "page-height", "auto"].includes(preset)) {
        pdfViewer.currentScaleValue = preset;
      }
      pdfViewer.update();
    });
    resizeObserver.observe(container);

    const handlePagesInit = () => {
      if (disposed) return;
      if (container.clientWidth && container.clientHeight) pdfViewer.currentScaleValue = "page-width";
      setScale("page-width");
      setStatus("ready");
    };
    const handlePageChanging = (event: { pageNumber?: number }) => {
      if (!disposed && typeof event.pageNumber === "number") {
        setPageNumber(event.pageNumber);
        setPageInput(String(event.pageNumber));
      }
    };
    const handleScaleChanging = (event: { scale: number; presetValue?: string }) => {
      if (!disposed) setScale(event.presetValue === "page-width" ? "page-width" : String(event.scale));
    };
    eventBus.on("pagesinit", handlePagesInit);
    eventBus.on("pagechanging", handlePageChanging);
    eventBus.on("scalechanging", handleScaleChanging);

    loadingTask = pdfjs.getDocument({ url, rangeChunkSize: 64 * 1024, disableAutoFetch: false });
    loadingTask.promise
      .then((loadedDocument) => {
        if (disposed) return;
        setPageCount(loadedDocument.numPages);
        linkService.setDocument(loadedDocument, null);
        pdfViewer.setDocument(loadedDocument);
      })
      .catch((reason: unknown) => {
        if (disposed) return;
        setStatus("error");
        setError(reason instanceof Error ? reason.message : "无法加载 PDF 文件。");
      });

    return () => {
      disposed = true;
      resizeObserver.disconnect();
      viewerLifetime.abort();
      eventBus.off("pagesinit", handlePagesInit);
      eventBus.off("pagechanging", handlePageChanging);
      eventBus.off("scalechanging", handleScaleChanging);
      pdfViewer.cleanup();
      pdfViewer.setDocument(undefined as never);
      linkService.setDocument(null);
      pdfViewerRef.current = null;
      if (loadingTask) void loadingTask.destroy();
    };
  }, [url]);

  const setViewerScale = (next: string) => {
    const viewer = pdfViewerRef.current;
    if (!viewer) return;
    viewer.currentScaleValue = next;
    setScale(next);
  };

  const goToPage = (next: number) => {
    const viewer = pdfViewerRef.current;
    if (!viewer || pageCount === 0) return;
    const bounded = Math.min(pageCount, Math.max(1, Math.trunc(next)));
    viewer.currentPageNumber = bounded;
    setPageNumber(bounded);
    setPageInput(String(bounded));
  };

  const commitPageInput = () => {
    const next = Number(pageInput);
    if (pageInput.trim() && Number.isFinite(next)) goToPage(next);
    else setPageInput(String(pageNumber));
  };

  return (
    <div className="mc-pdf-preview" aria-label={`PDF 预览 ${name}`}>
      <div className="mc-pdf-toolbar">
        <button type="button" className="mc-pdf-icon-button" title="上一页" aria-label="上一页" onClick={() => goToPage(pageNumber - 1)} disabled={pageNumber <= 1}>
          <ChevronLeft size={15} />
        </button>
        <label className="mc-pdf-page-control">
          <input aria-label="当前页" value={pageInput} onChange={(event) => setPageInput(event.target.value)} onBlur={commitPageInput}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                event.preventDefault();
                commitPageInput();
              }
            }} inputMode="numeric" disabled={status !== "ready"} />
          <span>/ {pageCount || "--"}</span>
        </label>
        <button type="button" className="mc-pdf-icon-button" title="下一页" aria-label="下一页" onClick={() => goToPage(pageNumber + 1)} disabled={pageCount === 0 || pageNumber >= pageCount}>
          <ChevronRight size={15} />
        </button>
        <span className="mc-pdf-toolbar-spacer" />
        <button type="button" className="mc-pdf-icon-button" title="缩小" aria-label="缩小" onClick={() => pdfViewerRef.current?.decreaseScale()} disabled={status !== "ready"}>
          <Minus size={14} />
        </button>
        <button type="button" className="mc-pdf-scale" title="适合宽度" aria-label="适合宽度" onClick={() => setViewerScale("page-width")} disabled={status !== "ready"}>
          <ScanLine size={14} />
          <span>{scale === "page-width" ? "适合宽度" : `${Math.round(Number(scale) * 100)}%`}</span>
        </button>
        <button type="button" className="mc-pdf-icon-button" title="放大" aria-label="放大" onClick={() => pdfViewerRef.current?.increaseScale()} disabled={status !== "ready"}>
          <Plus size={14} />
        </button>
        <button type="button" className="mc-pdf-icon-button" title="旋转" aria-label="旋转" onClick={() => {
          const viewer = pdfViewerRef.current;
          if (viewer) viewer.pagesRotation = (viewer.pagesRotation + 90) % 360;
        }} disabled={status !== "ready"}>
          <RotateCw size={14} />
        </button>
      </div>
      {status === "error" && (
        <div className="mc-pdf-state mc-pdf-error" role="alert">
          <TriangleAlert size={18} />
          <span>{error || "无法加载 PDF 文件。"}</span>
        </div>
      )}
      {status === "loading" && <div className="mc-pdf-state">正在加载 PDF 预览...</div>}
      <div className="mc-pdf-viewer-viewport">
        <div ref={containerRef} className="mc-pdf-viewer-container" data-pdf-status={status}>
          <div ref={viewerRef} className="pdfViewer" />
        </div>
      </div>
    </div>
  );
};
