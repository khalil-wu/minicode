/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  });
  return { send: vi.fn(() => true), upload: vi.fn() };
});

vi.mock("../protocol/ws-outbox", async (original) => ({
  ...await original<typeof import("../protocol/ws-outbox")>(),
  sendClientCommand: mocks.send,
}));
vi.mock("./uploads", () => ({
  uploadComposerFiles: mocks.upload,
  acceptAttachmentConversationOwner: vi.fn(() => true),
}));
vi.mock("./FooterRow", () => ({ FooterRow: () => <button type="button">Composer footer</button> }));
vi.mock("./MenuOverlay", () => ({ MenuOverlay: () => null }));
vi.mock("./ActionChipRegion", () => ({ ContextChipRegion: () => null }));
vi.mock("./AttachmentStrip", () => ({ AttachmentStrip: () => null }));
vi.mock("./QueuedMessageList", () => ({ QueuedMessageList: () => null }));
vi.mock("../chat/InlineAgentPrompt", () => ({ InlineAgentPrompt: () => null }));
vi.mock("../chat/components/TurnPlanProgress", () => ({ TurnPlanProgress: () => null }));

import { useAppStore } from "../stores";
import { Composer } from "./Composer";

beforeEach(() => {
  localStorage.clear();
  mocks.send.mockClear();
  mocks.upload.mockClear();
  useAppStore.setState({
    conversationId: "drop-owner",
    workingDirectory: "C:/drop-workspace",
    draft: "",
    messages: [],
    conversationMessages: {},
    sideChats: {},
    isConnected: true,
    isStreaming: false,
    currentModel: "gpt",
    sendShortcut: "enter",
    availableSkills: [],
    selectedSkills: [],
    selectedMentions: [],
    attachments: [],
    slashPanelOpen: false,
    mentionPanelOpen: false,
    slashCommands: [],
    activeGoal: null,
    pendingApproval: null,
    approvalQueue: [],
    pendingAskUser: null,
    askUserQueue: [],
    pendingDiffReview: null,
    diffReviewQueue: [],
    quotedMessage: null,
  });
});

afterEach(cleanup);

const renderComposer = (minimal = false) => {
  render(<Composer minimal={minimal} />);
  const input = screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement;
  const composer = input.closest(".composer-container") as HTMLDivElement;
  const footer = screen.getByRole("button", { name: "Composer footer" });
  return { input, composer, footer };
};

const fileTransfer = (files: File[] = []) => ({ types: ["Files"], files });

const beginFileDrag = (input: HTMLTextAreaElement, composer: HTMLDivElement) => {
  expect(fireEvent.dragOver(input, { dataTransfer: fileTransfer() })).toBe(false);
  expect(composer.getAttribute("data-drag-over")).toBe("true");
};

const leaveTo = (target: HTMLElement, relatedTarget: EventTarget | null) => {
  fireEvent(target, new MouseEvent("dragleave", { bubbles: true, relatedTarget }));
};

describe.each([false, true])("real composer file drop (minimal=%s)", (minimal) => {
  it("keeps provider handshakes out of the composer while retaining the active run and draft", () => {
    useAppStore.setState({ isStreaming: true, draft: "next question", agentProgress: [{
      type: "progress", id: "provider:request", stage: "status", status: "running", label: "provider", providerState: "connecting",
      message: "连接供应商", visibility: "debug", conversationId: "drop-owner", timestamp: 1,
    }] });
    const { input, composer } = renderComposer(minimal);
    expect(input.value).toBe("next question");
    expect(composer.parentElement?.textContent).not.toMatch(/连接供应商|Connecting|Waiting for model/);
    expect(document.querySelector(".provider-request-status")).toBeNull();
    expect(useAppStore.getState().isStreaming).toBe(true);
  });
  it("clears the border after the textarea consumes the drop and uploads exactly once", () => {
    const { input, composer } = renderComposer(minimal);
    const file = new File(["image"], "reference.png", { type: "image/png" });
    beginFileDrag(input, composer);

    expect(fireEvent.drop(input, { dataTransfer: fileTransfer([file]) })).toBe(false);

    expect(composer.getAttribute("data-drag-over")).toBe("false");
    expect(mocks.upload).toHaveBeenCalledExactlyOnceWith([file]);
  });

  it("accepts a file on the outer composer edge and uploads exactly once", () => {
    const { input, composer } = renderComposer(minimal);
    const file = new File(["document"], "notes.txt", { type: "text/plain" });
    beginFileDrag(input, composer);

    expect(fireEvent.drop(composer, { dataTransfer: fileTransfer([file]) })).toBe(false);

    expect(composer.getAttribute("data-drag-over")).toBe("false");
    expect(mocks.upload).toHaveBeenCalledExactlyOnceWith([file]);
  });
});

