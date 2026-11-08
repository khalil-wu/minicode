/* @vitest-environment jsdom */
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useMeasuredMarkdownBlock } from "./useMeasuredMarkdownBlock";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("publishes the latest visible geometry once per frame and cancels work on unmount", () => {
  let resized: ResizeObserverCallback;
  const disconnect = vi.fn();
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: ResizeObserverCallback) { resized = callback; }
    observe() {}
    disconnect = disconnect;
  });
  const frames = new Map<number, FrameRequestCallback>();
  let serial = 0;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.set(++serial, callback); return serial; });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  const Block = () => <div ref={useMeasuredMarkdownBlock<HTMLDivElement>()}>Retained source</div>;
  const view = render(<Block />);
  const element = view.container.firstElementChild as HTMLElement;
  const resize = (height: number) => act(() => resized([{ target: element, contentRect: { height } } as ResizeObserverEntry], {} as ResizeObserver));
  const paint = () => act(() => { const work = [...frames.values()]; frames.clear(); work.forEach(callback => callback(0)); });
  resize(120);
  resize(140);
  expect(frames.size).toBe(1);
  expect(element.dataset.measured).toBeUndefined();
  expect(element.textContent).toBe("Retained source");
  paint();
  expect(element.style.getPropertyValue("--markdown-block-height")).toBe("140px");
  expect(element.dataset.measured).toBe("true");
  resize(0);
  resize(140);
  expect(frames.size).toBe(0);
  resize(180);
  resize(0);
  paint();
  expect(element.style.getPropertyValue("--markdown-block-height")).toBe("180px");
  resize(240);
  view.unmount();
  expect(disconnect).toHaveBeenCalledTimes(1);
  expect(frames.size).toBe(0);
});
