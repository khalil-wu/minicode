/* @vitest-environment jsdom */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
import { useAppStore } from "../stores";
import { BottomDock } from "../shell/BottomDock";

beforeEach(() => useAppStore.setState({ dockCollapsed: false, activeBottomTab: "budget", settingsOpen: false,
  skillsMarketplaceOpen: false, rightPanelExpanded: false, budgetBuckets: [], lastUsage: null,
  usageTotals: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, reasoning: 0, turns: 0 } }));
afterEach(cleanup);

it("aggregates ordinary input from real inclusion semantics and displays consistent cache totals", () => {
  const store = useAppStore.getState();
  store.setLastUsage({ input: 100, output: 10, cacheRead: 40, cacheWrite: 0 });
  store.setLastUsage({ input: 20, inputIncludesCacheRead: false, inputIncludesCacheWrite: false,
    output: 5, cacheRead: 50, cacheWrite: 30 });
  expect(useAppStore.getState().usageTotals).toMatchObject({ ordinaryInput: 80, promptCacheTotal: 200, turns: 2 });
  render(<BottomDock />);
  const display = screen.getByText(/普通输入 20/).textContent;
  expect(display).toContain("提示词总量 100");
  expect(display).toContain("会话提示词 200 / 命中 45% / 2 轮");
});

it("retains ordinary input from a prior aggregate lacking the optional ordinary field", () => {
  useAppStore.setState({ usageTotals: { input: 100, output: 10, cacheRead: 40, cacheWrite: 0, promptCacheTotal: 100, turns: 1 } });
  useAppStore.getState().setLastUsage({ input: 20, inputIncludesCacheRead: false, inputIncludesCacheWrite: false,
    output: 5, cacheRead: 50, cacheWrite: 30 });
  expect(useAppStore.getState().usageTotals).toMatchObject({ ordinaryInput: 80, promptCacheTotal: 200, turns: 2 });
});
