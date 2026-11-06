/* @vitest-environment jsdom */
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useRef } from "react";
import { useReplyViewport } from "./useReplyViewport";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("keeps the reader still during width-only sidebar frames and follows an actual composer height change", () => {
  let resized: ResizeObserverCallback;
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: ResizeObserverCallback) { resized = callback; }
    observe() {} disconnect() {}
  });
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(600);
  const follow = vi.fn();
  function View() {
    const scroll = useRef<HTMLDivElement>(null);
    useReplyViewport(scroll, follow);
    return <div ref={scroll} style={{ padding: 0 }} />;
  }
  const view = render(<View />);
  follow.mockClear();
  for (const width of [900, 850, 800, 750]) act(() => resized([
    { contentRect: { width, height: 600 } } as ResizeObserverEntry,
  ], {} as ResizeObserver));
  expect(follow).not.toHaveBeenCalled();
  act(() => resized([{ contentRect: { width: 750, height: 450 } } as ResizeObserverEntry], {} as ResizeObserver));
  expect(follow).toHaveBeenCalledOnce();
  expect((view.container.firstChild as HTMLElement).style.getPropertyValue("--reply-viewport-height")).toBe("450px");
});
