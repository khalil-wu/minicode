/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import type { ChatMessage } from "../stores/types";
import type { ClientCommand, ServerEvent } from "../protocol/events";
import { registerWebSocketSender, resolveClientCommandResult, sendConversationDeleteCommand } from "../protocol/ws-outbox";
import { cacheEditorStateForWorkspace, clearEditorWorkspaceBufferCacheForTests } from "../stores/shared-helpers";
import { buildUpdateActivitySnapshot } from "../desktop/updateActivityMirror";
import { AssistantMarkdownCell } from "./cells/AssistantMarkdownCell";
import { ActivityCell } from "./cells/ActivityCell";
import { DiffCell } from "./cells/DiffCell";
import { InlineAgentPrompt } from "./InlineAgentPrompt";
import { InlineDiff } from "./diff/InlineDiff";
import { MarkdownRenderer } from "./messages/MarkdownRenderer";
import { hydrateMessages } from "./transcriptHydration";
import { handleArtifactEvent } from "./artifactEvents";
import { handleControlEvent } from "./controlEvents";
import { handleSessionEvent } from "./sessionEvents";
import { handleChatStreamEvent } from "./chatStreamEvents";

const mocks = vi.hoisted(() => ({
  preview: vi.fn(() => true),
  confirm: vi.fn(async () => true),
  kill: vi.fn(async () => 1),
  closeBrowser: vi.fn(async () => 1),
  terminals: vi.fn(async () => []),
  browsers: vi.fn(async () => []),
}));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "session", send: () => true }) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../overlays/DialogService", () => ({ showConfirm: mocks.confirm }));
vi.mock("./openAttachmentPreview", () => ({
  openWorkspaceFilePreview: mocks.preview, openArtifactPreview: vi.fn(),
  openAttachmentPreview: vi.fn(), openLocalFilePreview: vi.fn(),
}));
vi.mock("../desktop/runtime", () => ({
  isDesktop: () => true, desktop: () => undefined,
  openPath: vi.fn(), revealPath: vi.fn(),
  ptyKillConversation: mocks.kill, embeddedBrowserCloseConversation: mocks.closeBrowser,
  ptyList: mocks.terminals, embeddedBrowserList: mocks.browsers,
}));

const initial = useAppStore.getState();
const sent: ClientCommand[] = [];
const assistant = (id: string, patch: Partial<ChatMessage> = {}): ChatMessage => ({
  id, role: "assistant", content: "", blocks: [], artifacts: [], timestamp: 1, isStreaming: true, ...patch,
});
const buffer = { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() };
const handle = (event: Record<string, unknown>) => handleChatStreamEvent(event as ServerEvent, String(event.conversation_id), { textStreamBuffer: buffer });
const usage = { input_tokens: 100, output_tokens: 20, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, input_includes_cache_read: true };

beforeEach(() => {
  vi.clearAllMocks();
  clearEditorWorkspaceBufferCacheForTests();
  useAppStore.setState(initial, true);
  useAppStore.setState({
    conversationId: "parent", workingDirectory: "C:/parent", isConnected: true, runtimeSession: {},
    conversations: [{ id: "parent", title: "Parent", updatedAt: "2026-09-09T00:00:00Z", workspaceRoot: "C:/parent" }],
    messages: [assistant("A", { turnId: "turn-A" })], conversationStreaming: { parent: true }, isStreaming: true,
  });
  sent.length = 0;
  mocks.confirm.mockResolvedValue(true);
  registerWebSocketSender((command) => {
    sent.push(command);
    if (command.type === "diff.git_revert_patch") queueMicrotask(() => resolveClientCommandResult({
      type: "command.result", command: command.type, level: "success", message: "reverted",
      data: { client_command_id: command.client_command_id },
    }));
    return true;
  });
});
afterEach(() => { cleanup(); registerWebSocketSender(null); });

