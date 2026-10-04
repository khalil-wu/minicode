/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { BrowserIntegrationTab } from "./BrowserIntegrationTab";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
const { list, settings, update } = vi.hoisted(() => ({ list: vi.fn(), settings: vi.fn(), update: vi.fn() }));
vi.mock("../desktop/runtime", () => ({ desktop: () => null, isDesktop: () => true,
  embeddedBrowserList: list, embeddedBrowserGetSettings: settings, embeddedBrowserSetSettings: update }));
beforeEach(() => {
  vi.clearAllMocks();
  list.mockImplementation(async (owner: string) => [{ id: `tab-${owner}`, title: `${owner} 页面`, active: true }]);
  settings.mockResolvedValue({ downloadPolicy: "ask", origin: "", permissions: [] });
  useAppStore.setState({ conversationId: "A" });
});
afterEach(cleanup);

it("clears a previous owner's visible tabs while loading and refuses an old late tab response", async () => {
  let finishOld!: (tabs: unknown[]) => void;
  let finishNew!: (tabs: unknown[]) => void;
  list.mockReturnValueOnce(new Promise((resolve) => { finishOld = resolve; }))
    .mockReturnValueOnce(new Promise((resolve) => { finishNew = resolve; }));
  render(<BrowserIntegrationTab />);
  act(() => useAppStore.setState({ conversationId: "B" }));
  await act(async () => finishOld([{ id: "old", title: "Late A", active: true }]));
  expect(screen.queryByText("Late A")).toBeNull();
  await act(async () => finishNew([{ id: "new", title: "Current B", active: true }]));
  expect(screen.getByText("Current B")).toBeTruthy();
});

it("does not overwrite a saved global download policy with an older settings read", async () => {
  let finishSettings!: (settings: unknown) => void;
  render(<BrowserIntegrationTab />);
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
  await waitFor(() => expect(screen.getByLabelText("浏览器下载策略")).toHaveProperty("value", "ask"));
  expect(list).not.toHaveBeenCalled();
  expect(settings).toHaveBeenCalledWith("");
});
