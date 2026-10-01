// @vitest-environment jsdom
import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { computeAccessibleName } from "dom-accessibility-api";
import { useAppStore } from "../stores";
import { Composer } from "./Composer";
import { ComposerTextarea } from "./ComposerTextarea";
import { FooterRow } from "./FooterRow";
import { AttachmentStrip } from "./AttachmentStrip";
import { PromptHistoryOverlay } from "./PromptHistoryOverlay";
import { MenuOverlay } from "./MenuOverlay";
import { mentionSearchCache, mentionTreeCache } from "./mentionCache";
import { sendChatMessage } from "../chat/sendChatMessage";
import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { buildContextNativeAttachments } from "./contextPayload";
import { fsListTree, fsSearchFiles } from "../desktop/runtime";
import { openAttachmentPreview, openLocalFilePreview } from "../chat/openAttachmentPreview";
import { retryComposerAttachment } from "./uploads";
import { showConfirm } from "../overlays/DialogService";
import { pushToast } from "../overlays/ToastContainer";

vi.mock("../protocol/ws-outbox", async (load) => ({
  ...await load<typeof import("../protocol/ws-outbox")>(),
  sendClientCommand: vi.fn(() => true),
  sendClientCommandAwaitResult: vi.fn(async (_command, command) => ({ type: "command.result", command, level: "success", message: "" })),
}));
vi.mock("../protocol/api", async (load) => ({
  ...await load<typeof import("../protocol/api")>(),
  fetchWithTimeout: vi.fn(async () => new Response(JSON.stringify({ plugins: [] }), { status: 200 })),
}));
vi.mock("../desktop/runtime", async (load) => ({
  ...await load<typeof import("../desktop/runtime")>(),
  isDesktop: () => true,
  fsListTree: vi.fn(async () => []),
  fsSearchFiles: vi.fn(async () => []),
}));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "session-A" }) }));
vi.mock("../chat/sendChatMessage", () => ({ sendChatMessage: vi.fn(async () => true) }));
vi.mock("./contextPayload", () => ({
  buildContextPayload: vi.fn(async () => "Directory reference: C:/A/docs"),
  buildContextNativeAttachments: vi.fn(async () => ({ attachments: [], attachmentRefs: [], notes: "" })),
}));
vi.mock("./uploads", () => ({
  acceptAttachmentConversationOwner: vi.fn(() => true),
  uploadComposerFiles: vi.fn(), cancelComposerUpload: vi.fn(), retryComposerAttachment: vi.fn(),
}));
vi.mock("../chat/openAttachmentPreview", () => ({ openAttachmentPreview: vi.fn(), openLocalFilePreview: vi.fn() }));
vi.mock("../overlays/DialogService", () => ({ showConfirm: vi.fn(async () => true) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../chat/InlineAgentPrompt", () => ({ InlineAgentPrompt: () => null }));
vi.mock("../chat/components/TurnPlanProgress", () => ({ TurnPlanProgress: () => null }));
vi.mock("../shell/UsageRing", () => ({ UsageRing: () => null }));

const initial = useAppStore.getState();
const pr = (number: number) => ({ prUrl: `https://github.com/project/pull/${number}`, prNumber: number, ciStatus: "passed" as const, autoFix: false, autoMerge: false, lastCheckedAt: 0 });
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};

beforeEach(() => {
  vi.clearAllMocks();
  mentionTreeCache.clear(); mentionSearchCache.clear();
  useAppStore.setState({ ...initial, conversationId: "A", workingDirectory: "C:/A", permissionMode: "confirm", currentModel: "model", currentProvider: "openai", availableModels: ["model"], isConnected: true, isStreaming: false, draft: "", attachments: [], selectedMentions: [], selectedSkills: [], availableSkills: [], slashCommands: [], pendingApproval: null, pendingAskUser: null, pendingDiffReview: null, prMonitor: null, fileTreeVersion: 0 });
  vi.mocked(showConfirm).mockResolvedValue(true);
  vi.mocked(fsListTree).mockResolvedValue([]);
  vi.mocked(fsSearchFiles).mockResolvedValue([]);
  vi.mocked(buildContextNativeAttachments).mockResolvedValue({ attachments: [], attachmentRefs: [], notes: "" });
  HTMLElement.prototype.scrollIntoView = vi.fn();
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("composer user-boundary regressions", () => {
  it.each(["conversation", "workspace"])("cancels an unowned async send after a %s switch without clearing the new draft", async (kind) => {
    const pending = deferred<Awaited<ReturnType<typeof buildContextNativeAttachments>>>();
    vi.mocked(buildContextNativeAttachments).mockReturnValueOnce(pending.promise);
    useAppStore.setState({ conversationId: null, draft: "original task", selectedMentions: [{ kind: "folder", path: "C:/A/docs", name: "docs" }] });
    render(<Composer />);
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(buildContextNativeAttachments).toHaveBeenCalledTimes(1));
    act(() => useAppStore.setState({ conversationId: kind === "conversation" ? "B" : null, workingDirectory: "C:/B", draft: "new draft" }));
    await act(async () => pending.resolve({ attachments: [], attachmentRefs: [], notes: "" }));
    expect(sendChatMessage).not.toHaveBeenCalled();
    expect(useAppStore.getState().draft).toBe("new draft");
    expect(pushToast).toHaveBeenCalledWith(expect.stringContaining("本次发送已取消"), "warning", 4500);
  });

  it("still sends an unchanged blank intent", async () => {
    useAppStore.setState({ conversationId: null, draft: "task" });
    render(<Composer />);
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(sendChatMessage).toHaveBeenCalledTimes(1));
    expect(vi.mocked(sendChatMessage).mock.calls[0][0].conversationId).toBeUndefined();
  });

  it("keeps Unicode mention filtering open but does not treat an email as a mention", () => {
    render(<Composer />);
    fireEvent.change(screen.getByRole("textbox", { name: "消息输入" }), { target: { value: "@报告/é😀.md" } });
    expect(useAppStore.getState().mentionPanelOpen).toBe(true);
    fireEvent.change(screen.getByRole("textbox", { name: "消息输入" }), { target: { value: "user@example.com" } });
    expect(useAppStore.getState().mentionPanelOpen).toBe(false);
  });

  it.each([false, true])("does not steal attachment action activation (image=%s)", (image) => {
    const file = new File(["file"], image ? "file.png" : "file.txt", { type: image ? "image/png" : "text/plain" });
    useAppStore.setState({ attachments: [{ id: "attachment", name: file.name, type: file.type, size: file.size, status: "error", error: "offline", localFile: file, dataUrl: image ? "data:image/png;base64,AA==" : undefined, conversationId: "A" }] });
    render(<AttachmentStrip />);
    const retry = screen.getByRole("button", { name: image ? /上传失败/ : /重新上传/ });
    const remove = screen.getByRole("button", { name: `移除 ${file.name}` });
    for (const key of ["Enter", " "]) {
      expect(fireEvent.keyDown(retry, { key })).toBe(true);
      expect(fireEvent.keyDown(remove, { key })).toBe(true);
    }
    fireEvent.click(retry);
    expect(retryComposerAttachment).toHaveBeenCalledWith("attachment");
    fireEvent.click(remove);
    expect(useAppStore.getState().attachments).toEqual([]);
    expect(openAttachmentPreview).not.toHaveBeenCalled();
    expect(openLocalFilePreview).not.toHaveBeenCalled();
    expect(retry.closest('[role="button"]')).toBeNull();
  });

  it("history native actions retain keyboard ownership; the search input retains navigation", async () => {
    const onClear = vi.fn(), onSelect = vi.fn(), onClose = vi.fn();
    render(<PromptHistoryOverlay open items={["first", "second"]} onClear={onClear} onSelect={onSelect} onClose={onClose} />);
    await act(async () => {});
    const clear = screen.getByRole("button", { name: "清空输入历史" });
    clear.focus();
    expect(fireEvent.keyDown(clear, { key: "Enter" })).toBe(true);
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.click(clear);
    expect(onClear).toHaveBeenCalledTimes(1);
    const second = screen.getByRole("option", { name: "second" });
    second.focus();
    expect(fireEvent.keyDown(second, { key: "Enter" })).toBe(true);
    fireEvent.click(second);
    expect(onSelect).toHaveBeenLastCalledWith("second");
    const input = screen.getByRole("textbox", { name: "搜索输入历史" });
    input.focus(); fireEvent.keyDown(input, { key: "ArrowDown" }); fireEvent.keyDown(input, { key: "Enter" });
    expect(onSelect).toHaveBeenLastCalledWith("second");
    fireEvent.keyDown(clear, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("gives the primary textarea a stable accessible name", () => {
    render(<ComposerTextarea value="" onChange={vi.fn()} onSubmit={vi.fn()} />);
    expect(computeAccessibleName(screen.getByRole("textbox"))).toBe("消息输入");
  });

  it.each(["", "new"])("invalidates cached mention results on the existing file revision (query=%s)", async (filter) => {
    const view = render(<MenuOverlay open kind="mention" filter={filter} onSelect={vi.fn()} />);
    const read = filter ? fsSearchFiles : fsListTree;
    await waitFor(() => expect(read).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText("未找到文件或插件")).toBeTruthy());
    view.rerender(<MenuOverlay open={false} kind="mention" filter={filter} onSelect={vi.fn()} />);
    vi.mocked(fsListTree).mockResolvedValue([{ name: "new.ts", path: "C:/A/new.ts", isDirectory: false }]);
    vi.mocked(fsSearchFiles).mockResolvedValue([{ name: "new.ts", path: "C:/A/new.ts", kind: "file" }]);
    act(() => useAppStore.getState().bumpFileTreeVersion());
    view.rerender(<MenuOverlay open kind="mention" filter={filter} onSelect={vi.fn()} />);
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByText("new.ts")).toBeTruthy());
  });

  it("never applies A's full-access confirmation to B", async () => {
    const pending = deferred<boolean>();
    vi.mocked(showConfirm).mockReturnValueOnce(pending.promise);
    render(<FooterRow sendState="idle" onSend={vi.fn()} />);
    fireEvent.click(screen.getByTitle("权限：询问"));
    fireEvent.click(screen.getByRole("option", { name: "完全访问" }));
    await waitFor(() => expect(showConfirm).toHaveBeenCalledTimes(1));
    act(() => useAppStore.setState({ conversationId: "B", workingDirectory: "C:/B" }));
    await act(async () => pending.resolve(true));
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
    expect(useAppStore.getState().permissionMode).toBe("confirm");
    expect(pushToast).toHaveBeenCalledWith(expect.stringContaining("未应用原完全访问确认"), "warning");
  });

  it("still applies a full-access decision to its unchanged owner", async () => {
    render(<FooterRow sendState="idle" onSend={vi.fn()} />);
    fireEvent.click(screen.getByTitle("权限：询问"));
    fireEvent.click(screen.getByRole("option", { name: "完全访问" }));
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({ type: "conversation.permission_mode.set", conversation_id: "A", mode: "bypass" }), "conversation.permission_mode.set"));
  });

  it("never replaces B's PR with A's pending automation confirmation", async () => {
    const pending = deferred<boolean>();
    vi.mocked(showConfirm).mockReturnValueOnce(pending.promise);
    useAppStore.setState({ prMonitor: pr(42) });
    render(<FooterRow sendState="idle" onSend={vi.fn()} />);
    fireEvent.click(screen.getByText("自动合并"));
    await waitFor(() => expect(showConfirm).toHaveBeenCalledTimes(1));
    act(() => useAppStore.setState({ conversationId: "B", workingDirectory: "C:/B", prMonitor: pr(70) }));
    await act(async () => pending.resolve(true));
    expect(vi.mocked(sendClientCommand).mock.calls.some(([command]) => command.type === "git.pr_automation.set")).toBe(false);
    expect(useAppStore.getState().prMonitor).toEqual(pr(70));
  });

  it("pins unchanged PR commands to A and waits for server projection even on send admission", async () => {
    useAppStore.setState({ prMonitor: pr(42) });
    render(<FooterRow sendState="idle" onSend={vi.fn()} />);
    fireEvent.click(screen.getByText("自动合并"));
    await waitFor(() => expect(sendClientCommand).toHaveBeenCalledWith(expect.objectContaining({ type: "git.pr_automation.set", conversation_id: "A", workspace_root: "C:/A", auto_merge: true })));
    expect(useAppStore.getState().prMonitor?.autoMerge).toBe(false);
  });
});
