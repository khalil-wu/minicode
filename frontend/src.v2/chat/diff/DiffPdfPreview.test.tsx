// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const pdf = vi.hoisted(() => ({
  getDocument: vi.fn(),
  observers: [] as Array<{ callback: (entries: Array<{ contentRect: { width: number } }>) => void; element?: Element; disconnect: ReturnType<typeof vi.fn> }>,
}));
vi.mock("pdfjs-dist", () => ({
  GlobalWorkerOptions: {}, AnnotationMode: { ENABLE: 1 }, PasswordResponses: { NEED_PASSWORD: 1, INCORRECT_PASSWORD: 2 },
  getDocument: (...args: unknown[]) => pdf.getDocument(...args),
}));

import { DiffPdfPreview } from "./DiffPdfPreview";

function makeDocument(numPages: number) {
  const pages = new Map<number, ReturnType<typeof makePage>>();
  const getPage = vi.fn((number: number) => {
    const page = pages.get(number) ?? makePage();
    pages.set(number, page);
    return Promise.resolve(page);
  });
  return { numPages, getPage, pages };
}
function makePage() {
  return { cleanup: vi.fn(), getViewport: vi.fn(({ scale }: { scale: number }) => ({ width: 200 * scale, height: 300 * scale })),
    render: vi.fn(() => ({ promise: Promise.resolve(), cancel: vi.fn() })) };
}
function load(document: ReturnType<typeof makeDocument>) {
  const task = { promise: Promise.resolve(document), destroy: vi.fn() };
  pdf.getDocument.mockReturnValueOnce(task);
  return task;
}

