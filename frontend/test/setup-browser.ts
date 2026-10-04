import { beforeEach, vi } from "vitest";

// JSDOM has no layout observer. Geometry/lifetime specs install their own
// callbacks; other UI specs still need the browser API at shared primitives.
Element.prototype.scrollIntoView = vi.fn();
beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});
