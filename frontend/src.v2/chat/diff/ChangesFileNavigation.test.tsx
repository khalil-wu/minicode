// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MotionValue } from "motion/react";
import { ChangesFileJump, ChangedFilesTree, buildChangedFilesTree, type ChangesFileEntry } from "./ChangesFileNavigation";
import { rankChangedFilePaths } from "./changes-file-matcher";
import { useAppStore } from "../../stores";

const movement = vi.hoisted(() => ({ animate: vi.fn() }));
vi.mock("motion/react", async original => ({ ...await original<typeof import("motion/react")>(),
  animate: movement.animate }));

const entries: ChangesFileEntry[] = [
  { path: "src/chat/ChatPane.tsx", additions: 12, deletions: 3, commentCount: 2 },
  { path: "src/chat/diff/CodeDiff.tsx", additions: 1, deletions: 0 },
  { path: "tests/ChatPane.test.tsx", additions: null, deletions: null },
  { path: "README.md", additions: 2, deletions: 1 },
];
const previousReducedMotion = useAppStore.getState().reducedMotion;
let anchor: HTMLButtonElement;

beforeEach(() => {
  anchor = document.createElement("button");
  anchor.textContent = "Jump trigger";
  document.body.append(anchor);
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: true, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
  vi.stubGlobal("PointerEvent", MouseEvent);
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  movement.animate.mockReset().mockReturnValue({ stop: vi.fn() });
  useAppStore.setState({ reducedMotion: true });
});
afterEach(() => {
  cleanup(); anchor.remove();
  useAppStore.setState({ reducedMotion: previousReducedMotion });
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

const selectedJumpPath = () => screen.getAllByRole("option").find(option => option.getAttribute("aria-selected") === "true")?.getAttribute("aria-label");

describe("ChangesFileJump", () => {
  it("portals the current scope only and uses the Source matcher for filename/parent ranking", () => {
    const choose = vi.fn(), close = vi.fn();
    const { container, rerender } = render(<ChangesFileJump anchor={anchor} entries={entries}
      activePath="not-in-current-scope.ts" onSelect={choose} onClose={close} />);
    const dialog = screen.getByRole("dialog");
    expect(dialog.parentElement).toBe(document.body);
    expect(container.contains(dialog)).toBe(false);
    expect(screen.getAllByRole("option").map(option => option.getAttribute("aria-label")))
      .toEqual(rankChangedFilePaths(entries.map(entry => entry.path), ""));
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "CP" } });
    expect(screen.getAllByRole("option").map(option => option.getAttribute("aria-label")))
      .toEqual(rankChangedFilePaths(entries.map(entry => entry.path), "CP"));
    expect(dialog.querySelector(".mc-changes-file-counts")).toBeNull();
    rerender(<ChangesFileJump anchor={anchor} entries={[{ path: "other-scope/CP.ts" }]} onSelect={choose} onClose={close} />);
    expect(screen.queryByRole("option", { name: "src/chat/ChatPane.tsx" })).toBeNull();
    fireEvent.click(screen.getByRole("option", { name: "other-scope/CP.ts" }));
    expect(choose).toHaveBeenCalledExactlyOnceWith("other-scope/CP.ts");
    expect(close).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(anchor);
  });

  it("keeps search focus, does not wrap arrows, and supports Source command shortcuts", () => {
    const choose = vi.fn();
    render(<ChangesFileJump anchor={anchor} entries={entries} onSelect={choose} onClose={vi.fn()} />);
    const input = screen.getByRole("combobox");
    const ordered = rankChangedFilePaths(entries.map(entry => entry.path), "");
    expect(document.activeElement).toBe(input);
    expect(selectedJumpPath()).toBe(ordered[0]);
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(selectedJumpPath()).toBe(ordered[0]);
    fireEvent.keyDown(input, { key: "j", ctrlKey: true });
    expect(selectedJumpPath()).toBe(ordered[1]);
    fireEvent.keyDown(input, { key: "p", ctrlKey: true });
    expect(selectedJumpPath()).toBe(ordered[0]);
    fireEvent.keyDown(input, { key: "ArrowDown", metaKey: true });
    expect(selectedJumpPath()).toBe(ordered.at(-1));
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(selectedJumpPath()).toBe(ordered.at(-1));
    fireEvent.keyDown(input, { key: "Home" });
    expect(selectedJumpPath()).toBe(ordered[0]);
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    expect(choose).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(choose).toHaveBeenCalledExactlyOnceWith(ordered[0]);
    expect(input.getAttribute("aria-activedescendant")).toBe(screen.getByRole("option", { name: ordered[0] }).id);
  });

  it("closes Escape without selecting and resets query when reopened", () => {
    const choose = vi.fn(), close = vi.fn();
    const first = render(<ChangesFileJump anchor={anchor} entries={entries} onSelect={choose} onClose={close} />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "no-such-file" } });
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "Escape" });
    expect(choose).not.toHaveBeenCalled(); expect(close).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(anchor);
    first.unmount();
    render(<ChangesFileJump anchor={anchor} entries={entries} onSelect={choose} onClose={close} />);
    expect((screen.getByRole("combobox") as HTMLInputElement).value).toBe("");
    expect(screen.getAllByRole("option")).toHaveLength(entries.length);
  });
});