beforeEach(() => {
  pdf.getDocument.mockReset(); pdf.observers.length = 0;
  vi.spyOn(console, "warn").mockImplementation(() => {});
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({} as CanvasRenderingContext2D);
  vi.stubGlobal("ResizeObserver", class {
    record: typeof pdf.observers[number];
    constructor(callback: typeof pdf.observers[number]["callback"]) {
      this.record = { callback, disconnect: vi.fn() }; pdf.observers.push(this.record);
    }
    observe(element: Element) { this.record.element = element; }
    disconnect() { this.record.disconnect(); }
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("Source PDF diff preview", () => {
  it("keeps pagination independent on each side and bounds real page navigation", async () => {
    const before = makeDocument(3), after = makeDocument(2);
    load(before); load(after);
    const { container } = render(<DiffPdfPreview oldUrl="blob:old" newUrl="blob:new" />);
    const oldSide = container.querySelector('[data-diff-pdf-side="before"]') as HTMLElement;
    const newSide = container.querySelector('[data-diff-pdf-side="after"]') as HTMLElement;
    await within(oldSide).findByText("1/3"); await within(newSide).findByText("1/2");
    expect((within(oldSide).getByRole("button", { name: "上一页" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(oldSide).getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(before.getPage).toHaveBeenLastCalledWith(2));
    expect(within(newSide).getByText("1/2")).toBeTruthy();
    expect(after.getPage).toHaveBeenCalledTimes(1);
    fireEvent.click(within(newSide).getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(after.getPage).toHaveBeenLastCalledWith(2));
    expect((within(newSide).getByRole("button", { name: "下一页" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(oldSide).getByRole("button", { name: "上一页" }));
    await waitFor(() => expect(before.getPage).toHaveBeenLastCalledWith(1));
    const pager = within(oldSide).getByText("1/3").parentElement!;
    expect(pager.className).toContain("opacity-0");
    expect(pager.className).toContain("group-hover/pdf-preview:opacity-100");
    expect(pager.className).toContain("group-focus-within/pdf-preview:opacity-100");
    expect(screen.queryByRole("button", { name: "放大" })).toBeNull();
    expect(screen.queryByRole("button", { name: "旋转" })).toBeNull();
  });

  it("shows after only for new files, retains missing sides otherwise, and omits a single-page pager", async () => {
    load(makeDocument(1));
    const view = render(<DiffPdfPreview newUrl="blob:added" status="new" />);
    await waitFor(() => expect(view.container.querySelector("canvas")?.style.visibility).toBe(""));
    expect(view.container.querySelectorAll("[data-diff-pdf-side]")).toHaveLength(1);
    expect(view.container.querySelector(".grid-cols-1")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "下一页" })).toBeNull();
    load(makeDocument(1));
    view.rerender(<DiffPdfPreview oldUrl="blob:deleted" status="deleted" />);
    await screen.findByText("无 PDF 预览");
    expect(view.container.querySelectorAll("[data-diff-pdf-side]")).toHaveLength(2);
    expect(view.container.querySelector(".grid-cols-2")).toBeTruthy();
    view.rerender(<DiffPdfPreview status="changed" />);
    expect(screen.getAllByText("无 PDF 预览")).toHaveLength(2);
  });

  it("fits each canvas to its observed width without reloading the document or creating interaction layers", async () => {
    const document = makeDocument(2); load(document);
    const view = render(<DiffPdfPreview newUrl="blob:width" status="new" />);
    await waitFor(() => expect(document.pages.get(1)?.render).toHaveBeenCalled());
    const page = document.pages.get(1)!;
    const previousRender = page.render.mock.results[0].value;
    act(() => pdf.observers[0].callback([{ contentRect: { width: 321.8 } }]));
    await waitFor(() => expect(view.container.querySelector("canvas")?.style.width).toBe("321px"));
    expect(view.container.querySelector("canvas")?.height).toBe(Math.floor(481.5 * window.devicePixelRatio));
    expect(page.render).toHaveBeenLastCalledWith(expect.objectContaining({ annotationMode: 1 }));
    expect(previousRender.cancel).toHaveBeenCalledOnce();
    expect(pdf.getDocument).toHaveBeenCalledTimes(1);
    expect(document.getPage).toHaveBeenCalledTimes(1);
    expect(view.container.querySelector(".textLayer, .annotationLayer, a, input")).toBeNull();
    view.unmount();
    expect(pdf.observers[0].disconnect).toHaveBeenCalledOnce();
  });

  it("keeps document parsing blank, distinguishes page loading, and projects the actual failure stage", async () => {
    let finishDocument!: (value: ReturnType<typeof makeDocument>) => void;
    pdf.getDocument.mockReturnValueOnce({ promise: new Promise((resolve) => { finishDocument = resolve; }), destroy: vi.fn() });
    const view = render(<DiffPdfPreview newUrl="blob:pending" status="new" />);
    expect(view.container.textContent).toBe("");
    const document = makeDocument(1);
    let rejectPage!: (reason: Error) => void;
    document.getPage.mockImplementationOnce(() => new Promise((_, reject) => { rejectPage = reject; }));
    await act(async () => finishDocument(document));
    expect(screen.getByText("Loading page…")).toBeTruthy();
    await act(async () => rejectPage(new Error("Page object unavailable")));
    expect(screen.getByText("Failed to load the page.")).toBeTruthy();
    pdf.getDocument.mockReturnValueOnce({ promise: Promise.reject(new Error("Invalid PDF bytes")), destroy: vi.fn() });
    view.rerender(<DiffPdfPreview newUrl="blob:invalid" status="new" />);
    await screen.findByText("无法渲染 PDF 预览");
    expect(screen.queryByText("Invalid PDF bytes")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(console.warn).toHaveBeenCalledWith(expect.objectContaining({ message: "Invalid PDF bytes" }));
  });

  it("releases old tasks and prevents a late old document from replacing the selected source", async () => {
    let finishOld!: (value: ReturnType<typeof makeDocument>) => void;
    const oldTask = { promise: new Promise((resolve) => { finishOld = resolve; }), destroy: vi.fn() };
    pdf.getDocument.mockReturnValueOnce(oldTask);
    const view = render(<DiffPdfPreview newUrl="blob:first" status="new" />);
    const current = makeDocument(2), currentTask = load(current);
    view.rerender(<DiffPdfPreview newUrl="blob:second" status="new" />);
    await screen.findByText("1/2");
    const stale = makeDocument(9);
    await act(async () => finishOld(stale));
    expect(stale.getPage).not.toHaveBeenCalled();
    expect(oldTask.destroy).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByText("2/2");
    const next = makeDocument(3); load(next);
    view.rerender(<DiffPdfPreview newUrl="blob:third" status="new" />);
    await screen.findByText("1/3");
    expect(currentTask.destroy).toHaveBeenCalledOnce();
  });

  it("retains Source's native password request behavior for encrypted documents", () => {
    const task = { promise: new Promise(() => {}), destroy: vi.fn(),
      onPassword: undefined as undefined | ((update: (password: string) => void, reason: number) => void) };
    pdf.getDocument.mockReturnValueOnce(task);
    const prompt = vi.spyOn(window, "prompt").mockReturnValueOnce("secret").mockReturnValueOnce(null);
    render(<DiffPdfPreview newUrl="blob:encrypted" status="new" />);
    const update = vi.fn();
    task.onPassword!(update, 1);
    expect(prompt).toHaveBeenLastCalledWith("Enter the password to open this PDF file.");
    expect(update).toHaveBeenLastCalledWith("secret");
    task.onPassword!(update, 2);
    expect(prompt).toHaveBeenLastCalledWith("Invalid password. Please try again.");
    expect(update).toHaveBeenLastCalledWith(null);
  });
});
