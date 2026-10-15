/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { openWorkspaceFilePreview } from "../chat/openAttachmentPreview";
import { resetPreviewRequestScopesForTests } from "../chat/previewRequestScope";
import { PreviewPanel } from "./PreviewPanel";
import { sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { emptyConversationWorkbenchState } from "../stores/ui-slice";

const { preview } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  return { preview: vi.fn() };
});
vi.mock("../protocol/workspace", () => ({ fetchWorkspaceFilePreview: preview }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "preview-origin" }) }));
vi.mock("../protocol/api", () => ({ artifactRawResourceUrlWithToken: vi.fn(), attachmentRawResourceUrlWithToken: vi.fn(), workspaceRawResourceUrlWithToken: vi.fn(), fetchAttachmentPreview: vi.fn(), fetchArtifactOriginal: vi.fn(), fetchAttachmentOriginal: vi.fn() }));
vi.mock("../protocol/ws-outbox", () => ({ createClientCommandId: () => "origin-request", sendClientCommand: vi.fn(), sendClientCommandAwaitResult: vi.fn(), commandResultSucceeded: () => true }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

beforeEach(() => {
  resetPreviewRequestScopesForTests();
  preview.mockReset();
  vi.mocked(sendClientCommandAwaitResult).mockReset();
  useAppStore.setState({ conversationId: "workspace-owner", workingDirectory: "C:/owner", conversationWorkbenchStates: {}, previewArtifact: null,
    previewOwnerConversationId: null, panelSlots: [], editorOpenRequests: [], draft: "未发送的主草稿", isConnected: true });
});
afterEach(() => { cleanup(); resetPreviewRequestScopesForTests(); });

describe("workspace file origin reaches document navigation", () => {
  it("keeps a foreign preview's actual root and does not navigate after a late activation for another scope", async () => {
    useAppStore.setState({ conversationId: "active-other", workingDirectory: "C:/active-other", previewOwnerConversationId: "workspace-owner", conversationWorkbenchStates: {
      "workspace-owner": { ...emptyConversationWorkbenchState(), previewArtifact: { artifactId: "opaque-document", source: "workspace", sourcePath: "docs/design.md", workspaceRoot: "C:/owner", name: "design.md", mediaType: "text/markdown", content: "# 文档", loadedAt: 1 } },
    } });
    vi.mocked(sendClientCommandAwaitResult).mockResolvedValueOnce({ command: "workspace.set", level: "success", message: "activated", data: { conversation_id: "workspace-owner", workspace_root: "C:/owner" } });
    render(<PreviewPanel />);
    fireEvent.click(screen.getByRole("button", { name: "在来源工作区打开" }));
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({ type: "workspace.set", path: "C:/owner" }), "workspace.set"));
    expect(useAppStore.getState().editorOpenRequests).toEqual([]);
    expect(useAppStore.getState().workingDirectory).toBe("C:/active-other");
    expect(useAppStore.getState().draft).toBe("未发送的主草稿");
  });
  it("carries the real HTTP preview origin into the toolbar without changing an existing unsaved buffer", async () => {
    const dirtyTab = { id: "dirty", path: "docs/design.md", label: "design.md", content: "用户尚未保存的正文", original: "旧正文", loading: false };
    useAppStore.setState({ editorTabs: [dirtyTab], activeTabPath: dirtyTab.path });
    preview.mockResolvedValue({ file_name: "design.md", path: "docs/design.md", content: "# 已保存的预览正文", media_type: "text/markdown", kind: "document", has_native: false, size_bytes: 40, content_chars: 20, truncated: false, parse_error: "", summary: "" });
    expect(openWorkspaceFilePreview({ path: "C:/owner/docs/design.md", workspaceRoot: "C:/owner", conversationId: "workspace-owner" })).toBe(true);
    expect(useAppStore.getState().previewArtifact).toMatchObject({ source: "workspace", sourcePath: "C:/owner/docs/design.md", workspaceRoot: "C:/owner", loading: true });
    await waitFor(() => expect(useAppStore.getState().previewArtifact).toMatchObject({ sourcePath: "docs/design.md", workspaceRoot: "C:/owner", loading: false }));
    // Opening follows explicit source metadata even with an opaque artifact id.
    useAppStore.getState().setPreviewArtifact({ ...useAppStore.getState().previewArtifact!, artifactId: "opaque-origin-id" });
    render(<PreviewPanel />);
    fireEvent.click(screen.getByRole("button", { name: "打开文件" }));
    expect(useAppStore.getState().editorOpenRequests.at(-1)?.path).toBe(dirtyTab.path);
    expect(useAppStore.getState().editorTabs).toEqual([dirtyTab]);
    expect(useAppStore.getState().draft).toBe("未发送的主草稿");
    expect(preview).toHaveBeenCalledWith("C:/owner/docs/design.md", "C:/owner", expect.any(AbortSignal));
  });

  it.each(["attachment", "artifact", "local"] as const)("does not invent a disk-open action for a %s document", (source) => {
    useAppStore.getState().setPreviewArtifact({ artifactId: "workspace:C:/unrelated.md", content: "# 文档", name: "document.md", mediaType: "text/markdown", source, loadedAt: 1 });
    render(<PreviewPanel />);
    expect(screen.getByRole("button", { name: "查看源代码" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "打开文件" })).toBeNull();
    expect(screen.queryByRole("button", { name: "在来源工作区打开" })).toBeNull();
    expect(useAppStore.getState().editorOpenRequests).toEqual([]);
  });
});
