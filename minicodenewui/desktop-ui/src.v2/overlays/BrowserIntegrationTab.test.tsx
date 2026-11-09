/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { BrowserIntegrationTab } from "./BrowserIntegrationTab";
import { BrowserPageActions } from "../panels/BrowserPageActions";
import type { EmbeddedBrowserSettingsSnapshot } from "../desktop/runtime";
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: preferenceState.current, error: "", update: vi.fn() }) }));

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
const { list, settings, update, historySearch, historyRemove, preferenceState, settingsListeners, clearData, downloadList, downloadAction, downloadListeners, importProfiles, importBrowserProfile } = vi.hoisted(() => ({ clearData: vi.fn(async () => true), list: vi.fn(), settings: vi.fn(), update: vi.fn(), historySearch: vi.fn(), historyRemove: vi.fn(), preferenceState: { current: null as Record<string, unknown> | null }, settingsListeners: new Set<(settings: EmbeddedBrowserSettingsSnapshot) => void>(), downloadList: vi.fn(), downloadAction: vi.fn(), downloadListeners: new Set<(records: unknown[]) => void>(), importProfiles: vi.fn(), importBrowserProfile: vi.fn() }));
const personalData = vi.hoisted(() => ({ listPasswords: vi.fn(async () => ({ available: true, passwordAutofillEnabled: true, contactAutofillEnabled: true, items: [] })), listContacts: vi.fn(async () => []) }));
vi.mock("../desktop/runtime", () => ({ desktop: () => ({ browserPersonalData: personalData, browserProfileImport: { listImportableBrowserProfiles: importProfiles, importBrowserProfile }, embeddedBrowser: { pageCommand: vi.fn(async () => ({ zoomFactor: 1 })), downloads: downloadList, downloadAction, onDownloadsChanged: (listener: (records: unknown[]) => void) => { downloadListeners.add(listener); return () => downloadListeners.delete(listener); } } }), isDesktop: () => true,
  embeddedBrowserList: list, embeddedBrowserGetSettings: settings, embeddedBrowserSetSettings: update, browserHistorySearch: historySearch, browserHistoryRemove: historyRemove,
  onEmbeddedBrowserEvent: () => () => {},
  onEmbeddedBrowserSettingsChanged: (listener: (settings: EmbeddedBrowserSettingsSnapshot) => void) => { settingsListeners.add(listener); return () => { settingsListeners.delete(listener); }; } }));
vi.mock("../lib/browser-data", () => ({ clearBrowserData: clearData }));
beforeEach(() => {
  vi.clearAllMocks();
  settingsListeners.clear();
  downloadListeners.clear();
  downloadList.mockResolvedValue([]);
  downloadAction.mockResolvedValue("");
  importProfiles.mockResolvedValue([]);
  importBrowserProfile.mockResolvedValue({ history: { imported: 2 } });
  preferenceState.current = null;
  historySearch.mockResolvedValue([]);
  historyRemove.mockResolvedValue(true);
  list.mockImplementation(async (owner: string) => [{ id: `tab-${owner}`, title: `${owner} 页面`, active: true }]);
  settings.mockResolvedValue({ browserEnabled: true, deviceToolbarAlwaysOnNewTabs: false, downloadPolicy: "allow", origin: "", permissions: [] });
  update.mockResolvedValue({ browserEnabled: false, deviceToolbarAlwaysOnNewTabs: false, downloadPolicy: "allow", origin: "", permissions: [] });
  useAppStore.setState({ conversationId: "A", pendingConversationSwitchId: null, browserSettingsSection: "general" });
});
afterEach(cleanup);

const importableProfile = { source: "chrome", appName: "Google Chrome", profileName: "Personal", profileDirectoryName: "Default", profilePath: "C:/Chrome/Default", rootPath: "C:/Chrome", hasHistory: true };

it("discovers profiles only when opening the observed import dialog and imports only the selected history", async () => {
  importProfiles.mockResolvedValue([importableProfile]);
  render(<BrowserIntegrationTab />);
  expect(importProfiles).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "导入…" }));
  await screen.findByRole("dialog", { name: "从浏览器导入" });
  const historyToggle = await screen.findByRole("switch", { name: "浏览记录" });
  expect(screen.getByText("导入前，请完全关闭 Google Chrome")).toBeTruthy();
  expect(screen.queryByRole("switch", { name: "已保存的密码" })).toBeNull();
  expect(screen.queryByRole("switch", { name: "Cookie" })).toBeNull();
  fireEvent.click(historyToggle);
  expect(screen.getByRole("button", { name: "导入", exact: true })).toHaveProperty("disabled", true);
  fireEvent.click(historyToggle);
  fireEvent.click(screen.getByRole("button", { name: "导入", exact: true }));
  await waitFor(() => expect(importBrowserProfile).toHaveBeenCalledWith({ source: "chrome", profilePath: importableProfile.profilePath,
    importHistory: true, importCookies: false, importPasswords: false, importExtensions: false }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});

