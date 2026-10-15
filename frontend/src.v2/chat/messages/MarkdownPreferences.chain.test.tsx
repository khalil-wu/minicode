/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { useAppStore } from "../../stores";
import { __resetOpenWebInBrowserForTests, subscribeBrowserRequests } from "../openWebInBrowser";

const mocks = vi.hoisted(() => ({
  openPreferredPath: vi.fn(), openExternal: vi.fn(), openEditor: vi.fn(), preview: vi.fn(),
  preferences: { urlOpenTarget: "external-browser", localUrlOpenTarget: "in-app-browser" },
}));
vi.mock("../../desktop/runtime", () => ({
  isDesktop: () => true,
  desktop: () => ({ openPreferredPath: mocks.openPreferredPath }),
  getDesktopPreferences: async () => mocks.preferences,
  openExternal: mocks.openExternal, openPath: vi.fn(), revealPath: vi.fn(),
}));
vi.mock("../openAttachmentPreview", () => ({
  openWorkspaceFilePreview: mocks.preview, openArtifactPreview: vi.fn(), openLocalFilePreview: vi.fn(),
}));
vi.mock("../../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "preferences-owner", send: vi.fn() }) }));

beforeEach(() => {
  vi.clearAllMocks();
  __resetOpenWebInBrowserForTests();
  mocks.preferences.urlOpenTarget = "external-browser";
  mocks.preferences.localUrlOpenTarget = "in-app-browser";
  useAppStore.setState({ conversationId: "owner", workingDirectory: "C:/owner", rightStackTab: "tasks", openEditorFile: mocks.openEditor });
});
afterEach(() => { cleanup(); __resetOpenWebInBrowserForTests(); });

it("honors the native preferred file target without opening an internal editor", async () => {
  mocks.openPreferredPath.mockResolvedValue({ openInEditor: false, path: "C:/owner/src/app.ts" });
  render(<MarkdownRenderer content="[file](src/app.ts:7)" workspaceRoot="C:/owner" conversationId="owner" />);
  fireEvent.click(screen.getByRole("button", { name: "file" }));
  await waitFor(() => expect(mocks.openPreferredPath).toHaveBeenCalledWith("C:/owner/src/app.ts", 7, { workspaceRoot: "C:/owner" }));
  expect(mocks.openEditor).not.toHaveBeenCalled();
  expect(mocks.preview).not.toHaveBeenCalled();
});

it("keeps the file's original owner when the reader changes workspace while native preference resolution is pending", async () => {
  let release!: (result: { openInEditor: boolean; path: string }) => void;
  mocks.openPreferredPath.mockImplementation(() => new Promise(resolve => { release = resolve; }));
  render(<MarkdownRenderer content="[file](src/app.ts:7)" workspaceRoot="C:/owner" conversationId="owner" />);
  fireEvent.click(screen.getByRole("button", { name: "file" }));
  await waitFor(() => expect(mocks.openPreferredPath).toHaveBeenCalled());
  act(() => useAppStore.setState({ conversationId: "other", workingDirectory: "C:/other" }));
  await act(async () => release({ openInEditor: true, path: "C:/owner/src/app.ts" }));
  expect(mocks.preview).toHaveBeenCalledWith(expect.objectContaining({ path: "src/app.ts", workspaceRoot: "C:/owner", conversationId: "owner" }));
  expect(mocks.openEditor).not.toHaveBeenCalled();
});

it("applies distinct external and local URL preferences through the actual generic Markdown click path", async () => {
  const requested: string[] = [];
  const unsubscribe = subscribeBrowserRequests(request => requested.push(request.url));
  render(<MarkdownRenderer content="[remote](https://example.com/docs) [local](http://127.0.0.1:12345/)" />);
  fireEvent.click(screen.getByRole("link", { name: "remote" }));
  await waitFor(() => expect(mocks.openExternal).toHaveBeenCalledWith("https://example.com/docs"));
  expect(requested).toEqual([]);
  fireEvent.click(screen.getByRole("link", { name: "local" }));
  await waitFor(() => expect(requested).toEqual(["http://127.0.0.1:12345/"]));
  expect(useAppStore.getState().rightStackTab).toBe("browser");
  unsubscribe();
});
