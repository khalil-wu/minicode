// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const pdf = vi.hoisted(() => ({
  destroy: vi.fn(),
  setViewer: vi.fn(),
  update: vi.fn(),
  resize: () => {},
  viewer: null as unknown as { currentPageNumber: number; currentScaleValue: string },
}));

vi.mock("pdfjs-dist", () => ({
  GlobalWorkerOptions: {},
  getDocument: () => ({ promise: Promise.resolve({ numPages: 20 }), destroy: pdf.destroy }),
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
    constructor({ eventBus }: { eventBus: EventBus }) { this.eventBus = eventBus; pdf.viewer = this; }
    setDocument(document: unknown) { if (document) this.eventBus.dispatch("pagesinit"); }
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
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) { pdf.resize = callback; }
    observe() {} disconnect() {}
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("PDF viewer integration", () => {
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