it("does not invent an importable profile and retains real import errors", async () => {
  const view = render(<BrowserIntegrationTab />);
  fireEvent.click(screen.getByRole("button", { name: "导入…" }));
  await screen.findByText("未找到可导入的浏览器个人资料");
  expect(screen.getByRole("button", { name: "导入", exact: true })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "取消", exact: true }));
  view.unmount();
  importProfiles.mockResolvedValue([importableProfile]);
  importBrowserProfile.mockRejectedValueOnce(new Error("Chrome history snapshot is busy"));
  render(<BrowserIntegrationTab />);
  fireEvent.click(screen.getByRole("button", { name: "导入…" }));
  await screen.findByRole("switch", { name: "浏览记录" });
  fireEvent.click(screen.getByRole("button", { name: "导入", exact: true }));
  expect((await screen.findByRole("alert")).textContent).toContain("Chrome history snapshot is busy");
  expect(screen.getByRole("dialog", { name: "从浏览器导入" })).toBeTruthy();
});

it("renders the committed download directory and saves the native download prompt choice", async () => {
  settings.mockResolvedValue({ browserEnabled: true, downloadDirectory: "C:\\Downloads\\Work", downloadPolicy: "allow", origin: "", permissions: [] });
  update.mockResolvedValue({ browserEnabled: true, downloadDirectory: "C:\\Downloads\\Work", downloadPolicy: "ask", origin: "", permissions: [] });
  render(<BrowserIntegrationTab />);
  await screen.findByText("C:\\Downloads\\Work");
  fireEvent.click(screen.getByRole("switch", { name: "询问下载文件的保存位置" }));
  await waitFor(() => expect(update).toHaveBeenCalledWith({ downloadPolicy: "ask" }));
  await waitFor(() => expect(screen.getByRole("switch", { name: "询问下载文件的保存位置" }).getAttribute("aria-checked")).toBe("true"));
  act(() => { for (const listener of settingsListeners) listener({ browserEnabled: true, deviceToolbarAlwaysOnNewTabs: false, downloadDirectory: "D:\\Files", downloadPolicy: "allow", sitePermissions: {} }); });
  expect(screen.getByText("D:\\Files")).toBeTruthy();
  expect(screen.getByRole("switch", { name: "询问下载文件的保存位置" }).getAttribute("aria-checked")).toBe("false");
});

it("uses browser control and the observed settings rows without the old download-policy panel", async () => {
  render(<BrowserIntegrationTab />);
  const control = await screen.findByRole("switch", { name: "让 MiniCode 控制内置浏览器" });
  expect(control.getAttribute("aria-checked")).toBe("true");
  expect(screen.queryByText("高级")).toBeNull();
  expect(screen.queryByLabelText("浏览器下载策略")).toBeNull();
  fireEvent.click(control);
  await waitFor(() => expect(update).toHaveBeenCalledWith({ browserEnabled: false }));
});

it("reflects a committed setting from another window without losing it to an older read", async () => {
  let finishSettings!: (settings: unknown) => void;
  settings.mockReturnValueOnce(new Promise(resolve => { finishSettings = resolve; }));
  render(<BrowserIntegrationTab />);
  act(() => { for (const listener of settingsListeners) listener({ browserEnabled: false, deviceToolbarAlwaysOnNewTabs: true, downloadPolicy: "allow", sitePermissions: {} }); });
  expect(screen.getByRole("switch", { name: "让 MiniCode 控制内置浏览器" }).getAttribute("aria-checked")).toBe("false");
  await act(async () => finishSettings({ browserEnabled: true, downloadPolicy: "block", origin: "", permissions: [] }));
  expect(screen.getByRole("switch", { name: "让 MiniCode 控制内置浏览器" }).getAttribute("aria-checked")).toBe("false");
});

it("keeps a newer committed window setting when its own delayed write settles", async () => {
  let settle!: (settings: unknown) => void;
  update.mockReturnValueOnce(new Promise(resolve => { settle = resolve; }));
  render(<BrowserIntegrationTab />);
  fireEvent.click(await screen.findByRole("switch", { name: "让 MiniCode 控制内置浏览器" }));
  act(() => { for (const listener of settingsListeners) listener({ browserEnabled: true, deviceToolbarAlwaysOnNewTabs: false, downloadPolicy: "allow", sitePermissions: {} }); });
  await act(async () => settle({ browserEnabled: false, downloadPolicy: "allow", origin: "", permissions: [] }));
  expect(screen.getByRole("switch", { name: "让 MiniCode 控制内置浏览器" }).getAttribute("aria-checked")).toBe("true");
});