describe("composer drag lifecycle", () => {
  it("leaves ordinary text drag and drop available to the browser", () => {
    const { input, composer } = renderComposer();
    const dataTransfer = { types: ["text/plain"], files: [], getData: () => "dragged text" };

    expect(fireEvent.dragOver(input, { dataTransfer })).toBe(true);
    expect(composer.getAttribute("data-drag-over")).toBe("false");
    expect(fireEvent.drop(input, { dataTransfer })).toBe(true);
    expect(fireEvent.dragOver(composer, { dataTransfer })).toBe(true);
    expect(fireEvent.drop(composer, { dataTransfer })).toBe(true);
    expect(mocks.upload).not.toHaveBeenCalled();
  });

  it("keeps the border while moving between children and clears it on exit", () => {
    const { input, composer, footer } = renderComposer();
    beginFileDrag(input, composer);

    leaveTo(input, footer);
    expect(composer.getAttribute("data-drag-over")).toBe("true");
    leaveTo(footer, input);
    expect(composer.getAttribute("data-drag-over")).toBe("true");
    leaveTo(input, document.body);
    expect(composer.getAttribute("data-drag-over")).toBe("false");
  });

  it("clears the border when the drag leaves the window", () => {
    const { input, composer } = renderComposer();
    beginFileDrag(input, composer);

    leaveTo(input, null);

    expect(composer.getAttribute("data-drag-over")).toBe("false");
  });

  it.each(["outside drop", "dragend", "blur", "Escape"])("clears the border on %s", (ending) => {
    const { input, composer } = renderComposer();
    beginFileDrag(input, composer);

    if (ending === "outside drop") fireEvent.drop(document.body, { dataTransfer: fileTransfer() });
    if (ending === "dragend") fireEvent.dragEnd(document.body);
    if (ending === "blur") fireEvent.blur(window);
    if (ending === "Escape") fireEvent.keyDown(input, { key: "Escape" });

    expect(composer.getAttribute("data-drag-over")).toBe("false");
    expect(mocks.upload).not.toHaveBeenCalled();
  });

  it("cancels a file drag with Escape without interrupting the turn, then restores normal Escape", () => {
    useAppStore.setState({
      isStreaming: true,
      messages: [{
        id: "stream-message",
        turnId: "stream-turn",
        role: "assistant",
        content: "Working",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
    });
    const { input, composer } = renderComposer();
    mocks.send.mockClear();
    beginFileDrag(input, composer);

    expect(fireEvent.keyDown(input, { key: "Escape" })).toBe(false);

    expect(composer.getAttribute("data-drag-over")).toBe("false");
    expect(mocks.send).not.toHaveBeenCalled();
    expect(useAppStore.getState().isStreaming).toBe(true);

    fireEvent.keyDown(input, { key: "Escape" });

    expect(mocks.send).toHaveBeenCalledExactlyOnceWith({
      type: "interrupt",
      conversation_id: "drop-owner",
      turn_id: "stream-turn",
      message_id: "stream-message",
    });
  });
});
