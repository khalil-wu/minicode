// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const pdf = vi.hoisted(() => ({
  destroy: vi.fn(),
  setViewer: vi.fn(),
  update: vi.fn(),
  resize: () => {},
  viewer: null as unknown as { currentPageNumber: number; currentScaleValue: string },
  getDocument: vi.fn(),
  signals: [] as AbortSignal[],
  documents: [] as unknown[],
  autoInit: true,
  ready: () => {},
}));

vi.mock("pdfjs-dist", () => ({
  GlobalWorkerOptions: {},
  getDocument: (...args: unknown[]) => pdf.getDocument(...args),
}));

vi.mock("pdfjs-dist/web/pdf_viewer.mjs", () => {
  class EventBus {
    handlers = new Map<string, (event: unknown) => void>();
    on(name: string, handler: (event: unknown) => void) { this.handlers.set(name, handler); }
    off(name: string) { this.handlers.delete(name); }
    dispatch(name: string, event = {}) { this.handlers.get(name)?.(event); }
  }
  class PDFViewer {
    currentPageNumber = 1;
    currentScaleValue = "page-width";
    pagesRotation = 0;
    eventBus: EventBus;
    constructor({ eventBus, abortSignal }: { eventBus: EventBus; abortSignal: AbortSignal }) {
      this.eventBus = eventBus; pdf.viewer = this; pdf.signals.push(abortSignal); pdf.ready = () => this.eventBus.dispatch("pagesinit");
    }
    setDocument(document: unknown) { pdf.documents.push(document); if (document && pdf.autoInit) this.eventBus.dispatch("pagesinit"); }
    increaseScale() { this.eventBus.dispatch("scalechanging", { scale: 1.25, presetValue: undefined }); }
    decreaseScale() { this.eventBus.dispatch("scalechanging", { scale: 0.75, presetValue: undefined }); }
    cleanup() {}
    update() { pdf.update(); }
  }
  return {
    EventBus, PDFViewer, LinkTarget: { BLANK: 2 },
    PDFLinkService: class { setViewer = pdf.setViewer; setDocument() {} },
  };
});

import { PdfAttachmentPreview } from "./PdfAttachmentPreview";