it("retains the committed browser control state when persistence fails and shows the actual error", async () => {
  update.mockRejectedValueOnce(new Error("settings disk failure"));
  render(<BrowserIntegrationTab />);
  fireEvent.click(await screen.findByRole("switch", { name: "让 MiniCode 控制内置浏览器" }));
  await screen.findByRole("alert");
  expect(screen.getByRole("alert").textContent).toContain("settings disk failure");
  expect(screen.getByRole("switch", { name: "让 MiniCode 控制内置浏览器" }).getAttribute("aria-checked")).toBe("true");
});

it("does not perform browser IPC while hidden and loads global settings on activation without a conversation", async () => {
  useAppStore.setState({ conversationId: null });
  const view = render(<BrowserIntegrationTab active={false} />);
  expect(settings).not.toHaveBeenCalled();
  view.rerender(<BrowserIntegrationTab active />);
  await screen.findByRole("switch", { name: "让 MiniCode 控制内置浏览器" });
  expect(settings).toHaveBeenCalledWith("");
  expect(list).not.toHaveBeenCalled();
});

it("offers the observed single browsing-data action and uses its existing confirmation chain", async () => {
  render(<BrowserIntegrationTab />);
  expect(screen.queryByRole("button", { name: "删除 Cookie" })).toBeNull();
  expect(screen.queryByRole("button", { name: "显示单项浏览数据选项" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "清除浏览数据" }));
  await waitFor(() => expect(clearData).toHaveBeenCalledWith());
  expect(screen.queryByRole("button", { name: "删除下载记录" })).toBeNull();
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

it("keeps day cards separate and refetches the canonical history after clearing", async () => {
  historySearch.mockResolvedValueOnce([visit("first", "Before clear"), { ...visit("older", "Older page"), visitTime: new Date(2026, 9, 12, 12).getTime() }]);
  render(<BrowserIntegrationTab />);
  fireEvent.click(screen.getByRole("button", { name: "管理浏览历史" }));
  await screen.findByText("Before clear");
  expect(screen.getByText("Before clear").closest(".settings-card")).not.toBe(screen.getByText("Older page").closest(".settings-card"));
  historySearch.mockResolvedValueOnce([visit("fresh", "Canonical concurrent visit")]);
  fireEvent.click(screen.getByRole("button", { name: "清除浏览数据" }));
  expect(await screen.findByText("Canonical concurrent visit")).toBeTruthy();
  expect(screen.queryByText("Before clear")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "返回浏览器设置" }));
  expect(screen.getByRole("button", { name: "管理浏览历史" })).toBeTruthy();
});

const download = (id: string, state = "completed") => ({ id, filename: `${id}.txt`, url: `https://download.example/${id}`, path: `C:/Downloads/${id}.txt`, state, receivedBytes: 5, totalBytes: 10, startedAt: new Date(2026, 9, 19, 12).getTime(), fileExists: true,
  canPause: state === "progressing", canResume: state === "paused", canCancel: state === "progressing" || state === "paused" });

it("uses the independent download-history page and native open, reveal and remove consumers", async () => {
  downloadList.mockResolvedValueOnce([download("report")]);
  render(<BrowserIntegrationTab />);
  fireEvent.click(screen.getByRole("button", { name: "管理下载记录" }));
  fireEvent.click(await screen.findByRole("button", { name: "report.txt" }));
  await waitFor(() => expect(downloadAction).toHaveBeenCalledWith({ action: "open", id: "report" }));
  fireEvent.click(screen.getByRole("button", { name: "显示下载文件 report.txt" }));
  await waitFor(() => expect(downloadAction).toHaveBeenCalledWith({ action: "reveal", id: "report" }));
  fireEvent.click(screen.getByRole("button", { name: "移除下载记录 report.txt" }));
  await waitFor(() => expect(downloadAction).toHaveBeenCalledWith({ action: "remove", id: "report" }));
  act(() => { for (const listener of downloadListeners) listener([]); });
  expect(screen.queryByRole("button", { name: "report.txt" })).toBeNull();
  expect(screen.getByRole("textbox", { name: "搜索下载记录" })).toBeTruthy();
});

it("clears only terminal download records and retains a newer native event over an old initial read", async () => {
  let finishRead!: (records: unknown[]) => void;
  downloadList.mockReturnValueOnce(new Promise(resolve => { finishRead = resolve; }));
  render(<BrowserIntegrationTab />);
  fireEvent.click(screen.getByRole("button", { name: "管理下载记录" }));
  act(() => { for (const listener of downloadListeners) listener([download("finished"), download("active", "progressing")]); });
  await act(async () => finishRead([download("stale")]));
  expect(screen.queryByRole("button", { name: "stale.txt" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "清除全部" }));
  await waitFor(() => expect(downloadAction).toHaveBeenCalledTimes(1));
  expect(downloadAction).toHaveBeenCalledWith({ action: "remove", id: "finished" });
  expect(screen.queryByRole("button", { name: "移除下载记录 active.txt" })).toBeNull();
  expect(screen.getByRole("button", { name: "取消下载" })).toBeTruthy();
});

