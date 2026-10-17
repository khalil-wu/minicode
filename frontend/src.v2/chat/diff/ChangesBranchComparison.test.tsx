// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LanguageProvider } from "../../i18n";
import { ChangesBranchComparison, type ChangesBranchComparisonProps } from "./ChangesBranchComparison";

const view = (props: ChangesBranchComparisonProps) => <LanguageProvider locale="en-US"><ChangesBranchComparison {...props} /></LanguageProvider>;
beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("ChangesBranchComparison", () => {
  it.each([undefined, null, []])("renders no invented default branch when there is no target, request or available option (%j)", branches => {
    render(view({ currentBranch: "feature/actual", branches, onSelectBranch: vi.fn() }));
    expect(screen.queryByRole("group")).toBeNull();
    expect(screen.queryByText("main")).toBeNull();
  });

  it("uses HEAD only for an absent current branch and displays the actual target/default range", () => {
    const { rerender } = render(view({ defaultTargetBranch: "trunk", branches: ["trunk", "release"], onSelectBranch: vi.fn() }));
    const group = screen.getByRole("group", { name: "Branch comparison" });
    expect(group.textContent).toBe("HEAD→trunk");
    rerender(view({ currentBranch: "feature/actual", defaultTargetBranch: "trunk", targetBranch: "release",
      branches: ["trunk", "release"], onSelectBranch: vi.fn() }));
    expect(group.textContent).toBe("feature/actual→release");
    expect(screen.getByRole("button", { name: "release" })).toBeTruthy();
  });

  it("deduplicates actual selected/default/list entries in Source order and closes on a menu row", () => {
    const choose = vi.fn();
    render(view({ currentBranch: "feature", targetBranch: "release", defaultTargetBranch: "trunk",
      branches: ["trunk", "feature", "release", "staging"], onSelectBranch: choose }));
    const trigger = screen.getByRole("button", { name: "release" });
    fireEvent.click(trigger);
    const menu = screen.getByRole("menu");
    expect(menu.parentElement).toBe(document.body);
    expect(within(menu).getAllByRole("menuitem").map(row => row.textContent)).toEqual(["release", "trunk", "feature", "staging"]);
    expect(within(menu).getByRole("menuitem", { name: "release" }).getAttribute("data-selected")).toBe("true");
    fireEvent.click(within(menu).getByRole("menuitem", { name: "staging" }));
    expect(choose).toHaveBeenCalledExactlyOnceWith("staging");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("keeps an undefined branch list unavailable instead of fabricating target/default options", () => {
    render(view({ targetBranch: "known-ref", defaultTargetBranch: "actual-default", onSelectBranch: vi.fn() }));
    fireEvent.click(screen.getByRole("button", { name: "known-ref" }));
    expect(screen.getByText("No branches found")).toBeTruthy();
    expect(screen.queryAllByRole("menuitem")).toHaveLength(0);
  });

  it("filters by case-insensitive substring but treats a typed exact ref as case-sensitive", () => {
    const choose = vi.fn();
    render(view({ currentBranch: "feature", targetBranch: "main", branches: ["main", "maint", "release"], onSelectBranch: choose }));
    fireEvent.click(screen.getByRole("button", { name: "main" }));
    const input = screen.getByRole("textbox", { name: "Search branches" });
    fireEvent.change(input, { target: { value: "  MAIN  " } });
    expect(screen.getAllByRole("menuitem").map(row => row.textContent)).toEqual(["Use MAIN", "main", "maint"]);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(choose).toHaveBeenCalledExactlyOnceWith("MAIN");
    expect(screen.getByRole("menu")).toBeTruthy();
    fireEvent.change(input, { target: { value: " main " } });
    expect(screen.queryByRole("menuitem", { name: "Use main" })).toBeNull();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(choose.mock.calls.at(-1)).toEqual(["main"]);
  });

  it.each([{ loading: true }, { error: true }])("allows a real custom ref during the initial base-list request (%j)", state => {
    const choose = vi.fn();
    render(view({ branches: null, ...state, onSelectBranch: choose }));
    fireEvent.click(screen.getByRole("button", { name: "Select branch" }));
    const input = screen.getByRole("textbox", { name: "Search branches" });
    fireEvent.change(input, { target: { value: " refs/tags/v4.2 " } });
    expect(screen.queryByText("Loading branches…")).toBeNull();
    expect(screen.queryByText("Unable to load branches")).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "Use refs/tags/v4.2" }));
    expect(choose).toHaveBeenCalledExactlyOnceWith("refs/tags/v4.2");
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("reports actual loading/error states and retries without choosing a branch", () => {
    const retry = vi.fn(), choose = vi.fn();
    const { rerender } = render(view({ loading: true, branches: null, onRetry: retry, onSelectBranch: choose }));
    fireEvent.click(screen.getByRole("button", { name: "Select branch" }));
    expect(screen.getByRole("status").textContent).toBe("Loading branches…");
    rerender(view({ error: true, branches: null, onRetry: retry, onSelectBranch: choose }));
    expect(screen.getByText("Unable to load branches")).toBeTruthy();
    fireEvent.click(screen.getByRole("menuitem", { name: "Retry" }));
    expect(retry).toHaveBeenCalledOnce();
    expect(choose).not.toHaveBeenCalled();
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("retains full long names while preserving Source's current/base suffix spans", () => {
    const current = "feature/very-long-description/current-suffix", target = "release/very-long-description/target-suffix-to-retain";
    render(view({ currentBranch: current, targetBranch: target, branches: [target], onSelectBranch: vi.fn() }));
    const group = screen.getByRole("group");
    const currentName = group.querySelector('[data-suffix-characters="18"]')!;
    const targetName = group.querySelector('[data-suffix-characters="28"]')!;
    expect(currentName.textContent).toBe(current);
    expect(targetName.textContent).toBe(target);
    expect(currentName.querySelector(".mc-changes-branch-suffix")?.textContent).toBe(current.slice(-18));
    expect(targetName.querySelector(".mc-changes-branch-suffix")?.textContent).toBe(target.slice(-28));
  });

  it("moves from search to menu rows, returns from the first row, and does not select blank input on Enter", () => {
    const choose = vi.fn();
    render(view({ targetBranch: "main", branches: ["main", "release"], onSelectBranch: choose }));
    const trigger = screen.getByRole("button", { name: "main" });
    fireEvent.click(trigger);
    const menu = screen.getByRole("menu"), input = screen.getByRole("textbox", { name: "Search branches" });
    expect(document.activeElement).toBe(menu);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(choose).not.toHaveBeenCalled();
    expect(screen.getByRole("menu")).toBeTruthy();
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(document.activeElement).toBe(within(menu).getByRole("menuitem", { name: "main" }));
    fireEvent.keyDown(document.activeElement!, { key: "ArrowUp" });
    expect(document.activeElement).toBe(input);
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("uses Source menu typeahead cycling without replacing the branch search field", () => {
    vi.useFakeTimers();
    try {
      render(view({ targetBranch: "release", branches: ["release", "review", "trunk"], onSelectBranch: vi.fn() }));
      fireEvent.click(screen.getByRole("button", { name: "release" }));
      const menu = screen.getByRole("menu");
      fireEvent.keyDown(menu, { key: "r" });
      vi.advanceTimersByTime(0);
      expect(document.activeElement).toBe(within(menu).getByRole("menuitem", { name: "release" }));
      fireEvent.keyDown(document.activeElement!, { key: "r" });
      vi.advanceTimersByTime(0);
      expect(document.activeElement).toBe(within(menu).getByRole("menuitem", { name: "review" }));
      expect((screen.getByRole("textbox", { name: "Search branches" }) as HTMLInputElement).value).toBe("");
    } finally { vi.useRealTimers(); }
  });
});
