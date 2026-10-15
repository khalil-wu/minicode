/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { BrowserIntegrationTab } from "./BrowserIntegrationTab";
import type { EmbeddedBrowserSettingsSnapshot } from "../desktop/runtime";
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: preferenceState.current, error: "", update: vi.fn() }) }));

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
const { list, settings, update, historySearch, historyRemove, preferenceState, settingsListeners } = vi.hoisted(() => ({ list: vi.fn(), settings: vi.fn(), update: vi.fn(), historySearch: vi.fn(), historyRemove: vi.fn(), preferenceState: { current: null as Record<string, unknown> | null }, settingsListeners: new Set<(settings: EmbeddedBrowserSettingsSnapshot) => void>() }));
vi.mock("../desktop/runtime", () => ({ desktop: () => null, isDesktop: () => true,
  embeddedBrowserList: list, embeddedBrowserGetSettings: settings, embeddedBrowserSetSettings: update, browserHistorySearch: historySearch, browserHistoryRemove: historyRemove,
  onEmbeddedBrowserSettingsChanged: (listener: (settings: EmbeddedBrowserSettingsSnapshot) => void) => { settingsListeners.add(listener); return () => { settingsListeners.delete(listener); }; } }));
beforeEach(() => {
  vi.clearAllMocks();
  settingsListeners.clear();
  preferenceState.current = null;
  historySearch.mockResolvedValue([]);
  historyRemove.mockResolvedValue(true);
  list.mockImplementation(async (owner: string) => [{ id: `tab-${owner}`, title: `${owner} 页面`, active: true }]);
  settings.mockResolvedValue({ downloadPolicy: "ask", origin: "", permissions: [] });
  useAppStore.setState({ conversationId: "A", pendingConversationSwitchId: null });
});
afterEach(cleanup);

it("reflects a committed setting from another window without losing it to an older read", async () => {
  let finishSettings!: (settings: unknown) => void;
  settings.mockReturnValueOnce(new Promise((resolve) => { finishSettings = resolve; }));
  render(<BrowserIntegrationTab />);
  fireEvent.click(screen.getByText("高级"));
  act(() => { for (const listener of settingsListeners) listener({ downloadPolicy: "allow", sitePermissions: {} }); });
  expect(screen.getByRole("button", { name: "浏览器下载策略，当前：保存到下载目录" })).toBeTruthy();
  await act(async () => finishSettings({ downloadPolicy: "block", origin: "", permissions: [] }));
  expect(screen.getByLabelText("浏览器下载策略")).toHaveProperty("value", "allow");
});

it.each([false, true])("keeps a newer committed window change when its own delayed write settles (failure=%s)", async (failure) => {
  let settle!: () => void;
  update.mockReturnValueOnce(new Promise((resolve, reject) => {
    settle = () => failure ? reject(new Error("original write failed")) : resolve({ downloadPolicy: "allow", origin: "", permissions: [] });
  }));
  render(<BrowserIntegrationTab />);
  fireEvent.click(screen.getByText("高级"));
  await waitFor(() => expect(screen.getByLabelText("浏览器下载策略")).toHaveProperty("value", "ask"));
  fireEvent.click(screen.getByRole("button", { name: "浏览器下载策略，当前：每次询问" }));
  fireEvent.click(screen.getByRole("option", { name: "保存到下载目录" }));
  act(() => { for (const listener of settingsListeners) listener({ downloadPolicy: "block", sitePermissions: {} }); });
  await act(async () => settle());
  expect(screen.getByLabelText("浏览器下载策略")).toHaveProperty("value", "block");
});

it("keeps the confirmed download policy when persistence fails", async () => {
  update.mockRejectedValueOnce(new Error("settings disk failure"));
  render(<BrowserIntegrationTab />);
  fireEvent.click(screen.getByText("高级"));
  await waitFor(() => expect(screen.getByLabelText("浏览器下载策略")).toHaveProperty("value", "ask"));
  fireEvent.click(screen.getByRole("button", { name: "浏览器下载策略，当前：每次询问" }));
  fireEvent.click(screen.getByRole("option", { name: "保存到下载目录" }));
  await waitFor(() => expect(screen.getByLabelText("浏览器下载策略")).toHaveProperty("value", "ask"));
  expect(update).toHaveBeenCalledWith({ downloadPolicy: "allow" });
});

it("clears a previous owner's visible tabs while loading and refuses an old late tab response", async () => {
  let finishOld!: (tabs: unknown[]) => void;
  let finishNew!: (tabs: unknown[]) => void;
  list.mockReturnValueOnce(new Promise((resolve) => { finishOld = resolve; }))
    .mockReturnValueOnce(new Promise((resolve) => { finishNew = resolve; }));
  render(<BrowserIntegrationTab />);
  fireEvent.click(screen.getByText("高级"));
  act(() => useAppStore.setState({ conversationId: "B" }));
  await act(async () => finishOld([{ id: "old", title: "Late A", active: true }]));
  expect(screen.queryByText("Late A")).toBeNull();
  await act(async () => finishNew([{ id: "new", title: "Current B", active: true }]));
  expect(screen.getByText("Current B")).toBeTruthy();
});

