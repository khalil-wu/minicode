/* @vitest-environment jsdom */
import { beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { openConversationBrowserFilePreview } from "./openAttachmentPreview";
import { releasePreviewScope, resetPreviewRequestScopesForTests } from "./previewRequestScope";
import { fetchConversationBrowserPreview } from "../protocol/conversation-browser";
import { artifactResourceUrl } from "../lib/artifact-resource";
const transport = vi.hoisted(() => ({ sessionId: "browser-session" }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => transport }));
vi.mock("../protocol/conversation-browser", () => ({ fetchConversationBrowserPreview: vi.fn(),
  conversationBrowserRawUrl: (owner: {conversationId:string;executionCwd:string}, path: string) => {
    const url = new URL("http://127.0.0.1/api/workspace/browser-raw");
    url.searchParams.set("session_id", transport.sessionId); url.searchParams.set("conversation_id", owner.conversationId);
    url.searchParams.set("execution_cwd", owner.executionCwd); url.searchParams.set("path", path); return url.toString();
  },
}));
const target = { conversationId: "owner", executionCwd: "C:/base/date/task", path: "date/task/outputs/原始.txt", name: "原始.txt" };
const preview = { file_name: target.name, path: target.path, media_type: "text/plain", kind: "document", size_bytes: 18,
  summary: "", parse_error: "", content: "原始文本\r\n", content_chars: 6, truncated: false, has_native: false };
beforeEach(() => {
  vi.clearAllMocks(); resetPreviewRequestScopesForTests(); transport.sessionId = "browser-session";
  useAppStore.setState({ conversationId: "owner", conversations: [], conversationWorkbenchStates: {}, previewArtifact: null,
    workingDirectory: target.executionCwd, isConnected: true, panelSlots: [], rightPanelOpen: false,
    draft: "Keep original draft", attachments: [{ id: "original", name: "original.png", type: "image/png", size: 10, status: "ready" }] });
});

it("previews parsed browser bytes under the captured owner without promoting the base to execution or editor root", async () => {
  vi.mocked(fetchConversationBrowserPreview).mockResolvedValueOnce(preview);
  expect(openConversationBrowserFilePreview(target)).toBe(true);
  await vi.waitFor(() => expect(useAppStore.getState().previewArtifact).toMatchObject({ source: "browser", content: "原始文本\r\n", loading: false }));
  expect(fetchConversationBrowserPreview).toHaveBeenCalledWith({ conversationId: "owner", executionCwd: target.executionCwd }, target.path, expect.any(AbortSignal));
  expect(useAppStore.getState()).toMatchObject({ workingDirectory: target.executionCwd, draft: "Keep original draft", attachments: [{ id: "original" }] });
});

it("retains native image ownership and rebuilds its raw URL with the new session after reconnect", async () => {
  vi.mocked(fetchConversationBrowserPreview).mockResolvedValueOnce({ ...preview, media_type: "image/png", has_native: true });
  openConversationBrowserFilePreview(target);
  await vi.waitFor(() => expect(useAppStore.getState().previewArtifact?.url).toContain("browser-raw"));
  const originalUrl = useAppStore.getState().previewArtifact!.url!;
  transport.sessionId = "restored-browser-session";
  const rebuilt = artifactResourceUrl({ source: "browser", originalUrl, isConnected: true });
  expect(new URL(rebuilt).searchParams.get("session_id")).toBe(transport.sessionId);
  expect(new URL(rebuilt).searchParams.get("execution_cwd")).toBe(target.executionCwd);
  expect(new URL(rebuilt).searchParams.get("path")).toBe(target.path);
});

it("does not let a retired browser read replace the newly selected conversation or its preview", async () => {
  let finish!: (value: typeof preview) => void;
  vi.mocked(fetchConversationBrowserPreview).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  openConversationBrowserFilePreview(target);
  releasePreviewScope("owner");
  const currentPreview = { artifactId: "current", content: "Current task file", source: "local" as const, loadedAt: 1 };
  useAppStore.setState({ conversationId: "next", previewArtifact: currentPreview, workingDirectory: "C:/base/date/next", draft: "New task draft" });
  finish(preview);
  await Promise.resolve();
  expect(useAppStore.getState()).toMatchObject({ conversationId: "next", previewArtifact: currentPreview, workingDirectory: "C:/base/date/next", draft: "New task draft" });
});