it("opens the real global download-history consumer from More while preserving the conversation draft", async () => {
  useAppStore.setState({ settingsOpen: false, draft: "keep unsent text" });
  render(<><BrowserIntegrationTab /><BrowserPageActions conversationId="A" tabId="page" url="https://example.com/" position={{ x: 10, y: 10 }} onClose={vi.fn()} viewportEnabled={false} onViewportChange={vi.fn()} /></>);
  fireEvent.click(screen.getByRole("menuitem", { name: "下载", exact: true }));
  await screen.findByRole("textbox", { name: "搜索下载记录" });
  expect(downloadList).toHaveBeenCalledOnce();
  expect(useAppStore.getState()).toMatchObject({ browserSettingsSection: "downloads", settingsOpen: true, settingsTab: "browser", draft: "keep unsent text" });
  fireEvent.click(screen.getByRole("button", { name: "返回浏览器设置" }));
  expect(useAppStore.getState().browserSettingsSection).toBe("general");
  expect(screen.getByRole("button", { name: "管理下载记录" })).toBeTruthy();
});

it.each([["密码管理器", "passwords"], ["联系信息", "contacts"]] as const)("routes the nested %s menu to its existing native data consumer", async (title, section) => {
  useAppStore.setState({ browserSettingsSection: "downloads", settingsOpen: false, draft: "keep unsent text" });
  render(<><BrowserIntegrationTab /><BrowserPageActions conversationId="A" tabId="page" url="https://example.com/" position={{ x: 10, y: 10 }} onClose={vi.fn()} viewportEnabled={false} onViewportChange={vi.fn()} /></>);
  fireEvent.mouseEnter(screen.getByRole("menuitem", { name: "密码和自动填充" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: title, exact: true }));
  await screen.findByRole("heading", { name: title, exact: true });
  await waitFor(() => expect(personalData.listPasswords).toHaveBeenCalledOnce());
  if (section === "contacts") await waitFor(() => expect(personalData.listContacts).toHaveBeenCalledOnce());
  expect(useAppStore.getState()).toMatchObject({ browserSettingsSection: section, settingsOpen: true, settingsTab: "browser", draft: "keep unsent text" });
  fireEvent.click(screen.getByRole("button", { name: "返回浏览器设置" }));
  expect(useAppStore.getState().browserSettingsSection).toBe("general");
});

it("uses native pause, resume and cancel receipts without inventing a download outcome", async () => {
  downloadList.mockResolvedValueOnce([download("live", "progressing")]);
  useAppStore.setState({ browserSettingsSection: "downloads" });
  render(<BrowserIntegrationTab />);
  fireEvent.click(await screen.findByRole("button", { name: "暂停下载" }));
  await waitFor(() => expect(downloadAction).toHaveBeenLastCalledWith({ action: "pause", id: "live" }));
  expect(screen.queryByText("已暂停")).toBeNull();
  act(() => downloadListeners.forEach(listener => listener([download("live", "paused")])));
  expect(screen.getByText("已暂停")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "继续下载" }));
  await waitFor(() => expect(downloadAction).toHaveBeenLastCalledWith({ action: "resume", id: "live" }));
  act(() => downloadListeners.forEach(listener => listener([download("live", "progressing")])));
  fireEvent.click(screen.getByRole("button", { name: "取消下载" }));
  await waitFor(() => expect(downloadAction).toHaveBeenLastCalledWith({ action: "cancel", id: "live" }));
  expect(screen.queryByRole("button", { name: "移除下载记录 live.txt" })).toBeNull();
  act(() => downloadListeners.forEach(listener => listener([download("live", "cancelled")])));
  expect(screen.getByText("已取消")).toBeTruthy();
  expect(screen.getByRole("button", { name: "移除下载记录 live.txt" })).toBeTruthy();
});

it("shows actual missing completed files as deleted and does not open a nonexistent path", async () => {
  downloadList.mockResolvedValueOnce([{ ...download("deleted"), fileExists: false }]);
  useAppStore.setState({ browserSettingsSection: "downloads" });
  render(<BrowserIntegrationTab />);
  expect(await screen.findByText("已删除")).toBeTruthy();
  expect(screen.getByRole("button", { name: /^deleted\.txt/ })).toHaveProperty("disabled", true);
  expect(screen.queryByRole("button", { name: "显示下载文件 deleted.txt" })).toBeNull();
  expect(downloadAction).not.toHaveBeenCalled();
});