it("does not overwrite a saved global download policy with an older settings read", async () => {
  let finishSettings!: (settings: unknown) => void;
  render(<BrowserIntegrationTab />);
  fireEvent.click(screen.getByText("高级"));
  await waitFor(() => expect(screen.getByLabelText("浏览器下载策略")).toHaveProperty("value", "ask"));
  settings.mockReturnValueOnce(new Promise((resolve) => { finishSettings = resolve; }));
  act(() => useAppStore.setState({ conversationId: "B" }));
  expect(screen.queryByText("A 页面")).toBeNull();
  update.mockResolvedValueOnce({ downloadPolicy: "allow", origin: "", permissions: [] });
  fireEvent.click(screen.getByRole("button", { name: "浏览器下载策略，当前：每次询问" }));
  fireEvent.click(screen.getByRole("option", { name: "保存到下载目录" }));
  await waitFor(() => expect(update).toHaveBeenCalledWith({ downloadPolicy: "allow" }));
  await act(async () => { await Promise.resolve(); });
  await act(async () => finishSettings({ downloadPolicy: "block", origin: "", permissions: [] }));
  expect(screen.getByLabelText("浏览器下载策略")).toHaveProperty("value", "allow");
  expect(screen.getByText("B 页面")).toBeTruthy();
});

it("pauses hidden-page browser IPC across owner changes and refreshes the current owner on reopening", async () => {
  const view = render(<BrowserIntegrationTab active={false} />);
  fireEvent.click(screen.getByText("高级"));
  act(() => useAppStore.setState({ conversationId: "B" }));
  expect(list).not.toHaveBeenCalled();
  expect(settings).not.toHaveBeenCalled();
  view.rerender(<BrowserIntegrationTab active />);
  expect(await screen.findByText("B 页面")).toBeTruthy();
  expect(list).toHaveBeenCalledWith("B");
  expect(settings).toHaveBeenCalledTimes(1);
  view.rerender(<BrowserIntegrationTab active={false} />);
  act(() => useAppStore.setState({ conversationId: "C" }));
  act(() => useAppStore.setState({ conversationId: "D" }));
  expect(list).toHaveBeenCalledTimes(1);
  expect(settings).toHaveBeenCalledTimes(1);
  view.rerender(<BrowserIntegrationTab active />);
  expect(await screen.findByText("D 页面")).toBeTruthy();
  expect(list).toHaveBeenLastCalledWith("D");
  expect(settings).toHaveBeenCalledTimes(2);
});

it("loads global download settings when no conversation is open", async () => {
  useAppStore.setState({ conversationId: undefined });
  render(<BrowserIntegrationTab active />);
  fireEvent.click(screen.getByText("高级"));
  await waitFor(() => expect(screen.getByLabelText("浏览器下载策略")).toHaveProperty("value", "ask"));
  expect(list).not.toHaveBeenCalled();
  expect(settings).toHaveBeenCalledWith("");
});

const historyPreferences = { urlOpenTarget: "external-browser", localUrlOpenTarget: "in-app-browser", showFullUrl: false, annotationScreenshotsMode: "always" };
const visit = (id: string, title: string) => ({ id, title, url: `https://${id}.example/`, visitTime: new Date(2026, 9, 13, 12).getTime(), source: "other" });

it("refetches a visited history pane on activation and rejects reads from a retired owner or pending switch", async () => {
  preferenceState.current = historyPreferences;
  historySearch.mockResolvedValueOnce([visit("one", "Initial page")]);
  const view = render(<BrowserIntegrationTab />);
  fireEvent.click(screen.getByRole("button", { name: "管理浏览历史" }));
  expect(await screen.findByText("Initial page")).toBeTruthy();
  view.rerender(<BrowserIntegrationTab active={false} />);
  act(() => useAppStore.setState({ conversationId: "B" }));
  expect(historySearch).toHaveBeenCalledTimes(1);
  let finishOld!: (visits: unknown[]) => void;
  historySearch.mockReturnValueOnce(new Promise((resolve) => { finishOld = resolve; }));
  view.rerender(<BrowserIntegrationTab active />);
  act(() => useAppStore.setState({ pendingConversationSwitchId: "C" }));
  await act(async () => finishOld([visit("late", "Retired owner response")]));
  expect(screen.queryByText("Retired owner response")).toBeNull();
  historySearch.mockResolvedValueOnce([visit("new", "Real reopened visit")]);
  act(() => useAppStore.setState({ conversationId: "C", pendingConversationSwitchId: null }));
  expect(await screen.findByText("Real reopened visit")).toBeTruthy();
  expect(historySearch).toHaveBeenCalledTimes(3);
});

it("reads canonical native history after removal ACK instead of filtering old renderer rows", async () => {
  preferenceState.current = historyPreferences;
  historySearch.mockResolvedValueOnce([visit("one", "Selected page"), visit("other", "Old renderer title")]);
  let finishRemoval!: (result: boolean) => void;
  historyRemove.mockReturnValueOnce(new Promise((resolve) => { finishRemoval = resolve; }));
  render(<BrowserIntegrationTab />);
  fireEvent.click(screen.getByRole("button", { name: "管理浏览历史" }));
  await screen.findByText("Selected page");
  fireEvent.click(screen.getByRole("checkbox", { name: "选择 Selected page" }));
  fireEvent.click(screen.getByRole("button", { name: "移除所选" }));
  expect(historyRemove).toHaveBeenCalledWith(["one"]);
  expect(screen.getByRole("checkbox", { name: "选择 Selected page" })).toHaveProperty("disabled", true);
  expect(historySearch).toHaveBeenCalledTimes(1);
  historySearch.mockResolvedValueOnce([visit("other", "Canonical native title")]);
  await act(async () => finishRemoval(true));
  expect(await screen.findByText("Canonical native title")).toBeTruthy();
  expect(screen.queryByText("Old renderer title")).toBeNull();
  expect(screen.queryByText("Selected page")).toBeNull();
  expect(historySearch).toHaveBeenCalledTimes(2);
});
