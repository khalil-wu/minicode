// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ToolCallEvent, ToolResultEvent } from "../../protocol/events";
import type { ArtifactPreview, ChatMessage, ContentBlock, ProgressContentBlock } from "../../stores/types";
import { useAppStore } from "../../stores";
import { handleChatStreamEvent } from "../chatStreamEvents";
import { projectMessagesToTurns } from "../chatSurfaceState";
import { getToolCallsFromMessage } from "../../lib/content-blocks";
import { createStreamBuffer } from "../../lib/stream-buffer";
import { canonicalArtifactKind, recordHasImageArtifact } from "../../lib/artifact-projection";
import { reduceToolCallResult, reduceToolCallStart, type ToolCallRecord } from "../../lib/tool-call-reducer";
import { AssistantMarkdownCell } from "./AssistantMarkdownCell";

// Unmodified frames emitted by the real PresentFileTool/store_result replay.
const capture = JSON.parse(readFileSync(resolve(process.cwd(), "src.v2/chat/cells/fixtures/present-file-large-result.json"), "utf8")) as {
  call: ToolCallEvent;
  result: ToolResultEvent;
};
const owner = capture.result.conversation_id!;
const initialStore = useAppStore.getState();
const image: ArtifactPreview = {
  artifactId: "unit-inline-image",
  kind: "image",
  mediaType: "image/png",
  summary: "Image",
  url: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5xQAAAAASUVORK5CYII=",
};

beforeEach(() => {
  useAppStore.setState({
    ...initialStore,
    conversationId: owner,
    messages: [],
    conversationMessages: {},
    conversationStreaming: {},
    sideChats: {},
    conversationWorkbenchStates: {},
    workingDirectory: "C:/production-cell-roots",
    isConnected: false,
  });
});

afterEach(() => {
  cleanup();
  document.body.style.overflow = "";
});

function seedMessage(blocks: ContentBlock[], isStreaming = false, artifacts: ArtifactPreview[] = []): ChatMessage {
  const message: ChatMessage = {
    id: "owned-message", role: "assistant", content: "", timestamp: Date.now(),
    blocks, artifacts, isStreaming,
  };
  useAppStore.setState({ messages: [message], isStreaming });
  return message;
}

function imageProgress(status: ProgressContentBlock["status"]): ProgressContentBlock {
  return {
    type: "progress", id: "provider:image:owned", stage: "image_generation",
    status, message: "正在生成图像", timestamp: Date.now(),
  };
}

function renderOwnedAnswer() {
  const state = useAppStore.getState();
  const turn = projectMessagesToTurns(state.messages, state.isStreaming)[0];
  render(<AssistantMarkdownCell cell={turn.finalAnswerCell!} conversationId={owner} />);
  return turn;
}

function capturedRecord(): ToolCallRecord {
  const calls = reduceToolCallStart(new Map(), capture.call);
  return reduceToolCallResult(calls, capture.result).get(capture.call.id)!;
}

describe("primary artifact identity is not the output file identity", () => {
  it("does not turn the actual persisted text result into its separate PNG deliverable", () => {
    const record = capturedRecord();
    expect(record.artifactId).toBe(capture.result.artifact_id);
    expect(record.outputFiles?.[0].mimeType).toBe("image/png");
    expect(record.outputFiles?.[0].isImage).toBe(true);
    expect(recordHasImageArtifact(record)).toBe(false);
    expect(canonicalArtifactKind(record.artifactKind, record.artifactMediaType, record)).toBe("file");
  });

  it("keeps declared JSON bytes distinct from an image output file", () => {
    const record = { ...capturedRecord(), artifactKind: "json", artifactMediaType: "application/json" };
    expect(recordHasImageArtifact(record)).toBe(false);
    expect(canonicalArtifactKind(record.artifactKind, record.artifactMediaType, record)).toBe("json");
  });

  it("preserves image evidence belonging to the primary resource itself", () => {
    const record = { ...capturedRecord(), artifactMediaType: "image/png; charset=binary" };
    expect(recordHasImageArtifact(record)).toBe(true);
    expect(canonicalArtifactKind(record.artifactKind, record.artifactMediaType, record)).toBe("image");
  });

  it("preserves the exact legacy browser screenshot discriminator", () => {
    const record = { ...capturedRecord(), name: "browser_control", args: { action: "screenshot" } };
    expect(recordHasImageArtifact(record)).toBe(true);
    expect(canonicalArtifactKind(record.artifactKind, record.artifactMediaType, record)).toBe("image");
  });

  it("keeps the real tool-result PNG on the owned reply without a text-artifact img URL", () => {
    seedMessage([] , true);
    const buffer = createStreamBuffer(() => {});
    expect(handleChatStreamEvent(capture.call, owner, { textStreamBuffer: buffer })).toBe(true);
    expect(handleChatStreamEvent(capture.result, owner, { textStreamBuffer: buffer })).toBe(true);
    useAppStore.getState().completeAgentMessage({ id: "answer", text: "文件已就绪。", source: "reply", status: "completed" }, owner);
    useAppStore.getState().finishStreaming(owner, undefined, "completed", "owned-message");
    const current = useAppStore.getState().messages[0];
    expect(getToolCallsFromMessage(current)[0].status).toBe("success");
    expect(current.replyAttachments).toEqual([{
      path: capture.result.output_files![0].path,
      size: capture.result.output_files![0].size,
      isImage: true,
    }]);
    const turn = renderOwnedAnswer();
    expect(turn.finalAnswerCell!.attachments![0].path).toBe(capture.result.output_files![0].path);
    const file = screen.getByRole("button", { name: /image\.png/ });
    expect(file.getAttribute("title")).toBe(capture.result.output_files![0].path);
    expect(document.querySelector(`img[src*="${capture.result.artifact_id}"]`)).toBeNull();
    buffer.destroy();
  });
});

