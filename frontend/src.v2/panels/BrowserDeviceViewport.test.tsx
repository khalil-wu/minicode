// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BrowserDeviceResizeHandles, browserDeviceLayout, resizeBrowserDeviceViewport, type BrowserDeviceViewport } from "./BrowserPageActions";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const viewport: BrowserDeviceViewport = { width: 400, height: 600, mobile: false, presetId: "responsive" };
const stage = { x: 100, y: 80, width: 1040, height: 1020 };
const drag = { startWidth: 400, startHeight: 600, startPointerX: 100, startPointerY: 200 };

describe("installed Codex device viewport geometry", () => {
  it("fits the actual page between the 20px side and bottom grips without enlarging it", () => {
    expect(browserDeviceLayout(stage, viewport).visualBounds).toEqual({ x: 420, y: 80, width: 400, height: 600 });
    const fitted = browserDeviceLayout({ x: 700, y: 150, width: 600, height: 700 }, { ...viewport, width: 390, height: 844 });
    expect(fitted.fitWidth).toBe(560);
    expect(fitted.fitHeight).toBe(680);
    expect(fitted.scale).toBe(680 / 844);
    expect(fitted.visualBounds).toEqual({ x: 843, y: 150, width: 314, height: 680 });
    expect(browserDeviceLayout({ x: 100, y: 80, width: 20, height: 10 }, viewport).visualBounds).toEqual({ x: 120, y: 80, width: 0, height: 0 });
  });

  it.each([
    ["left", 80, 230, 440, 600], ["right", 120, 230, 440, 600], ["bottom", 120, 230, 400, 630],
    ["bottom-left", 80, 230, 440, 630], ["bottom-right", 120, 230, 440, 630],
  ] as const)("drags the %s grip about the horizontal centre", (edge, x, y, width, height) => {
    expect(resizeBrowserDeviceViewport({ ...drag, edge }, browserDeviceLayout(stage, viewport), x, y)).toEqual({ width, height });
  });

  it("uses the current fitted scale and limits expansion to the available stage", () => {
    const fitted = browserDeviceLayout({ x: 0, y: 0, width: 540, height: 420 }, { ...viewport, width: 1000, height: 800 });
    expect(fitted.scale).toBe(0.5);
    expect(resizeBrowserDeviceViewport({ ...drag, edge: "bottom-right" }, fitted, 150, 250)).toEqual({ width: 600, height: 700 });
    expect(resizeBrowserDeviceViewport({ ...drag, edge: "bottom-right" }, fitted, 500, 600)).toEqual({ width: 1000, height: 800 });
  });

  it("keeps the source dimension limits when a grip crosses its opposite edge or expands far out", () => {
    const large = browserDeviceLayout({ ...stage, width: 10000, height: 10000 }, viewport);
    expect(resizeBrowserDeviceViewport({ ...drag, edge: "bottom-right" }, large, -1000, -1000)).toEqual({ width: 240, height: 160 });
    expect(resizeBrowserDeviceViewport({ ...drag, edge: "bottom-right" }, large, 5000, 5000)).toEqual({ width: 4096, height: 4096 });
  });
});

describe("device grip pointer lifecycle", () => {
  const raf = () => {
    vi.stubGlobal("PointerEvent", MouseEvent);
    let id = 0;
    const callbacks = new Map<number, FrameRequestCallback>();
    const request = vi.spyOn(window, "requestAnimationFrame").mockImplementation(callback => { callbacks.set(++id, callback); return id; });
    const cancel = vi.spyOn(window, "cancelAnimationFrame").mockImplementation(frame => { callbacks.delete(frame); });
    return { request, cancel, callbacks, flush: () => act(() => { const queued = [...callbacks.values()]; callbacks.clear(); queued.forEach(callback => callback(1)); }) };
  };

  it("captures a grip, coalesces moves, and flushes the last dimensions before releasing the native page", () => {
    const frames = raf(), change = vi.fn(), active = vi.fn();
    const props = { viewport, layout: browserDeviceLayout(stage, viewport), onChange: change, onResizeActiveChange: active };
    const view = render(<BrowserDeviceResizeHandles {...props} />);
    const right = screen.getByRole("button", { name: "从右边缘调整设备视口" });
    right.setPointerCapture = vi.fn();
    expect(screen.getAllByRole("button")).toHaveLength(5);
    expect(right.tabIndex).toBe(-1);
    fireEvent.pointerDown(right, { clientX: 100, clientY: 200 });
    expect(right.setPointerCapture).toHaveBeenCalledOnce();
    expect(active).toHaveBeenLastCalledWith(true);
    fireEvent.pointerMove(right, { clientX: 110, clientY: 200 });
    fireEvent.pointerMove(right, { clientX: 120, clientY: 200 });
    expect(frames.request).toHaveBeenCalledTimes(1);
    expect(change).not.toHaveBeenCalled();
    frames.flush();
    expect(change).toHaveBeenLastCalledWith({ ...viewport, width: 440 });
    view.rerender(<BrowserDeviceResizeHandles {...props} layout={browserDeviceLayout({ ...stage, width: 540, height: 420 }, { ...viewport, width: 1000, height: 800 })} />);
    fireEvent.pointerMove(right, { clientX: 130, clientY: 200 });
    fireEvent.pointerUp(right);
    expect(change).toHaveBeenLastCalledWith({ ...viewport, width: 520 });
    expect(active.mock.calls).toEqual([[true], [false]]);
    expect(frames.callbacks.size).toBe(0);
    fireEvent.lostPointerCapture(right);
    expect(active).toHaveBeenCalledTimes(2);
  });

  it.each(["pointerCancel", "lostPointerCapture"] as const)("flushes pending work on %s and cancels it when unmounted", (event) => {
    const frames = raf(), change = vi.fn(), active = vi.fn();
    const view = render(<BrowserDeviceResizeHandles viewport={viewport} layout={browserDeviceLayout(stage, viewport)} onChange={change} onResizeActiveChange={active} />);
    const bottom = screen.getByRole("button", { name: "从下边缘调整设备视口" });
    bottom.setPointerCapture = vi.fn();
    fireEvent.pointerDown(bottom, { clientX: 100, clientY: 200 });
    fireEvent.pointerMove(bottom, { clientX: 100, clientY: 230 });
    fireEvent[event](bottom);
    expect(change).toHaveBeenLastCalledWith({ ...viewport, height: 630 });
    expect(active).toHaveBeenLastCalledWith(false);
    fireEvent.pointerDown(bottom, { clientX: 100, clientY: 200 });
    fireEvent.pointerMove(bottom, { clientX: 100, clientY: 250 });
    view.unmount();
    expect(frames.callbacks.size).toBe(0);
    expect(change).toHaveBeenCalledTimes(1);
    expect(active.mock.calls).toEqual([[true], [false], [true], [false]]);
  });
});
