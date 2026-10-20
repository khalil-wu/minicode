// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ tree: vi.fn(), preview: vi.fn() }));
vi.mock("../protocol/conversation-browser", () => ({ fetchConversationBrowserTree: mocks.tree, fetchConversationBrowserOriginal: vi.fn() }));
vi.mock("../chat/openAttachmentPreview", () => ({ openConversationBrowserFilePreview: mocks.preview }));
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: null }) }));
vi.mock("../desktop/runtime", async load => ({ ...await load<typeof import("../desktop/runtime")>(), desktop: () => null }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
import { useAppStore } from "../stores";
import { FilesPanel } from "./FilesPanel";

const page = (name: string, workspaceRoot = "D:/browse") => ({ workspace_root: workspaceRoot, requested_path: ".", entries: [{ name, path: name, is_dir: false, size_bytes: 1, modified_at: "", has_children: false }] });
beforeEach(() => {
  vi.clearAllMocks();
  useAppStore.setState({ conversationId: "A", conversations: [{ id: "A", title: "A", updatedAt: "", executionCwd: "C:/task/A", projectlessBrowserRoot: "D:/browse" }],
    workingDirectory: "C:/different-editor", pendingConversationSwitchId: null, isConnected: true, fileTreeVersion: 1,
    quickOpenVisible: false, draft: "keep my draft", attachments: [{ id: "keep", name: "keep.txt", type: "text/plain", size: 1, status: "ready" }] });
  mocks.tree.mockResolvedValue(page("note.txt"));
});
afterEach(cleanup);

it("reads the chat-owned Files tab and previews the selected file without opening Quick Open", async () => {
  render(<FilesPanel />);
  fireEvent.click(await screen.findByText("note.txt"));
  expect(mocks.tree).toHaveBeenCalledWith({ conversationId: "A", executionCwd: "C:/task/A" }, ".", expect.any(AbortSignal));
  expect(mocks.preview).toHaveBeenCalledWith({ conversationId: "A", executionCwd: "C:/task/A", path: "note.txt", name: "note.txt" });
  expect(useAppStore.getState()).toMatchObject({ quickOpenVisible: false, workingDirectory: "C:/different-editor", draft: "keep my draft", attachments: [{ name: "keep.txt" }] });
  expect(screen.queryByRole("button")).toBeNull();
});

it("reuses file watcher revisions and suspends hidden Files tabs", async () => {
  const view = render(<FilesPanel active={false} />);
  expect(mocks.tree).not.toHaveBeenCalled();
  view.rerender(<FilesPanel active />);
  await screen.findByText("note.txt");
  act(() => useAppStore.setState({ fileTreeVersion: 2 }));
  await waitFor(() => expect(mocks.tree).toHaveBeenCalledTimes(2));
  view.rerender(<FilesPanel active={false} />);
  act(() => useAppStore.setState({ fileTreeVersion: 3 }));
  expect(mocks.tree).toHaveBeenCalledTimes(2);
});

it("does not publish a late file tree from the previous conversation", async () => {
  let finish!: (value: ReturnType<typeof page>) => void;
  mocks.tree.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  render(<FilesPanel />);
  const signal = mocks.tree.mock.calls[0][2] as AbortSignal;
  act(() => useAppStore.setState({ conversationId: "B", conversations: [{ id: "B", title: "B", updatedAt: "", workspaceRoot: "C:/project/B" }] }));
  await screen.findByText("note.txt");
  await act(async () => finish(page("stale-A.txt")));
  expect(signal.aborted).toBe(true);
  expect(screen.queryByText("stale-A.txt")).toBeNull();
  expect(mocks.tree).toHaveBeenLastCalledWith({ conversationId: "B", executionCwd: "C:/project/B" }, ".", expect.any(AbortSignal));
});

it("shows unavailable Files without borrowing an unrelated editor workspace", () => {
  useAppStore.setState({ conversationId: null, conversations: [] });
  render(<FilesPanel />);
  expect(screen.getByText("文件不可用")).toBeTruthy();
  expect(mocks.tree).not.toHaveBeenCalled();
});
