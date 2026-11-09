/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SidebarSortableList } from "./SidebarSortableList";

const identifiers = Array.from({ length: 120 }, (_, index) => `thread-${index}`);
const reorder = vi.fn(async () => true);
const navigate = vi.fn();
function Harness({ retainedIds = [], renamingId = null }: { retainedIds?: string[]; renamingId?: string | null }) {
  return <div data-sidebar-scroll data-testid="viewport" style={{ height: 300, overflow: "auto" }}>
    <div style={{ height: 120 }}>Project header</div>
    <SidebarSortableList identifiers={identifiers} scope="recent" ariaLabel="Threads" virtualize
      retainedIds={retainedIds} renamingId={renamingId} onDragStart={() => {}} onReorder={reorder}
      renderOverlay={id => id}
      renderRow={(id, binding) => <button ref={binding.dragActivatorRef} onClick={() => navigate(id)}>{id}</button>} />
  </div>;
}

beforeEach(() => {
  reorder.mockClear(); navigate.mockClear();
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function () {
    return this.dataset.testid === "viewport" ? 300 : this.dataset.index !== undefined ? 30 : 0;
  });
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(280);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function () {
    const viewport = document.querySelector<HTMLElement>('[data-testid="viewport"]');
    const top = this.dataset.sidebarSortList ? 120 - (viewport?.scrollTop ?? 0) : 0;
    return new DOMRect(0, top, 280, this.offsetHeight);
  });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("renders the viewport, keeps the full scroll extent, and navigates to later rows", () => {
  render(<Harness />);
  const viewport = screen.getByTestId("viewport");
  const list = screen.getByRole("list", { name: "Threads" });
  expect(list.style.height).toBe("3600px");
  expect(screen.getAllByRole("listitem").length).toBeLessThan(25);
  expect(screen.queryByRole("button", { name: "thread-80", exact: true })).toBeNull();
  fireEvent.scroll(viewport, { target: { scrollTop: 2400 } });
  fireEvent.click(screen.getByRole("button", { name: "thread-80", exact: true }));
  expect(navigate).toHaveBeenCalledExactlyOnceWith("thread-80");
  const row = screen.getByRole("button", { name: "thread-80", exact: true }).closest('[role="listitem"]')!;
  expect(row.getAttribute("aria-posinset")).toBe("81");
  expect(row.getAttribute("aria-setsize")).toBe("120");
  expect(screen.getAllByRole("listitem").length).toBeLessThan(25);
});

it("retains menu, rename and focused rows while scrolling and reorders using the complete list", async () => {
  render(<Harness retainedIds={["thread-4"]} renamingId="thread-5" />);
  const focused = screen.getByRole("button", { name: "thread-2", exact: true });
  act(() => focused.focus());
  fireEvent.scroll(screen.getByTestId("viewport"), { target: { scrollTop: 2400 } });
  expect(document.activeElement).toBe(focused);
  for (const index of [1, 3, 4, 5]) expect(screen.getByRole("button", { name: `thread-${index}`, exact: true })).toBeTruthy();
  const later = screen.getByRole("button", { name: "thread-80", exact: true });
  act(() => later.focus());
  await act(async () => fireEvent.keyDown(later, { key: "ArrowUp", altKey: true }));
  expect(reorder).toHaveBeenCalledExactlyOnceWith("thread-80", "thread-79", identifiers);
  expect(document.activeElement).toBe(later);
  expect(screen.queryByRole("button", { name: "thread-2", exact: true })).toBeNull();
});
