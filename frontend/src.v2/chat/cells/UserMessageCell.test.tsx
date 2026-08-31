/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { UserMessageCell } from "./UserMessageCell";
import { useAppStore } from "../../stores";

const { sendMock, cancelQueuedMessageMock, openAttachmentPreviewMock, openLocalFilePreviewMock } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
  return {
    sendMock: vi.fn(),
    cancelQueuedMessageMock: vi.fn(async () => ({
      type: "command_result",
      ok: true,
      status: "success",
    })),
    openAttachmentPreviewMock: vi.fn(() => true),
    openLocalFilePreviewMock: vi.fn(() => true),
  };
});

vi.mock("../../hooks/useWebSocket", () => ({
  getWebSocket: () => ({ send: sendMock }),
}));

vi.mock("../../protocol/ws-outbox", () => ({
  sendClientCommand: sendMock,
  sendClientCommandAwaitResult: cancelQueuedMessageMock,
  commandResultSucceeded: vi.fn(() => true),
}));

vi.mock("../../overlays/DialogService", () => ({
  showConfirm: vi.fn(async () => true),
}));

vi.mock("../openAttachmentPreview", () => ({
  openAttachmentPreview: openAttachmentPreviewMock,
  openLocalFilePreview: openLocalFilePreviewMock,
}));

afterEach(() => {
  cleanup();
  sendMock.mockClear();
  cancelQueuedMessageMock.mockClear();
  openAttachmentPreviewMock.mockClear();
  openLocalFilePreviewMock.mockClear();
  useAppStore.setState({
    conversationId: null,
    messages: [],
    conversationStreaming: {},
    isStreaming: false,
  });
  vi.restoreAllMocks();
});

