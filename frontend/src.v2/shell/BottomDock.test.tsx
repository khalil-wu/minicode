/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { BottomDock } from "./BottomDock";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
});

vi.mock("../panels/GitPanel", () => ({
  GitPanel: () => <div>Git panel</div>,
}));

vi.mock("../panels/TerminalPanel", () => ({
  TerminalPanel: () => <div>终端面板</div>,
}));

describe("BottomDock", () => {
  beforeEach(() => {
    useAppStore.setState({
      dockCollapsed: false,
      dockHeight: 220,
      activeBottomTab: "budget",
      totalBudgetPercent: 0.42,
      budgetBuckets: [{ name: "history", used: 42, limit: 100 }],
      lastUsage: { input: 80, ordinaryInput: 55, output: 10, cacheRead: 20, cacheWrite: 5, promptCacheTotal: 80, reasoning: 12 },
      usageTotals: { input: 180, ordinaryInput: 100, output: 30, cacheRead: 70, cacheWrite: 10, promptCacheTotal: 180, reasoning: 24, turns: 3 },
    });
  });

  afterEach(() => {
    cleanup();
  });

  it("shows provider prompt-cache stats in the budget tab", () => {
    const { container } = render(<BottomDock />);

    expect(screen.getByText("模型用量")).toBeTruthy();
    expect(container.textContent).toContain("普通输入 55 · 缓存读取 20 · 缓存写入 5 · 提示词总量 80 · 命中 25% · 会话提示词 180 / 命中 39% / 3 轮");
    expect(container.textContent).toContain("推理 12 / 会话 24");
  });

  it("shows n/a when prompt usage exists but the provider reports no cache read", () => {
    useAppStore.setState({
      lastUsage: {
        input: 80,
        ordinaryInput: 80,
        output: 10,
        cacheRead: 0,
        cacheWrite: 0,
        promptCacheTotal: 80,
      },
      usageTotals: {
        input: 80,
        ordinaryInput: 80,
        output: 10,
        cacheRead: 0,
        cacheWrite: 0,
        promptCacheTotal: 80,
        turns: 1,
      },
    });
    const { container } = render(<BottomDock />);

    expect(container.textContent).toContain("普通输入 80 · 缓存读取 0 · 缓存写入 0 · 提示词总量 80 · 命中 n/a");
    expect(container.textContent).not.toContain("命中 0%");
  });

  it("keeps the floating drawer mounted but non-interactive while closed", () => {
    useAppStore.setState({ dockCollapsed: true, activeBottomTab: "terminal" });
    const { container } = render(<BottomDock />);

    const drawer = container.querySelector<HTMLElement>(".mc-bottom-drawer");
    expect(drawer?.dataset.open).toBe("false");
    expect(drawer?.getAttribute("aria-hidden")).toBe("true");
    expect(screen.queryByRole("button", { name: "终端" })).toBeNull();
  });

  it("lazily mounts terminal content in the bottom drawer", async () => {
    useAppStore.setState({ dockCollapsed: false, activeBottomTab: "terminal" });
    render(<BottomDock />);

    expect(await screen.findByText("终端面板")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "关闭底部工具" }));

    await waitFor(() => expect(useAppStore.getState().dockCollapsed).toBe(true));
    expect(screen.queryByText("终端面板")).toBeNull();
  });

  it("keeps runtime activity out of the bottom drawer", () => {
    render(<BottomDock />);

    expect(screen.queryByRole("button", { name: "活动" })).toBeNull();
  });

  it("exposes an accessible keyboard-resizable split pane", () => {
    render(<BottomDock />);

    const separator = screen.getByRole("separator", { name: "调整底部工具高度" });
    expect(separator.getAttribute("tabindex")).toBe("0");
    expect(separator.getAttribute("aria-valuenow")).toBe("220");

    fireEvent.keyDown(separator, { key: "ArrowUp" });
    expect(useAppStore.getState().dockHeight).toBe(240);
    fireEvent.keyDown(separator, { key: "Home" });
    expect(useAppStore.getState().dockHeight).toBe(180);
    fireEvent.keyDown(separator, { key: "End" });
    expect(useAppStore.getState().dockHeight).toBe(520);
  });
});