beforeEach(() => {
  pdf.destroy.mockClear(); pdf.setViewer.mockClear(); pdf.update.mockClear();
  pdf.signals.length = 0; pdf.documents.length = 0; pdf.autoInit = true;
  pdf.getDocument.mockReset().mockImplementation(() => ({ promise: Promise.resolve({ numPages: 20 }), destroy: pdf.destroy }));
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) { pdf.resize = callback; }
    observe() {} disconnect() {}
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("PDF viewer integration", () => {
  it("keeps navigation disabled while native document pages are still initializing", async () => {
    pdf.autoInit = false;
    render(<PdfAttachmentPreview url="http://localhost/report.pdf" name="Report" />);
    await screen.findByText("/ 20");
    expect((screen.getByRole("button", { name: "下一页" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("textbox", { name: "当前页" }) as HTMLInputElement).disabled).toBe(true);
    act(() => pdf.ready());
    expect((screen.getByRole("button", { name: "下一页" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("releases a failed native task and retries the same external document with a fresh request", async () => {
    pdf.getDocument.mockImplementationOnce(() => ({ promise: Promise.reject(new Error("PDF network failure")), destroy: pdf.destroy }));
    const view = render(<PdfAttachmentPreview url="http://localhost/report.pdf" name="Report" />);
    await screen.findByText("PDF network failure");
    fireEvent.click(screen.getByRole("button", { name: "重试 PDF 预览" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "放大" }) as HTMLButtonElement).disabled).toBe(false));
    expect(pdf.getDocument).toHaveBeenLastCalledWith(expect.objectContaining({ url: "http://localhost/report.pdf?preview_retry=1" }));
    expect(pdf.signals[0].aborted).toBe(true);
    expect(pdf.destroy).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(pdf.signals[1].aborted).toBe(true);
    expect(pdf.destroy).toHaveBeenCalledTimes(2);
  });

  it("lets the managed resource owner regenerate its token before retrying the PDF task", async () => {
    pdf.getDocument.mockImplementationOnce(() => ({ promise: Promise.reject(new Error("Expired token")), destroy: pdf.destroy }));
    const onRetry = vi.fn();
    const view = render(<PdfAttachmentPreview url="http://localhost/report.pdf?raw_token=expired" name="Report" onRetry={onRetry} />);
    await screen.findByText("Expired token");
    fireEvent.click(screen.getByRole("button", { name: "重试 PDF 预览" }));
    expect(onRetry).toHaveBeenCalledOnce();
    expect(pdf.getDocument).toHaveBeenCalledTimes(1);
    view.rerender(<PdfAttachmentPreview url="http://localhost/report.pdf?raw_token=fresh" name="Report" onRetry={onRetry} />);
    await waitFor(() => expect((screen.getByRole("button", { name: "放大" }) as HTMLButtonElement).disabled).toBe(false));
    expect(pdf.getDocument).toHaveBeenCalledTimes(2);
    expect(pdf.getDocument).toHaveBeenLastCalledWith(expect.objectContaining({ url: "http://localhost/report.pdf?raw_token=fresh" }));
  });

  it("ignores a late PDF load after the visible document and its native lifetime have changed", async () => {
    let finish!: (document: { numPages: number }) => void;
    pdf.getDocument.mockImplementationOnce(() => ({ promise: new Promise((resolve) => { finish = resolve; }), destroy: pdf.destroy }));
    const view = render(<PdfAttachmentPreview url="http://localhost/first.pdf" name="First" />);
    view.rerender(<PdfAttachmentPreview url="http://localhost/second.pdf" name="Second" />);
    await waitFor(() => expect((screen.getByRole("button", { name: "放大" }) as HTMLButtonElement).disabled).toBe(false));
    await act(async () => finish({ numPages: 99 }));
    expect(pdf.signals[0].aborted).toBe(true);
    expect(pdf.documents).not.toContainEqual({ numPages: 99 });
    expect(screen.getByText("/ 20")).toBeTruthy();
  });

  it("preserves scale while its workspace tab is hidden and resumes layout when shown", async () => {
    const { container } = render(<PdfAttachmentPreview url="http://localhost/report.pdf" name="Report" />);
    await waitFor(() => expect((screen.getByRole("button", { name: "放大" }) as HTMLButtonElement).disabled).toBe(false));
    pdf.viewer.currentScaleValue = "1.25";
    act(() => pdf.resize());
    expect(pdf.update).not.toHaveBeenCalled();
    expect(pdf.viewer.currentScaleValue).toBe("1.25");
    const viewport = container.querySelector(".mc-pdf-viewer-container")!;
    Object.defineProperty(viewport, "clientWidth", { value: 600 });
    Object.defineProperty(viewport, "clientHeight", { value: 800 });
    act(() => pdf.resize());
    expect(pdf.update).toHaveBeenCalledOnce();
    expect(pdf.viewer.currentScaleValue).toBe("1.25");
  });

  it("connects internal links and displays the scale from pdf.js zoom events", async () => {
    const view = render(<PdfAttachmentPreview url="http://localhost/report.pdf" name="Report" />);
    await waitFor(() => expect((screen.getByRole("button", { name: "放大" }) as HTMLButtonElement).disabled).toBe(false));
    expect(pdf.setViewer).toHaveBeenCalledWith(pdf.viewer);
    fireEvent.click(screen.getByRole("button", { name: "放大" }));
    expect(screen.getByText("125%")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "缩小" }));
    expect(screen.getByText("75%")).toBeTruthy();
    view.unmount();
    expect(pdf.destroy).toHaveBeenCalledTimes(1);
  });

  it("allows editing a page number before committing a bounded integer page", async () => {
    render(<PdfAttachmentPreview url="http://localhost/report.pdf" name="Report" />);
    const page = screen.getByRole("textbox", { name: "当前页" }) as HTMLInputElement;
    await waitFor(() => expect(page.disabled).toBe(false));
    fireEvent.change(page, { target: { value: "" } });
    expect(page.value).toBe("");
    fireEvent.change(page, { target: { value: "12.5" } });
    expect(pdf.viewer.currentPageNumber).toBe(1);
    fireEvent.keyDown(page, { key: "Enter" });
    expect(pdf.viewer.currentPageNumber).toBe(12);
    expect(page.value).toBe("12");
    fireEvent.change(page, { target: { value: "invalid" } });
    fireEvent.blur(page);
    expect(page.value).toBe("12");
    await act(async () => {});
  });
});