describe("UserMessageCell", () => {
  it("collapses long user messages by default and allows expanding them", () => {
    const longMessage = Array.from({ length: 20 }, (_, index) => `line ${index}`).join("\n");
    const { container } = render(
      <UserMessageCell
        cell={{
          kind: "user_message",
          id: "user-long",
          content: longMessage,
          createdAt: 1,
        }}
      />,
    );

    expect(container.querySelector(".user-cell-content")?.getAttribute("data-collapsed")).toBe("true");
    const expand = screen.getByRole("button", { name: "展开消息" });
    expect(expand.getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(expand);

    expect(container.querySelector(".user-cell-content")?.getAttribute("data-collapsed")).toBe("false");
    expect(screen.getByRole("button", { name: "收起消息" }).getAttribute("aria-expanded")).toBe("true");
  });

  it("keeps short user messages expanded without a toggle", () => {
    const { container } = render(
      <UserMessageCell
        cell={{ kind: "user_message", id: "user-short", content: "short message", createdAt: 1 }}
      />,
    );

    expect(container.querySelector(".user-cell-content")?.getAttribute("data-collapsed")).toBe("false");
    expect(screen.queryByRole("button", { name: "展开消息" })).toBeNull();
  });

  it("shows when a queued message was injected into the active turn", () => {
    render(
      <UserMessageCell
        cell={{
          kind: "user_message",
          id: "user-steer",
          content: "change direction",
          createdAt: 1,
          steeredIntoMessageId: "assistant-current",
        }}
      />,
    );

    expect(screen.getByText("已引导当前任务")).toBeTruthy();
  });

  it("does not render an empty text row for attachment-only prompts", () => {
    const { container } = render(
      <UserMessageCell
        cell={{
          kind: "user_message",
          id: "user-attachment-only",
          content: "   ",
          createdAt: 1,
          attachments: [
            { id: "att-doc", artifactId: "artifact-doc", name: "brief.md", type: "text/markdown" },
          ],
        }}
      />,
    );

    expect(container.querySelector(".user-cell-content")).toBeNull();
    expect(container.querySelector(".user-cell-attachments-only")).toBeTruthy();
    expect(screen.getByText("brief.md")).toBeTruthy();
  });

  it("renders repeated attachment names without duplicate React keys", () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    render(
      <UserMessageCell
        cell={{
          kind: "user_message",
          id: "user-1",
          content: "see images",
          createdAt: 1,
          attachments: [
            { id: "att-1", artifactId: "artifact-1", name: "image.png", type: "image/png" },
            { id: "att-2", artifactId: "artifact-2", name: "image.png", type: "image/png" },
          ],
        }}
      />,
    );

    expect(screen.getAllByText("image.png")).toHaveLength(2);
    expect(errorSpy.mock.calls.flat().join("\n")).not.toContain("Encountered two children with the same key");
  });

  it("opens a local image attachment in the unified Preview panel", () => {
    render(
      <UserMessageCell
        cell={{
          kind: "user_message",
          id: "user-2",
          content: "see image",
          createdAt: 1,
          attachments: [
            {
              id: "att-1",
              name: "image.png",
              type: "image/png",
              dataUrl: "data:image/png;base64,AA==",
            },
          ],
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "image.png" }));

    expect(openLocalFilePreviewMock).toHaveBeenCalledWith({
      id: "att-1",
      name: "image.png",
      mediaType: "image/png",
      kind: "image",
      url: "data:image/png;base64,AA==",
    });
  });

  it("opens an already-uploaded image by artifact id when no local dataUrl remains", async () => {
    render(
      <UserMessageCell
        cell={{
          kind: "user_message",
          id: "user-3",
          content: "see uploaded image",
          createdAt: 1,
          attachments: [
            {
              id: "att-1",
              artifactId: "artifact-image-1",
              name: "uploaded.png",
              type: "image/png",
            },
          ],
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "uploaded.png" }));

    expect(openAttachmentPreviewMock).toHaveBeenCalledWith({
      artifactId: "artifact-image-1",
      name: "uploaded.png",
      mediaType: "image/png",
      kind: "image",
    });
  });

  it("keeps multiple uploaded images individually addressable by artifact id", async () => {
    render(
      <UserMessageCell
        cell={{
          kind: "user_message",
          id: "user-4",
          content: "see uploaded images",
          createdAt: 1,
          attachments: [
            { id: "att-1", artifactId: "artifact-image-1", name: "image.png", type: "image/png" },
            { id: "att-2", artifactId: "artifact-image-2", name: "image.png", type: "image/png" },
          ],
        }}
      />,
    );

    const chips = screen.getAllByRole("button", { name: "image.png" });
    fireEvent.click(chips[1]);

    expect(openAttachmentPreviewMock).toHaveBeenCalledWith({
      artifactId: "artifact-image-2",
      name: "image.png",
      mediaType: "image/png",
      kind: "image",
    });
  });

  it("cancels a queued message against its transcript owner instead of the active conversation", async () => {
    useAppStore.setState({ conversationId: "conv-active" });
    render(
      <UserMessageCell
        conversationId="conv-queue-owner"
        cell={{
          kind: "user_message",
          id: "user-queued-owner",
          content: "queued prompt",
          createdAt: 1,
          queueState: "queued",
          queueMessageId: "queue-message-owner",
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "取消排队消息" }));

    await waitFor(() => expect(cancelQueuedMessageMock).toHaveBeenCalledWith({
      type: "user_message.queue.cancel",
      conversation_id: "conv-queue-owner",
      message_id: "queue-message-owner",
      user_message_id: "user-queued-owner",
    }, "user_message.queue.cancel"));
  });

  it("requests Stop and waits for its terminal event before recalling", async () => {
    const focusListener = vi.fn();
    window.addEventListener("composer:focus", focusListener);
    useAppStore.setState({
      conversationId: "conv-recall",
      messages: [
        { id: "user-5", role: "user", content: "old prompt", artifacts: [], timestamp: 1 },
        { id: "assistant-5", role: "assistant", content: "", blocks: [], artifacts: [], timestamp: 2, isStreaming: true },
      ],
      conversationMessages: {
        "conv-recall": [
          { id: "user-5", role: "user", content: "old prompt", artifacts: [], timestamp: 1 },
          { id: "assistant-5", role: "assistant", content: "", blocks: [], artifacts: [], timestamp: 2, isStreaming: true },
        ],
      },
      conversationStreaming: { "conv-recall": true },
      isStreaming: true,
    });

    render(
      <UserMessageCell
        cell={{
          kind: "user_message",
          id: "user-5",
          content: "old prompt",
          createdAt: 1,
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "撤回到输入框" }));

    await waitFor(() => expect(sendMock).toHaveBeenCalledWith({
      type: "interrupt",
      conversation_id: "conv-recall",
      message_id: "assistant-5",
    }));
    expect(focusListener).not.toHaveBeenCalled();
    expect(useAppStore.getState().isStreaming).toBe(true);
    window.removeEventListener("composer:focus", focusListener);
  });
});
