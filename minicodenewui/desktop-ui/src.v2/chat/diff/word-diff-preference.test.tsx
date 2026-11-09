/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { InlineDiff } from "./InlineDiff";
import {
  readWordDiffsEnabled,
  setWordDiffsEnabled,
  subscribeWordDiffsPreference,
  useWordDiffsPreference,
  WORD_DIFFS_STORAGE_KEY,
} from "./word-diff-preference";

vi.mock("../../lib/monaco-colorize", async original => ({
  ...await original<typeof import("../../lib/monaco-colorize")>(),
  useColorizedLines: vi.fn((lines: { kind: string; text: string }[]) => lines.map(line => {
    const span = document.createElement("span");
    span.className = "syntax-token";
    span.textContent = line.text;
    return span.outerHTML;
  })),
}));

beforeEach(() => { localStorage.removeItem(WORD_DIFFS_STORAGE_KEY); });
afterEach(() => { cleanup(); localStorage.removeItem(WORD_DIFFS_STORAGE_KEY); vi.restoreAllMocks(); });

function PreferenceControl({ label = "word preference" }: { label?: string }) {
  const [enabled, setEnabled] = useWordDiffsPreference();
  return <button type="button" role="switch" aria-label={label} aria-checked={enabled} onClick={() => setEnabled(!enabled)} />;
}

const storageEvent = (key: string | null, storageArea = localStorage, newValue: string | null = null) =>
  window.dispatchEvent(new StorageEvent("storage", { key, storageArea, newValue }));

describe("app-wide Source word diff preference", () => {
  it("starts enabled, persists a real boolean and restores it on a new mount", () => {
    expect(readWordDiffsEnabled()).toBe(true);
    const first = render(<PreferenceControl />);
    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByRole("switch"));
    expect(localStorage.getItem(WORD_DIFFS_STORAGE_KEY)).toBe("false");
    first.unmount();
    render(<PreferenceControl />);
    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("false");
    expect(readWordDiffsEnabled()).toBe(false);
  });

  it("updates every same-window caller and both actual InlineDiff projections while retaining the draft and syntax", () => {
    const patch = "@@ -10 +20 @@\n-const answer = old;\n+const answer = new;";
    const { container } = render(<>
      <PreferenceControl label="first" /><PreferenceControl label="second" />
      <textarea aria-label="unrelated draft" defaultValue="keep me" />
      <InlineDiff patch={patch} filePath="first.ts" />
      <InlineDiff patch={patch} filePath="second.ts" />
    </>);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "unsaved draft" } });
    expect(container.querySelectorAll("[data-diff-span]")).toHaveLength(4);
    fireEvent.click(screen.getByRole("switch", { name: "first" }));
    expect(screen.getByRole("switch", { name: "second" }).getAttribute("aria-checked")).toBe("false");
    expect(container.querySelectorAll("[data-diff-span]")).toHaveLength(0);
    expect(container.querySelectorAll(".syntax-token")).toHaveLength(4);
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("unsaved draft");
    fireEvent.click(screen.getByRole("switch", { name: "second" }));
    expect(container.querySelectorAll("[data-diff-span]")).toHaveLength(4);
  });

  it("reads the latest other-window value, deletion and clear instead of replaying an old payload", () => {
    render(<PreferenceControl />);
    act(() => {
      localStorage.setItem(WORD_DIFFS_STORAGE_KEY, "false");
      storageEvent(WORD_DIFFS_STORAGE_KEY, localStorage, "false");
    });
    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("false");
    act(() => {
      localStorage.setItem(WORD_DIFFS_STORAGE_KEY, "true");
      storageEvent(WORD_DIFFS_STORAGE_KEY, localStorage, "false");
    });
    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true");
    act(() => { setWordDiffsEnabled(false); });
    act(() => { localStorage.removeItem(WORD_DIFFS_STORAGE_KEY); storageEvent(WORD_DIFFS_STORAGE_KEY); });
    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true");
    act(() => { setWordDiffsEnabled(false); });
    act(() => { localStorage.removeItem(WORD_DIFFS_STORAGE_KEY); storageEvent(null); });
    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true");
  });

  it("shares one native listener, ignores unrelated storage and removes it after the last consumer", () => {
    const add = vi.spyOn(window, "addEventListener"), remove = vi.spyOn(window, "removeEventListener");
    const first = vi.fn(), second = vi.fn();
    const stopFirst = subscribeWordDiffsPreference(first), stopSecond = subscribeWordDiffsPreference(second);
    expect(add.mock.calls.filter(([name]) => name === "storage")).toHaveLength(1);
    storageEvent("minicode.other-setting");
    storageEvent(WORD_DIFFS_STORAGE_KEY, sessionStorage);
    expect(first).not.toHaveBeenCalled();
    expect(second).not.toHaveBeenCalled();
    setWordDiffsEnabled(false);
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);
    stopFirst();
    expect(remove.mock.calls.filter(([name]) => name === "storage")).toHaveLength(0);
    stopSecond();
    expect(remove.mock.calls.filter(([name]) => name === "storage")).toHaveLength(1);
    expect(remove.mock.calls.find(([name]) => name === "storage")?.[1]).toBe(add.mock.calls.find(([name]) => name === "storage")?.[1]);
  });
});
