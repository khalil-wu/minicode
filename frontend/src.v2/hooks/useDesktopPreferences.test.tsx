/* @vitest-environment jsdom */
import { StrictMode } from "react";
import { act, cleanup, fireEvent, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { DesktopPreferences, DesktopPreferenceTargets } from "../desktop/runtime";
import { useDesktopPreferences } from "./useDesktopPreferences";
import { useDesktopPreferenceTargets } from "./useDesktopPreferenceTargets";
import { useDesktopBehaviorPreferences } from "./useDesktopBehaviorPreferences";
import { useAppStore } from "../stores";
import { defaultWorkbenchPreferences, useDesktopDictationPreferences } from "../lib/workbench-preferences";
import { pushToast } from "../overlays/ToastContainer";

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (reason: Error) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function bindNative() {
  const initial = { ...defaultWorkbenchPreferences, fileOpenTarget: "minicode", fileOpenTargetPerPath: {}, showBottomPanel: true,
    defaultTerminalLocation: "bottom", composerPlainTextMode: false, primaryNumberShortcutTarget: "tabs" } as DesktopPreferences;
  const listeners = new Set<(value: DesktopPreferences) => void>();
  const stop = vi.fn();
  const onChanged = vi.fn((listener: (value: DesktopPreferences) => void) => {
    listeners.add(listener); return () => { listeners.delete(listener); stop(); };
  });
  const get = vi.fn(async () => initial), set = vi.fn(async () => initial);
  const getTargets = vi.fn(async (): Promise<DesktopPreferenceTargets> => ({ fileOpenTargets: [], terminalShellTargets: [] }));
  window.__MINICODE_RUNTIME__ = { desktop: { preferences: { get, set, getTargets, onChanged } } } as never;
  return { initial, get, set, getTargets, onChanged, stop, listeners, emit: (value: DesktopPreferences) => listeners.forEach(listener => listener(value)) };
}

afterEach(() => { cleanup(); delete window.__MINICODE_RUNTIME__; vi.clearAllMocks(); });

it("shares one native read and subscription across StrictMode consumers and keeps a live change over a late snapshot", async () => {
  const native = bindNative(), initial = deferred<DesktopPreferences>();
  native.get.mockReturnValue(initial.promise);
  const a = renderHook(useDesktopPreferences, { wrapper: StrictMode });
  const b = renderHook(useDesktopPreferences);
  expect(native.get).toHaveBeenCalledTimes(1);
  expect(native.listeners.size).toBe(1);
  const changed = { ...native.initial, showBottomPanel: false };
  act(() => native.emit(changed));
  await act(async () => initial.resolve(native.initial));
  expect(a.result.current.preferences).toBe(changed);
  expect(b.result.current.preferences).toBe(changed);
  a.unmount();
  expect(native.listeners.size).toBe(1);
  b.unmount();
  expect(native.listeners.size).toBe(0);
});

it("retains the real committed values when an overlapping write fails", async () => {
  const native = bindNative();
  const view = renderHook(useDesktopPreferences);
  await waitFor(() => expect(view.result.current.preferences).toBe(native.initial));
  const first = deferred<DesktopPreferences>(), second = deferred<DesktopPreferences>();
  native.set.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  let a!: Promise<void>, b!: Promise<void>;
  act(() => { a = view.result.current.update({ showBottomPanel: false }); b = view.result.current.update({ showBottomPanel: true }); });
  const saved = { ...native.initial, showBottomPanel: false };
  await act(async () => { native.emit(saved); first.resolve(saved); await a; });
  await act(async () => { second.reject(new Error("Actual disk error")); await b; });
  expect(view.result.current.preferences).toBe(saved);
  expect(pushToast).toHaveBeenCalledWith("Actual disk error", "error");
});

it("clears an initial read error when a real native change arrives", async () => {
  const native = bindNative();
  native.get.mockRejectedValueOnce(new Error("Actual native read error"));
  const view = renderHook(useDesktopPreferences);
  await waitFor(() => expect(view.result.current.error).toContain("Actual native read error"));
  act(() => native.emit(native.initial));
  expect(view.result.current.error).toBe("");
  expect(view.result.current.preferences).toBe(native.initial);
});

it("discovers targets only while visible, refreshes on focus, and ignores a hidden lifetime's response", async () => {
  const native = bindNative(), obsolete = deferred<DesktopPreferenceTargets>();
  const view = renderHook(({ active }) => useDesktopPreferenceTargets(active), { initialProps: { active: false } });
  fireEvent.focus(window);
  expect(native.getTargets).not.toHaveBeenCalled();
  native.getTargets.mockReturnValueOnce(obsolete.promise);
  view.rerender({ active: true });
  expect(native.getTargets).toHaveBeenCalledTimes(1);
  view.rerender({ active: false });
  await act(async () => obsolete.resolve({ fileOpenTargets: [{ id: "cursor", label: "Obsolete", kind: "editor" }], terminalShellTargets: [] }));
  expect(view.result.current.targets).toBeNull();
  view.rerender({ active: true });
  await waitFor(() => expect(view.result.current.targets?.fileOpenTargets).toEqual([]));
  native.getTargets.mockRejectedValueOnce(new Error("Actual discovery error"));
  fireEvent.focus(window);
  await waitFor(() => expect(view.result.current.error).toContain("Actual discovery error"));
  fireEvent.focus(window);
  await waitFor(() => expect(view.result.current.error).toBe(""));
  expect(native.getTargets).toHaveBeenCalledTimes(4);
});

it("does not rewrite workbench state or drafts when an unrelated preference changes", async () => {
  const native = bindNative();
  useDesktopDictationPreferences();
  useAppStore.setState({ workbenchPreferences: { ...defaultWorkbenchPreferences }, defaultTerminalLocation: "bottom", composerPlainTextMode: false,
    primaryNumberShortcutTarget: "tabs", draft: "Keep this unsent draft", workspaceComposerDrafts: {} });
  renderHook(useDesktopBehaviorPreferences);
  await waitFor(() => expect(native.listeners.size).toBe(1));
  const unchanged = useAppStore.getState(), changed = vi.fn(), stop = useAppStore.subscribe(changed);
  await act(async () => native.emit({ ...native.initial, showBottomPanel: false }));
  expect(changed).not.toHaveBeenCalled();
  expect(useAppStore.getState()).toBe(unchanged);
  stop();
});
