/* @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ native: null as unknown, fetch: vi.fn(), runtime: { remoteHost: { id: "remote-window", displayName: "Owned remote", kind: "ssh" } } }));
vi.mock("../desktop/runtime", () => ({ desktop: () => mocks.native, runtime: () => mocks.runtime }));
vi.mock("./api", () => ({ apiBase: () => "http://bound-window.test", authHeaders: () => ({ "X-MiniCode-Token": "window-token" }), fetchWithTimeout: mocks.fetch,
  errorMessageFromResponseText: (text: string) => text }));
import { listPersonalizationHosts, personalizationRequest } from "./memory-settings";
beforeEach(() => { mocks.native = null; mocks.fetch.mockReset().mockResolvedValue(Response.json({ feature_enabled: false })); });
afterEach(() => vi.restoreAllMocks());

it("routes selected-host requests through the narrow native API without borrowing another host's credentials", async () => {
  const listHosts = vi.fn(async () => [{ id: "local", displayName: "Local", kind: "local" }]);
  const request = vi.fn(async () => ({ feature_enabled: true }));
  mocks.native = { personalizations: { listHosts, request } };
  expect(await listPersonalizationHosts()).toEqual([{ id: "local", displayName: "Local", kind: "local" }]);
  await personalizationRequest("local", "update-memory", { enabled: true });
  expect(request).toHaveBeenCalledWith("local", "update-memory", { enabled: true });
  expect(mocks.fetch).not.toHaveBeenCalled();
  expect(mocks.runtime.remoteHost.id).toBe("remote-window");
});

it("keeps a browser page on its actual server owner and reports a different-host request", async () => {
  expect(await listPersonalizationHosts()).toEqual([{ id: "remote-window", displayName: "Owned remote", kind: "ssh" }]);
  await personalizationRequest("remote-window", "read-memory");
  expect(mocks.fetch).toHaveBeenCalledWith("http://bound-window.test/api/settings/memory", expect.objectContaining({ method: "GET" }));
  await expect(personalizationRequest("local", "read-memory")).rejects.toThrow("not connected");
  expect(mocks.fetch).toHaveBeenCalledTimes(1);
});
