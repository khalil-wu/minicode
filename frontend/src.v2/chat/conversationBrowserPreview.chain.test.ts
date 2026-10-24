/* @vitest-environment jsdom */
import { createElement } from "react";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { openConversationBrowserFilePreview } from "./openAttachmentPreview";
import { FileViewerPanel } from "../panels/FileViewerPanel";
const { transport, fetchMock } = vi.hoisted(() => ({ transport: { sessionId: "browser-session" }, fetchMock: vi.fn() }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => transport }));
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: null }) }));
vi.mock("../desktop/runtime", async load => ({ ...await load<typeof import("../desktop/runtime")>(), desktop: () => null, runtime: () => null }));
vi.mock("../shell/tabs/ConversationFilesTree", () => ({ ConversationFilesTree: () => null }));
vi.mock("../lib/monaco-colorize", () => ({ guessLanguageFromPath: () => "plaintext", useColorizedLines: () => null }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true) }));
const target = { conversationId: "owner", executionCwd: "C:/base/date/task", path: "date/task/outputs/原始.txt", name: "原始.txt" };
const preview = { file_name: target.name, path: target.path, media_type: "text/plain", kind: "document", size_bytes: 18,
  summary: "", parse_error: "", content: "原始文本\r\n", content_chars: 6, truncated: false, has_native: false };
const jsonResponse = (value: unknown) => new Response(JSON.stringify(value), { headers: { "content-type": "application/json" } });
const previewRequests = () => fetchMock.mock.calls.filter(call => new URL(String(call[0])).pathname.endsWith("browser-preview"));
const rawRequests = () => fetchMock.mock.calls.filter(call => new URL(String(call[0])).pathname.endsWith("browser-raw"));
beforeEach(() => {
  vi.clearAllMocks(); transport.sessionId = "browser-session";
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL = vi.fn(() => "blob:captured-browser-file");
    static revokeObjectURL = vi.fn();
  });
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (input: URL | string) => {
    const url = new URL(String(input));
    if (url.pathname.endsWith("browser-raw")) return new Response(new Blob(["image bytes"], { type: "image/png" }));
    if (url.pathname.endsWith("git/branches")) return jsonResponse({ is_git_repo: false, branches: [], commits: [] });
    return jsonResponse(url.searchParams.get("conversation_id") === "next" ? { ...preview, content: "Current task file" } : preview);
  });
  useAppStore.setState({ conversationId: "owner", conversations: [{ id: "owner", title: "Owner", updatedAt: "", executionCwd: target.executionCwd, projectlessBrowserRoot: "C:/base" }],
    workspaceFileTabs: [], fileBrowserRoots: {}, pendingWorkspaceFileTabId: null, pendingConversationSwitchId: null, previewArtifact: null,
    workingDirectory: target.executionCwd, isConnected: true, panelSlots: [{ id: "chat", kind: "chat", focused: true }], rightPanelOpen: false,
    editorTabs: [{ id: "unsaved", path: "C:/editor.ts", content: "unsaved editor text", original: "saved", loading: false }],
    draft: "Keep original draft", attachments: [{ id: "original", name: "original.png", type: "image/png", size: 10, status: "ready" }] });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("reads a browser-owned file through its mounted FileViewer without promoting the browser base to execution or editor root", async () => {
  expect(openConversationBrowserFilePreview(target)).toBe(true);
  expect(fetchMock).not.toHaveBeenCalled();
  const tab = useAppStore.getState().workspaceFileTabs[0];
  expect(tab).toMatchObject({ path: target.path, title: target.name, owner: { scopeId: "conversation:owner", conversationId: "owner", executionCwd: target.executionCwd, browserRoot: "C:/base" } });
  render(createElement(FileViewerPanel, { tabId: tab.id }));
  expect(await screen.findByText("原始文本")).toBeTruthy();
  const url = new URL(String(previewRequests()[0][0]));
  expect(Object.fromEntries(url.searchParams)).toMatchObject({ session_id: "browser-session", conversation_id: "owner", execution_cwd: target.executionCwd, path: target.path });
  expect(useAppStore.getState()).toMatchObject({ workingDirectory: target.executionCwd, draft: "Keep original draft", previewArtifact: null, attachments: [{ id: "original" }] });
  expect(useAppStore.getState().editorTabs[0].content).toBe("unsaved editor text");
});

it("retains native image ownership and reads raw bytes with the new session after reconnect", async () => {
  fetchMock.mockImplementation(async (input: URL | string) => new URL(String(input)).pathname.endsWith("browser-raw")
    ? new Response(new Blob(["image bytes"], { type: "image/png" }))
    : jsonResponse(new URL(String(input)).pathname.endsWith("git/branches") ? { is_git_repo: false, branches: [], commits: [] }
      : { ...preview, media_type: "image/png", has_native: true }));
  openConversationBrowserFilePreview(target);
  const tab = useAppStore.getState().workspaceFileTabs[0];
  render(createElement(FileViewerPanel, { tabId: tab.id }));
  expect(await screen.findByRole("img", { name: target.name })).toBeTruthy();
  expect(new URL(String(rawRequests()[0][0])).searchParams.get("session_id")).toBe("browser-session");
  act(() => useAppStore.setState({ isConnected: false }));
  transport.sessionId = "restored-browser-session";
  act(() => useAppStore.setState({ isConnected: true }));
  await waitFor(() => expect(rawRequests()).toHaveLength(2));
  const url = new URL(String(rawRequests()[1][0]));
  expect(Object.fromEntries(url.searchParams)).toMatchObject({ session_id: transport.sessionId, conversation_id: "owner", execution_cwd: target.executionCwd, path: target.path });
  expect(await screen.findByRole("img", { name: target.name })).toBeTruthy();
});

it("aborts a retired FileViewer read and keeps its late bytes out of the newly selected owner", async () => {
  let finish!: (value: Response) => void;
  fetchMock.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  openConversationBrowserFilePreview(target);
  const tab = useAppStore.getState().workspaceFileTabs[0];
  const view = render(createElement(FileViewerPanel, { tabId: tab.id }));
  const signal = previewRequests()[0][1].signal as AbortSignal;
  view.rerender(createElement(FileViewerPanel, { tabId: tab.id, active: false }));
  act(() => useAppStore.setState(state => ({ conversationId: "next", workingDirectory: "C:/base/date/next", draft: "New task draft",
    conversations: [...state.conversations, { id: "next", title: "Next", updatedAt: "", executionCwd: "C:/base/date/next", projectlessBrowserRoot: "C:/base" }] })));
  let currentId!: string;
  act(() => { currentId = useAppStore.getState().openWorkspaceFileTab({ scopeId: "conversation:next", conversationId: "next", executionCwd: "C:/base/date/next", browserRoot: "C:/base" }, "date/next/current.txt"); });
  view.rerender(createElement(FileViewerPanel, { tabId: currentId }));
  expect(await screen.findByText("Current task file")).toBeTruthy();
  await act(async () => { finish(jsonResponse(preview)); });
  expect(signal.aborted).toBe(true);
  expect(screen.queryByText("原始文本")).toBeNull();
  expect(useAppStore.getState()).toMatchObject({ conversationId: "next", workingDirectory: "C:/base/date/next", draft: "New task draft", rightStackTab: `file:${currentId}` });
  expect(useAppStore.getState().editorTabs[0].content).toBe("unsaved editor text");
});
