/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MarketplaceRemoveDialog, MarketplaceSettings } from "./MarketplaceSettings";
import type { PluginEntry, PluginMarketplace } from "./PluginsTab";
vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", { writable: true,
    value: vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) });
});

const market: PluginMarketplace = { name: "team", interface: { displayName: "Team tools" }, status: "ready", source: { source: "git", url: "https://example.test/team.git" },
  materialized_path: "C:/catalogs/team", plugins: [{ id: "first@team", name: "first" }, { id: "second@team", name: "second" }] };
const installed: PluginEntry[] = [{ id: "first@team", name: "first", displayName: "First", path: "C:/plugins/first", marketplace: "team", enabled: true },
  { id: "second@team", name: "second", displayName: "Second", path: "C:/plugins/second", marketplace: "team", enabled: false }];
afterEach(cleanup);

it("searches user marketplace metadata and permits upgrades only for Git sources", () => {
  const onUpgrade = vi.fn(), onRemove = vi.fn();
  const local = { ...market, name: "local", interface: undefined, materialized_path: "C:/catalogs/local", source: { source: "directory", path: "C:/market" } };
  const view = render(<MarketplaceSettings marketplaces={[market, local, { ...market, name: "builtin", isBuiltIn: true }]}
    search="" loading={false} error="" busy={false} onRetry={vi.fn()} onUpgrade={onUpgrade} onRemove={onRemove} />);
  expect(screen.getByText("Team tools")).toBeTruthy();
  expect(screen.queryByText("builtin")).toBeNull();
  expect(screen.getByRole("button", { name: "升级插件市场 local" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "升级插件市场 Team tools" }));
  expect(onUpgrade).toHaveBeenCalledWith(market);
  expect(screen.queryByRole("button", { name: /编辑/ })).toBeNull();
  view.rerender(<MarketplaceSettings marketplaces={[market, local]} search="catalogs/team" loading={false} error="" busy={false} onRetry={vi.fn()} onUpgrade={onUpgrade} onRemove={onRemove} />);
  expect(screen.queryByText("local", { exact: true })).toBeNull();
  expect(screen.getByText("Team tools")).toBeTruthy();
});

it("loads the real installed list and resumes from the failed uninstall before removing the source", async () => {
  const onLoadInstalled = vi.fn(async () => installed);
  const onUninstall = vi.fn().mockResolvedValueOnce([]).mockRejectedValueOnce(new Error("second is locked")).mockResolvedValueOnce([]);
  const onRemove = vi.fn(async () => {}), onClose = vi.fn();
  render(<MarketplaceRemoveDialog marketplace={market} onLoadInstalled={onLoadInstalled} onUninstall={onUninstall}
    onRefreshRuntime={vi.fn()} onRemove={onRemove} onClose={onClose} />);
  fireEvent.click(await screen.findByRole("button", { name: "卸载并移除" }));
  expect(await screen.findByText("second is locked")).toBeTruthy();
  expect(onRemove).not.toHaveBeenCalled();
  expect(screen.getByText("已卸载")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  expect(onUninstall.mock.calls.map(([plugin]) => plugin.id)).toEqual(["first@team", "second@team", "second@team"]);
  expect(onRemove).toHaveBeenCalledTimes(1);
});

it("retries runtime refresh without deleting the already-uninstalled plugin again", async () => {
  const onUninstall = vi.fn().mockResolvedValueOnce(["old MCP process is still active"]).mockResolvedValueOnce([]);
  const onRefreshRuntime = vi.fn(async () => {}), onRemove = vi.fn(async () => {});
  render(<MarketplaceRemoveDialog marketplace={market} onLoadInstalled={async () => installed} onUninstall={onUninstall}
    onRefreshRuntime={onRefreshRuntime} onRemove={onRemove} onClose={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "卸载并移除" }));
  expect(await screen.findByText("old MCP process is still active")).toBeTruthy();
  expect(onRemove).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  await waitFor(() => expect(onRemove).toHaveBeenCalledTimes(1));
  expect(onRefreshRuntime).toHaveBeenCalledTimes(1);
  expect(onUninstall.mock.calls.map(([plugin]) => plugin.id)).toEqual(["first@team", "second@team"]);
});

it("does not treat an installed-inventory failure as an empty market", async () => {
  const load = vi.fn().mockRejectedValueOnce(new Error("inventory denied")).mockResolvedValueOnce([]);
  const onRemove = vi.fn(async () => {}), onUninstall = vi.fn();
  render(<MarketplaceRemoveDialog marketplace={market} onLoadInstalled={load} onUninstall={onUninstall}
    onRefreshRuntime={vi.fn()} onRemove={onRemove} onClose={vi.fn()} />);
  expect(await screen.findByText("inventory denied")).toBeTruthy();
  expect(screen.getByRole("button", { name: "移除", exact: true })).toHaveProperty("disabled", true);
  fireEvent.click(within(screen.getByRole("alert")).getByRole("button", { name: "重试" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "移除", exact: true })).toHaveProperty("disabled", false));
  fireEvent.click(screen.getByRole("button", { name: "移除", exact: true }));
  await waitFor(() => expect(onRemove).toHaveBeenCalledTimes(1));
  expect(onUninstall).not.toHaveBeenCalled();
});

it("stops the remaining uninstall sequence after the owning dialog unmounts", async () => {
  let complete!: (errors: string[]) => void;
  const uninstall = vi.fn(() => new Promise<string[]>(resolve => { complete = resolve; }));
  const remove = vi.fn(async () => {});
  const view = render(<MarketplaceRemoveDialog marketplace={market} onLoadInstalled={async () => installed} onUninstall={uninstall}
    onRefreshRuntime={vi.fn()} onRemove={remove} onClose={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "卸载并移除" }));
  view.unmount();
  await act(async () => complete([]));
  expect(uninstall).toHaveBeenCalledTimes(1);
  expect(remove).not.toHaveBeenCalled();
});