describe("ChangedFilesTree", () => {
  it("does not inspect file paths while the tree is closed, then renders the current entries when opened", () => {
    const readPath = vi.fn(() => "src/latest.ts");
    const latest = [{ get path() { return readPath(); } }];
    const props = { entries: latest, parentWidth: 1000, onSelect: vi.fn(), onClose: vi.fn() };
    const view = render(<ChangedFilesTree {...props} open={false} />);
    expect(readPath).not.toHaveBeenCalled();
    view.rerender(<ChangedFilesTree {...props} entries={[...latest]} open={false} />);
    expect(readPath).not.toHaveBeenCalled();
    view.rerender(<ChangedFilesTree {...props} open />);
    expect(screen.getByRole("treeitem", { name: "src/latest.ts" })).toBeTruthy();
  });

  it("groups a broad directory tree without changing path identities, directory order or file rank", () => {
    const paths = Array.from({ length: 2000 }, (_, index) => `packages/pkg-${String(1999 - index).padStart(4, "0")}/src/file.ts`);
    const input = paths.map(path => ({ path }));
    const roots = buildChangedFilesTree(input, paths);
    expect(roots).toHaveLength(1);
    const root = roots[0];
    expect(root.kind).toBe("directory");
    if (root.kind !== "directory") throw new Error("Expected packages");
    expect(root.children).toHaveLength(2000);
    expect(root.children[0]).toMatchObject({ key: "packages/pkg-0000/src/", name: "pkg-0000/src" });
    expect(root.children.at(-1)).toMatchObject({ key: "packages/pkg-1999/src/", name: "pkg-1999/src" });
    for (const directory of root.children) {
      if (directory.kind !== "directory") throw new Error("Expected package directory");
      expect(directory.children).toEqual([{ kind: "file", key: `${directory.key}file.ts`, name: "file.ts", entry: expect.objectContaining({ path: `${directory.key}file.ts` }) }]);
    }
  });

  it("flattens empty parent chains, expands parents, shows real counts, and allows repeated active selection", () => {
    const choose = vi.fn();
    render(<ChangedFilesTree open entries={entries} activePath={entries[0].path} parentWidth={1000} onSelect={choose} onClose={vi.fn()} />);
    expect(screen.getByRole("treeitem", { name: "src/chat" }).getAttribute("aria-expanded")).toBe("true");
    const file = screen.getByRole("treeitem", { name: entries[0].path });
    expect(file.getAttribute("aria-selected")).toBe("true");
    expect(within(file).getByText("+12")).toBeTruthy();
    expect(within(file).getByText("-3")).toBeTruthy();
    expect(within(file).getByLabelText("2 comments")).toBeTruthy();
    expect(within(screen.getByRole("treeitem", { name: entries[1].path })).queryByText("-0")).toBeNull();
    fireEvent.click(file); fireEvent.click(file);
    expect(choose.mock.calls).toEqual([[entries[0].path], [entries[0].path]]);
    fireEvent.click(screen.getByRole("treeitem", { name: "src/chat" }));
    expect(screen.queryByRole("treeitem", { name: entries[0].path })).toBeNull();
    expect(choose).toHaveBeenCalledTimes(2);
  });

  it("takes the first current entry when the active path leaves the scope and filters with the shared matcher", () => {
    const choose = vi.fn(), close = vi.fn();
    const { rerender } = render(<ChangedFilesTree open entries={entries} activePath="outside.ts" parentWidth={1000} onSelect={choose} onClose={close} />);
    expect(screen.getByRole("treeitem", { name: entries[0].path }).getAttribute("aria-selected")).toBe("true");
    const filter = screen.getByRole("textbox");
    fireEvent.change(filter, { target: { value: "CD" } });
    const matching = new Set(rankChangedFilePaths(entries.map(entry => entry.path), "CD"));
    for (const entry of entries) expect(Boolean(screen.queryByRole("treeitem", { name: entry.path }))).toBe(matching.has(entry.path));
    rerender(<ChangedFilesTree open entries={[{ path: "current/CD.ts" }]} activePath={entries[0].path}
      parentWidth={1000} onSelect={choose} onClose={close} />);
    expect(screen.queryByRole("treeitem", { name: entries[1].path })).toBeNull();
    fireEvent.click(screen.getByRole("treeitem", { name: "current/CD.ts" }));
    expect(choose).toHaveBeenCalledExactlyOnceWith("current/CD.ts");
  });

  it("moves through directory hierarchy by keyboard without selecting a file prematurely", () => {
    const choose = vi.fn();
    render(<ChangedFilesTree open entries={entries} parentWidth={1000} onSelect={choose} onClose={vi.fn()} />);
    const directory = screen.getByRole("treeitem", { name: "src/chat" });
    directory.focus();
    fireEvent.keyDown(directory, { key: "ArrowLeft" });
    expect(directory.getAttribute("aria-expanded")).toBe("false");
    fireEvent.keyDown(directory, { key: "ArrowRight" });
    expect(directory.getAttribute("aria-expanded")).toBe("true");
    fireEvent.keyDown(directory, { key: "ArrowRight" });
    expect(document.activeElement?.getAttribute("aria-level")).toBe("2");
    expect(choose).not.toHaveBeenCalled();
  });

  it("clamps drag width to Source bounds, closes below 100, and restores the original cursor on cleanup", () => {
    const close = vi.fn();
    const { unmount } = render(<ChangedFilesTree open entries={entries} parentWidth={500} onSelect={vi.fn()} onClose={close} />);
    const separator = screen.getByRole("separator");
    expect(separator.getAttribute("aria-valuenow")).toBe("250");
    fireEvent.pointerDown(separator, { button: 0, clientX: 400 });
    fireEvent.pointerMove(window, { clientX: 0 });
    expect(separator.getAttribute("aria-valuenow")).toBe("300");
    fireEvent.pointerUp(window);
    fireEvent.pointerDown(separator, { button: 0, clientX: 400 });
    fireEvent.pointerMove(window, { clientX: 550 });
    expect(separator.getAttribute("aria-valuenow")).toBe("200");
    expect(close).not.toHaveBeenCalled();
    fireEvent.pointerMove(window, { clientX: 601 });
    expect(close).toHaveBeenCalledOnce();
    unmount();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
  });

  it("shares one Source spring progress between width and opacity and retains the closing pane until zero", () => {
    useAppStore.setState({ reducedMotion: false });
    const props = { entries, parentWidth: 1000, onSelect: vi.fn(), onClose: vi.fn() };
    const { rerender } = render(<ChangedFilesTree {...props} open />);
    rerender(<ChangedFilesTree {...props} open={false} />);
    expect(screen.getByRole("complementary", { hidden: true })).toBeTruthy();
    const [progress, target, options] = movement.animate.mock.calls.at(-1) as [MotionValue<number>, number,
      { type: string; duration: number; bounce: number; onComplete: () => void }];
    expect(target).toBe(0);
    expect(options).toMatchObject({ type: "spring", duration: .5, bounce: .1 });
    act(() => { progress.set(0); options.onComplete(); });
    expect(screen.queryByRole("complementary", { hidden: true })).toBeNull();
  });
});
