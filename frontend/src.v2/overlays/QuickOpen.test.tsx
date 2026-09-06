/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  searchWorkspaceFiles: vi.fn(),
  fsSearchFiles: vi.fn(),
}));

vi.mock("../protocol/workspace", () => ({
  searchWorkspaceFiles: (...args: unknown[]) => mocks.searchWorkspaceFiles(...args),
}));
vi.mock("../desktop/runtime", () => ({
  isDesktop: () => false,
  fsSearchFiles: (...args: unknown[]) => mocks.fsSearchFiles(...args),
}));
vi.mock("../hooks/useFocusTrap", () => ({
  useFocusTrap: () => undefined,
}));

import { QuickOpen } from "./QuickOpen";
import { useAppStore } from "../stores";

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
};

describe("QuickOpen", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mocks.searchWorkspaceFiles.mockReset();
    mocks.fsSearchFiles.mockReset();
    useAppStore.setState({
      quickOpenVisible: true,
      quickOpenResults: [],
      quickOpenLoading: false,
      editorTabs: [],
      editorOpenRequests: [],
      workingDirectory: "C:\\workspace-a",
      runtimeCapabilities: {},
    });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("drops a late result from an older query", async () => {
    const first = deferred<Array<{ path: string; name: string }>>();
    const second = deferred<Array<{ path: string; name: string }>>();
    mocks.searchWorkspaceFiles
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    render(<QuickOpen />);
    const input = screen.getByRole("combobox", { name: "搜索文件" });

    fireEvent.change(input, { target: { value: "rea" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    fireEvent.change(input, { target: { value: "read" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });

    first.resolve([{ path: "README.md", name: "README.md" }]);
    await act(async () => { await Promise.resolve(); });
    expect(useAppStore.getState().quickOpenResults).toEqual([]);

    second.resolve([{ path: "src/reader.ts", name: "reader.ts" }]);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole("option", { name: /reader\.ts/ })).toBeTruthy();
    expect(mocks.searchWorkspaceFiles).toHaveBeenNthCalledWith(
      1,
      "C:\\workspace-a",
      "rea",
      20,
      "file",
    );
  });

  it("does not publish a result under a different workspace", async () => {
    const pending = deferred<Array<{ path: string; name: string }>>();
    mocks.searchWorkspaceFiles.mockReturnValueOnce(pending.promise).mockResolvedValue([{ path: "src/new-app.ts", name: "new-app.ts" }]);
    render(<QuickOpen />);
    fireEvent.change(screen.getByRole("combobox", { name: "搜索文件" }), { target: { value: "app" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });

    act(() => { useAppStore.setState({ workingDirectory: "C:\\workspace-b" }); });
    pending.resolve([{ path: "src/app.ts", name: "app.ts" }]);
    await act(async () => { await Promise.resolve(); });

    expect(useAppStore.getState().quickOpenResults).toEqual([]);
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    expect(mocks.searchWorkspaceFiles).toHaveBeenLastCalledWith("C:\\workspace-b", "app", 20, "file");
    expect(screen.getByRole("option", { name: /new-app\.ts/ })).toBeTruthy();
  });

  it("shows search failures instead of reporting an empty workspace", async () => {
    mocks.searchWorkspaceFiles.mockRejectedValue(new Error("服务不可用"));
    render(<QuickOpen />);
    fireEvent.change(screen.getByRole("combobox", { name: "搜索文件" }), { target: { value: "app" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByRole("alert").textContent).toContain("服务不可用");
    expect(screen.queryByText("未找到文件。")).toBeNull();
  });

  it("clears already completed results as soon as the workspace changes", async () => {
    mocks.searchWorkspaceFiles.mockResolvedValueOnce([{ path: "old.ts", name: "old.ts" }]);
    render(<QuickOpen />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "file" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    expect(screen.getByRole("option", { name: /old\.ts/ })).toBeTruthy();

    act(() => useAppStore.setState({ workingDirectory: "C:\\workspace-b" }));

    expect(screen.queryByRole("option", { name: /old\.ts/ })).toBeNull();
    expect(useAppStore.getState().quickOpenResults).toEqual([]);
  });

  it("cancels queued searches when the input is cleared or the dialog closes", async () => {
    render(<QuickOpen />);
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "first" } });
    fireEvent.change(input, { target: { value: "" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    expect(mocks.searchWorkspaceFiles).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: "second" } });
    fireEvent.keyDown(input, { key: "Escape" });
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    expect(mocks.searchWorkspaceFiles).not.toHaveBeenCalled();
  });

  it("allows Enter to open the first result after ArrowDown during loading", async () => {
    mocks.searchWorkspaceFiles.mockResolvedValue([{ path: "first.ts", name: "first.ts" }]);
    render(<QuickOpen />);
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "first" } });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(useAppStore.getState().editorOpenRequests.at(-1)?.path).toBe("first.ts");
  });
});
