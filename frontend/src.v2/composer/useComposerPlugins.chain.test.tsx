/* @vitest-environment jsdom */
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useComposerPlugins } from "./useComposerPlugins";
import { captureExtensionSettingsOwner, publishExtensionSettingsChange } from "../lib/extension-settings-events";

vi.mock("../protocol/api", () => ({
  apiBase: () => "http://plugins.test",
  authHeaders: () => ({ "X-MiniCode-Token": "owner-token" }),
  fetchWithTimeout: (url: string, init?: RequestInit) => fetch(url, init),
  errorMessageFromResponseText: (text: string) => text,
}));

beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("rebuilds an open plugin menu from the committed current scope and retires its old read", async () => {
  let finishOld!: (value: Response) => void;
  let oldSignal!: AbortSignal;
  const current = { id: "new@team", name: "new", marketplace: "team", enabled: true };
  const fetchMock = vi.mocked(fetch).mockImplementation((input, init) => {
    if (fetchMock.mock.calls.length === 1) {
      oldSignal = init!.signal!;
      return new Promise<Response>(resolve => { finishOld = resolve; });
    }
    expect(new URL(String(input)).searchParams.get("workspace_root")).toBe("C:/B");
    return Promise.resolve(Response.json({ plugins: [current] }));
  });
  const { result, rerender } = renderHook(({ root }) => useComposerPlugins(true, root), { initialProps: { root: "C:/A" } });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
  rerender({ root: "C:/B" });
  await waitFor(() => expect(result.current.plugins).toEqual([current]));
  act(() => publishExtensionSettingsChange("plugins", captureExtensionSettingsOwner(), {}));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  await waitFor(() => expect(result.current.loading).toBe(false));
  await act(async () => finishOld(Response.json({ plugins: [{ ...current, id: "old@team", name: "old" }] })));
  expect(oldSignal.aborted).toBe(true);
  expect(result.current.plugins).toEqual([current]);
  expect(result.current.error).toBe("");
});

it("does no inventory work while the menu is closed and unsubscribes on close", async () => {
  vi.mocked(fetch).mockImplementation(async () => Response.json({ plugins: [] }));
  const { result, rerender } = renderHook(({ open }) => useComposerPlugins(open, "C:/A"), { initialProps: { open: false } });
  const owner = captureExtensionSettingsOwner();
  act(() => publishExtensionSettingsChange("plugins", owner, {}));
  expect(fetch).not.toHaveBeenCalled();
  rerender({ open: true });
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(fetch).toHaveBeenCalledOnce();
  rerender({ open: false });
  act(() => publishExtensionSettingsChange("plugins", owner, {}));
  expect(fetch).toHaveBeenCalledOnce();
  rerender({ open: true });
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.error).toBe("");
});

it("replaces a pending same-scope menu request after a commit without accepting its late error", async () => {
  let finishOld!: (value: Response) => void;
  let oldSignal!: AbortSignal;
  const current = { id: "enabled@team", name: "enabled", marketplace: "team", enabled: true };
  vi.mocked(fetch).mockImplementationOnce((_input, init) => {
    oldSignal = init!.signal!;
    return new Promise<Response>(resolve => { finishOld = resolve; });
  }).mockImplementation(async () => Response.json({ plugins: [current] }));
  const { result } = renderHook(() => useComposerPlugins(true, "C:/A"));
  act(() => publishExtensionSettingsChange("plugins", captureExtensionSettingsOwner(), {}));
  expect(oldSignal.aborted).toBe(true);
  await waitFor(() => expect(result.current.plugins).toEqual([current]));
  await act(async () => finishOld(new Response("Old inventory denied", { status: 403 })));
  expect(result.current.error).toBe("");
  expect(result.current.loading).toBe(false);
  expect(result.current.plugins).toEqual([current]);
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("keeps installed plugins visible but removes them from enabled mentions when the feature is off", async () => {
  vi.mocked(fetch).mockImplementation(async () => Response.json({ plugins_enabled: false,
    plugins: [{ id: "review@team", name: "review", marketplace: "team", enabled: true }] }));
  const { result } = renderHook(() => useComposerPlugins(true, "C:/A"));
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.plugins).toEqual([{ id: "review@team", name: "review", marketplace: "team", enabled: false }]);
});
