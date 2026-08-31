import { describe, expect, it } from "vitest";
import type { ChatMessage } from "../../stores/types";
import { collectArtifacts } from "./ArtifactsTab";

const messageWithToolArtifact = (overrides: Partial<ChatMessage> = {}): ChatMessage => ({
  id: "message-browser",
  role: "assistant",
  content: "",
  artifacts: [],
  timestamp: 100,
  blocks: [{
    type: "tool_call",
    record: {
      id: "browser-call",
      name: "browser_control",
      args: { action: "screenshot" },
      status: "success",
      artifactId: "artifact-browser-shot",
      artifactKind: "image",
      artifactMediaType: "image/png",
      artifactBytes: 2048,
      displaySummary: "浏览器截图",
      startedAt: 100,
      finishedAt: 101,
    },
  }],
  ...overrides,
});

describe("ArtifactsTab projection", () => {
  it("includes image artifacts owned by tool_call records", () => {
    const [item] = collectArtifacts([messageWithToolArtifact()], null, "conversation-browser");

    expect(item).toMatchObject({
      artifactId: "artifact-browser-shot",
      kind: "image",
      label: "浏览器截图",
      mediaType: "image/png",
      conversationId: "conversation-browser",
    });
  });

  it("deduplicates a tool artifact already projected on its message", () => {
    const message = messageWithToolArtifact({
      artifacts: [{
        artifactId: "artifact-browser-shot",
        kind: "image",
        summary: "浏览器截图",
        mediaType: "image/png",
      }],
    });

    expect(collectArtifacts([message], null, "conversation-browser")).toHaveLength(1);
  });

  it("merges sparse message metadata with the richer tool record", () => {
    const message = messageWithToolArtifact({
      artifacts: [{
        artifactId: "artifact-browser-shot",
        kind: "browser_screenshot" as never,
        summary: " ",
      }],
    });

    const [item] = collectArtifacts([message], null, "conversation-browser");
    expect(item).toMatchObject({
      artifactId: "artifact-browser-shot",
      kind: "image",
      mediaType: "image/png",
      detail: "2.0 KB",
      conversationId: "conversation-browser",
      url: undefined,
    });
  });

  it("keeps an upload separate when its id matches a generated artifact id", () => {
    const message = messageWithToolArtifact({
      attachmentRefs: [{
        id: "upload-1",
        artifactId: "artifact-browser-shot",
        name: "same-id.png",
        kind: "image",
        mediaType: "image/png",
      }],
    });

    const items = collectArtifacts([message], null, "conversation-browser");
    expect(items).toHaveLength(2);
    expect(items.map((item) => item.id)).toContain("attachment:artifact-browser-shot");
    expect(items.map((item) => item.kind)).toContain("attachment");
  });

  it("classifies an image MIME even when the declared kind is unknown", () => {
    const message = messageWithToolArtifact({
      blocks: [{
        type: "tool_call",
        record: {
          ...messageWithToolArtifact().blocks?.[0]?.record,
          artifactKind: "old_browser_result",
          artifactMediaType: "IMAGE/WEBP; charset=binary",
        },
      }],
    });
    const [item] = collectArtifacts([message], null, "conversation-browser");
    expect(item.kind).toBe("image");
    expect(item.mediaType).toBe("image/webp");
  });

  it("projects a legacy toolCalls screenshot when blocks are absent", () => {
    const legacyMessage = Object.assign(messageWithToolArtifact({ blocks: undefined }), {
      toolCalls: [{
        id: "browser-legacy",
        name: "browser_control",
        args: { action: "screenshot" },
        status: "success",
        artifactId: "legacy-shot",
        artifactKind: "browser_screenshot",
        artifactMediaType: "image/png",
        displaySummary: "浏览器截图",
      }],
    }) as ChatMessage;

    expect(collectArtifacts([legacyMessage], null, "conversation-browser")).toMatchObject([{
      artifactId: "legacy-shot",
      kind: "image",
      label: "浏览器截图",
    }]);
  });

  it("keeps the current preview at the top after applying the artifact cap", () => {
    const messages = Array.from({ length: 31 }, (_, index) => messageWithToolArtifact({
      id: `assistant-${index}`,
      blocks: [],
      artifacts: [{
        artifactId: `artifact-${index}`,
        kind: "file",
        summary: `output-${index}.txt`,
      }],
    }));
    const items = collectArtifacts(messages, {
      artifactId: "preview-current",
      content: "",
      name: "preview-current.png",
      kind: "image",
      mediaType: "image/png",
      loadedAt: 1,
    }, "conversation-browser");

    expect(items).toHaveLength(30);
    expect(items[0]).toMatchObject({
      artifactId: "preview-current",
      label: "preview-current.png",
      kind: "image",
    });
  });

  it("does not carry an artifact owner across conversation projections", () => {
    const first = collectArtifacts([messageWithToolArtifact()], null, "conversation-one")[0];
    const second = collectArtifacts([messageWithToolArtifact()], null, "conversation-two")[0];

    expect(first).toMatchObject({ artifactId: "artifact-browser-shot", conversationId: "conversation-one" });
    expect(second).toMatchObject({ artifactId: "artifact-browser-shot", conversationId: "conversation-two" });
    expect(first.conversationId).not.toBe(second.conversationId);
  });
});
