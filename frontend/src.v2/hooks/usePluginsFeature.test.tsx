/* @vitest-environment jsdom */
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setPluginsFeatureEnabled, usePluginsFeature } from "./usePluginsFeature";

const api = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock("../protocol/api", () => ({ apiBase: () => "http://fixture", authHeaders: (extra = {}) => extra,
  fetchWithTimeout: api.fetch, errorMessageFromResponseText: (text: string, status: string) => text || status }));
const response = (enabled: boolean, source = "settings") => new Response(JSON.stringify({
  flags: [{ name: "plugins", source }], effective: { plugins: { enabled } },
}));

beforeEach(() => api.fetch.mockReset());
afterEach(cleanup);

it("one persisted global mutation updates every mounted consumer and a stale initial read cannot restore the old value", async () => {
  let release: (value: Response) => void;
  const stale = new Promise<Response>(resolve => { release = resolve; });
  api.fetch.mockReturnValueOnce(stale).mockResolvedValueOnce(response(true)).mockResolvedValueOnce(response(false));
  const first = renderHook(() => usePluginsFeature());
  const second = renderHook(() => usePluginsFeature());
  await waitFor(() => expect(second.result.current.enabled).toBe(true));
  await act(async () => { await setPluginsFeatureEnabled(false); });
  expect(first.result.current.enabled).toBe(false);
  expect(second.result.current.enabled).toBe(false);
  expect(JSON.parse(api.fetch.mock.calls[2][1].body)).toEqual({ flags: { plugins: false } });
  await act(async () => { release!(response(true)); });
  expect(first.result.current.enabled).toBe(false);
});

it("a real feature read failure remains unknown and visible instead of pretending plugins are disabled", async () => {
  api.fetch.mockResolvedValue(new Response("Actual settings read failed", { status: 500 }));
  const result = renderHook(() => usePluginsFeature());
  await waitFor(() => expect(result.result.current.error).toBe("Actual settings read failed"));
  expect(result.result.current.enabled).toBeUndefined();
});

it("policy-owned enablement is read as locked and native focus refreshes the current value", async () => {
  api.fetch.mockResolvedValueOnce(response(true, "managed")).mockResolvedValueOnce(response(false, "env"));
  const result = renderHook(() => usePluginsFeature());
  await waitFor(() => expect(result.result.current).toMatchObject({ enabled: true, locked: true }));
  act(() => window.dispatchEvent(new Event("focus")));
  await waitFor(() => expect(result.result.current).toMatchObject({ enabled: false, locked: true }));
});