describe("repaired transcript and resource contracts", () => {
  it("keeps code and copied source intact when hiding prose citation markers", async () => {
    const source = 'Example [1]\n\n```python\nif ok:\n    values = [1]\n    print("a  b")\n```';
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const { container } = render(<AssistantMarkdownCell cell={{
      kind: "assistant_markdown", id: "reply", markdownSource: source, phase: "final", copyable: true,
      citations: [{ source: "https://example.org", range: [0, 1] }], createdAt: 1,
    }} />);
    expect(container.querySelector(".md-body p")?.textContent?.trim()).toBe("Example");
    expect(container.textContent).toContain("values = [1]");
    fireEvent.click(screen.getByRole("button", { name: "复制回复" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(source));
  });

  it("opens child transcript file links in the child workspace", () => {
    render(<AssistantMarkdownCell isTranscriptMode conversationId="child" workspaceRoot="C:/child" cell={{
      kind: "assistant_markdown", id: "reply", markdownSource: "[child](src/main.ts:12)", phase: "final", copyable: true, createdAt: 1,
    }} />);
    fireEvent.click(screen.getByRole("button", { name: "child" }));
    expect(mocks.preview).toHaveBeenCalledWith(expect.objectContaining({ path: "src/main.ts", workspaceRoot: "C:/child", conversationId: "child" }));
    expect(useAppStore.getState().editorOpenRequests).toEqual([]);
  });

  it("keeps tool-record file targets in the child workspace", () => {
    const records = ["src/one.ts", "src/two.ts"].map((path, index) => ({
      id: `read-${index}`, name: "read_file", args: { file_path: path }, status: "success" as const,
      activityKind: "fileRead", resultKind: "file", startedAt: 1, outputPreview: "file content",
    }));
    render(<ActivityCell conversationId="child" workspaceRoot="C:/child" cell={{
      kind: "activity", id: "read", activityKind: "fileRead", title: "Read", status: "done", collapsed: false, startedAt: 1, toolCallRecords: records,
    }} />);
    fireEvent.click(screen.getByRole("button", { name: "打开 src/one.ts" }));
    expect(mocks.preview).toHaveBeenCalledWith(expect.objectContaining({ path: "src/one.ts", workspaceRoot: "C:/child", conversationId: "child" }));
  });

  it("retains the line on a linkified absolute Windows file reference", () => {
    useAppStore.setState({ workingDirectory: "C:/repo" });
    render(<MarkdownRenderer content="C:/repo/main.ts:12" workspaceRoot="C:/repo" />);
    fireEvent.click(screen.getByRole("button", { name: "C:/repo/main.ts:12" }));
    expect(useAppStore.getState().editorOpenRequests.at(-1)).toMatchObject({ path: "main.ts", line: 12 });
  });

  it("uses each file's real hunk line numbers", () => {
    const { container } = render(<InlineDiff patch={"diff --git a/one b/one\n@@ -10 +10 @@\n-old\n+new\ndiff --git a/two b/two\n@@ -1 +1 @@\n-old\n+new\n"} />);
    expect([...container.querySelectorAll(".inline-diff-line-added .inline-diff-number")].map((node) => node.textContent)).toEqual(["10", "1"]);
  });

  it("enables the next diff approval after completing the first", async () => {
    useAppStore.getState().setDiffReview({ requestId: "first", conversationId: "parent", diff: "one" });
    useAppStore.getState().setDiffReview({ requestId: "second", conversationId: "parent", diff: "two" });
    render(<InlineAgentPrompt />);
    fireEvent.click(screen.getByRole("button", { name: "允许文件更改" }));
    await waitFor(() => expect(useAppStore.getState().pendingDiffReview?.requestId).toBe("second"));
    expect((screen.getByRole("button", { name: "允许文件更改" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "拒绝文件更改" }));
    await waitFor(() => expect(useAppStore.getState().pendingDiffReview).toBeNull());
  });

  it("sends the exact displayed patch with the original owner after a dialog-time switch", async () => {
    let confirm!: (value: boolean) => void;
    mocks.confirm.mockImplementationOnce(() => new Promise((resolve) => { confirm = resolve; }));
    const patch = "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-before\n+after\n";
    render(<DiffCell conversationId="parent" workspaceRoot="C:/parent" cell={{
      kind: "diff", id: "edit", status: "updated", files: [{ path: "file.txt", patch, additions: 1, deletions: 1 }],
      summary: { added: 1, deleted: 1, modifiedFiles: 1 }, collapsed: false, createdAt: 1,
    }} />);
    fireEvent.click(screen.getByRole("button", { name: "撤销" }));
    act(() => { useAppStore.setState({ conversationId: "other", workingDirectory: "C:/other" }); confirm(true); });
    await waitFor(() => expect(sent.some((command) => command.type === "diff.git_revert_patch")).toBe(true));
    expect(sent.find((command) => command.type === "diff.git_revert_patch")).toMatchObject({ conversation_id: "parent", workspace_root: "C:/parent", patch });
    expect(sent.some((command) => command.type === "diff.git_revert_file")).toBe(false);
  });

  it("hydrates turn ownership, image progress, and attachment-only messages", () => {
    const messages = hydrateMessages([
      { id: "image", role: "assistant", turn_id: "image-turn", content: "", blocks: [{ type: "progress", id: "image-progress", stage: "image_generation", status: "failed", message: "Failed" }] },
      { id: "artifact-only", role: "assistant", content: "", artifacts: [{ artifactId: "image-file", kind: "image", summary: "Generated" }] },
    ]);
    expect(messages).toHaveLength(2);
    expect(messages[0]).toMatchObject({ turnId: "image-turn", blocks: [{ stage: "image_generation" }] });
    expect(messages[1].artifacts[0].artifactId).toBe("image-file");
  });

  it("projects side-chat artifacts and citations into the real side-chat cache", () => {
    useAppStore.setState({ sideChats: { side: { id: "side", draft: "", isStreaming: true, messages: [assistant("side-answer")] } } });
    handleArtifactEvent({ type: "artifact.preview", conversation_id: "side", message_id: "side-answer", artifact_id: "image", kind: "image", summary: "Generated" });
    handleArtifactEvent({ type: "citation.add", message_id: "side-answer", source: "https://example.org", range: [0, 2] }, "side");
    const message = useAppStore.getState().sideChats.side.messages[0];
    expect(message.artifacts[0].artifactId).toBe("image");
    expect(message.citations?.[0].source).toBe("https://example.org");
  });

  it("keeps a queued assistant idle when a session inventory confirms an active run", () => {
    const messages = [assistant("A"), assistant("Q", { queueState: "queued", isStreaming: false })];
    useAppStore.setState({ messages, conversationMessages: { parent: messages } });
    handleSessionEvent({ type: "conversation.list", conversations: [{ id: "parent", title: "Parent" }], active_conversation_id: "parent", session: { active_stream_conversation_ids: ["parent"] } }, { textStreamBuffer: buffer, thinkingStreamBuffer: buffer });
    expect(useAppStore.getState().messages.map((message) => message.isStreaming)).toEqual([true, false]);
  });

  it("does not stop an active task for another conversation's billing error", () => {
    useAppStore.setState({ conversationMessages: { background: [assistant("background-answer")] }, conversationStreaming: { parent: true, background: true } });
    handle({ type: "error", conversation_id: "background", message_id: "background-answer", error_type: "billing", recoverable: false, message: "Balance insufficient" });
    expect(useAppStore.getState().isStreaming).toBe(true);
    expect(useAppStore.getState().conversationMessages.background[0].terminalStatus).toBe("failed");
  });

  it("enriches an error-sealed turn with DONE without reopening it or closing a newer turn", () => {
    handle({ type: "error", conversation_id: "parent", message_id: "A", error_type: "auth", recoverable: false, message: "Unauthorized" });
    useAppStore.setState((state) => ({ messages: [...state.messages, assistant("B")], isStreaming: true, conversationStreaming: { parent: true } }));
    const done = { type: "done", conversation_id: "parent", message_id: "A", turn_id: "turn-A", status: "failed", reason: "auth_error", duration_ms: 1234, usage };
    handle(done);
    handle(done);
    expect(useAppStore.getState().messages[0]).toMatchObject({ isStreaming: false, usage: { input: 100 }, terminationReason: "auth_error", durationMs: 1234 });
    expect(useAppStore.getState().messages[1].isStreaming).toBe(true);
    expect(useAppStore.getState().isStreaming).toBe(true);
    expect(useAppStore.getState().usageTotals.turns).toBe(1);
  });

  it("accepts a running resume snapshot after a recoverable error", () => {
    handle({ type: "stream_resume", conversation_id: "parent", message_id: "A", stream_status: "running", last_event_type: "error", event_seq: 123, tool_calls_pending: [], content_blocks: [{ type: "text", itemId: "answer", content: "Still running", isStreaming: true }] });
    expect(useAppStore.getState().messages[0].blocks?.[0]).toMatchObject({ content: "Still running" });
  });

  it("reports hidden dirty buffers and rejects cleanup before closing local resources", async () => {
    cacheEditorStateForWorkspace("C:/hidden", [{ id: "dirty", path: "unsaved.ts", content: "changed", original: "old", loading: false }], "unsaved.ts", "unsaved.ts");
    expect(buildUpdateActivitySnapshot(useAppStore.getState()).dirtyEditors).toEqual(["c:/hidden/unsaved.ts"]);
    handleControlEvent({ type: "control_request", request_id: "cleanup", conversation_id: "hidden", request: { subtype: "conversation_resources_cleanup", workspace_root: "C:/hidden" } });
    await waitFor(() => expect(sent.some((command) => command.type === "control_response")).toBe(true));
    expect(sent.find((command) => command.type === "control_response")).toMatchObject({ response: { response: { action: "reject" } } });
    expect(mocks.kill).not.toHaveBeenCalled();
    expect(mocks.closeBrowser).not.toHaveBeenCalled();
  });

  it("does not close resources when a delete could not be sent", async () => {
    registerWebSocketSender(null);
    expect(await sendConversationDeleteCommand({ type: "conversation.delete", conversation_id: "parent" })).toBe(false);
    expect(mocks.kill).not.toHaveBeenCalled();
    expect(mocks.closeBrowser).not.toHaveBeenCalled();
  });

  it("acknowledges cleanup only after owned local resources have closed", async () => {
    let finishCleanup!: (value: number) => void;
    mocks.kill.mockReturnValueOnce(new Promise<number>((resolve) => { finishCleanup = resolve; }));
    handleControlEvent({ type: "control_request", request_id: "cleanup", conversation_id: "parent", request: { subtype: "conversation_resources_cleanup", workspace_root: "C:/parent" } });
    await Promise.resolve();
    expect(sent.some((command) => command.type === "control_response")).toBe(false);
    finishCleanup(1);
    await waitFor(() => expect(sent.some((command) => command.type === "control_response")).toBe(true));
    expect(mocks.kill).toHaveBeenCalledWith("parent");
    expect(mocks.closeBrowser).toHaveBeenCalledWith("parent");
    expect(sent.find((command) => command.type === "control_response")).toMatchObject({ request_id: "cleanup", conversation_id: "parent", response: { response: { action: "approve" } } });
  });

  it("rejects backend cleanup when the desktop terminal cannot be closed", async () => {
    mocks.kill.mockRejectedValueOnce(new Error("terminal still running"));
    handleControlEvent({ type: "control_request", request_id: "cleanup", conversation_id: "parent", request: { subtype: "conversation_resources_cleanup", workspace_root: "C:/parent" } });
    await waitFor(() => expect(sent.some((command) => command.type === "control_response")).toBe(true));
    expect(sent.find((command) => command.type === "control_response")).toMatchObject({
      request_id: "cleanup", conversation_id: "parent",
      response: { response: { action: "reject", guidance: "terminal still running" } },
    });
  });
});