describe("image progress uses the actual terminal status", () => {
  it.each(["interrupted", "partial"] as const)("settles %s through the real store and projection", (terminalStatus) => {
    seedMessage([imageProgress("running")], true);
    useAppStore.getState().finishStreaming(owner, undefined, terminalStatus, "owned-message");
    const turn = renderOwnedAnswer();
    expect(turn.status).toBe(terminalStatus);
    expect(turn.finalAnswerCell!.imageProgress![0].status).toBe("partial");
    const placeholder = document.querySelector(".assistant-cell-image-placeholder")!;
    expect(placeholder.getAttribute("data-running")).toBe("false");
    expect(placeholder.getAttribute("data-status")).toBe("partial");
    expect(placeholder.textContent).toContain("图像生成未完整结束");
    expect(placeholder.textContent).not.toContain("正在生成图像");
    expect(screen.getByRole("button", { name: "复制回复" })).toBeTruthy();
  });

  it("keeps partial evidence beside a surviving image instead of erasing it", () => {
    seedMessage([imageProgress("partial")], false, [image]);
    renderOwnedAnswer();
    expect(document.querySelector('.assistant-cell-image-placeholder[data-status="partial"]')?.getAttribute("data-running")).toBe("false");
    expect(screen.getByAltText("模型生成的图片")).toBeTruthy();
    expect(screen.getByRole("button", { name: "复制回复" })).toBeTruthy();
  });

  it("preserves incomplete failure detail without relabeling it as success", () => {
    const progress = { ...imageProgress("partial"), detail: "连接在结果传送中断开。" };
    seedMessage([progress]);
    renderOwnedAnswer();
    expect(screen.getByText("连接在结果传送中断开。")).toBeTruthy();
    expect(document.querySelector('.assistant-cell-image-placeholder[data-status="partial"]')?.getAttribute("data-running")).toBe("false");
  });

  it.each(["running", "completed"] as const)("retains %s waiting-for-bytes behavior", (status) => {
    seedMessage([imageProgress(status)]);
    renderOwnedAnswer();
    expect(document.querySelector(".assistant-cell-image-placeholder")?.getAttribute("data-running")).toBe("true");
    expect(screen.queryByRole("button", { name: "复制回复" })).toBeNull();
  });

  it("retains the existing failed image alert", () => {
    seedMessage([imageProgress("failed")]);
    renderOwnedAnswer();
    expect(screen.getByRole("alert").textContent).toContain("图像生成失败");
    expect(screen.getByRole("alert").getAttribute("data-running")).toBe("false");
  });
});

describe("generated-image modal borrows the existing focus owner", () => {
  async function openLightbox() {
    seedMessage([], false, [image]);
    renderOwnedAnswer();
    // Unit DOM does not fetch PNG bytes; the native Chrome replay checks real load.
    fireEvent.load(screen.getByAltText("模型生成的图片"));
    const opener = screen.getByRole("button", { name: "查看生成图片大图" });
    opener.focus();
    fireEvent.click(opener);
    const dialog = screen.getByRole("dialog", { name: "生成图片大图" });
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    return { opener, dialog };
  }

  it("enters the concrete dialog and traps Tab in both directions", async () => {
    const { dialog } = await openLightbox();
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    for (const shiftKey of [false, true]) {
      const event = new KeyboardEvent("keydown", { key: "Tab", shiftKey, bubbles: true, cancelable: true });
      document.activeElement!.dispatchEvent(event);
      expect(event.defaultPrevented).toBe(true);
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
  });

  it("closes with scoped Escape and restores the real opener and scroll state", async () => {
    document.body.style.overflow = "auto";
    const { opener, dialog } = await openLightbox();
    expect(document.body.style.overflow).toBe("hidden");
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "生成图片大图" })).toBeNull();
    expect(document.activeElement).toBe(opener);
    expect(document.body.style.overflow).toBe("auto");
  });

  it("restores the opener when the actual backdrop closes", async () => {
    const { opener, dialog } = await openLightbox();
    fireEvent.click(dialog);
    expect(screen.queryByRole("dialog", { name: "生成图片大图" })).toBeNull();
    expect(document.activeElement).toBe(opener);
  });
});
