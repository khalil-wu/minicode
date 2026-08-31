import { describe, expect, it } from "vitest";
import type { ChatMessage } from "../stores/types";
import { collectLiveArtifacts } from "./LiveArtifacts";

const message = (overrides: Partial<ChatMessage> = {}): ChatMessage => ({
  id: "assistant-live",
  role: "assistant",
  content: "",
  timestamp: 100,
  artifacts: [],
  blocks: [{
    type: "tool_call",
    record: {
      id: "browser-live",
      name: "browser_control",
      args: { action: "screenshot" },
      status: "success",
      resultKind: "browser",
      artifactId: "shot-live",
      artifactKind: "browser_screenshot",
      artifactMediaType: "IMAGE/PNG; charset=binary",
      artifactBytes: 4096,
      displaySummary: "Browser screenshot",
      startedAt: 100,
      finishedAt: 101,
    },
  }],
  ...overrides,
});

describe("LiveArtifacts projection", () => {
  it("projects browser screenshots as owner-scoped images", () => {
    const [artifact] = collectLiveArtifacts([message()], "conversation-live");
    expect(artifact).toMatchObject({
      artifactId: "shot-live",
      kind: "image",
      mediaType: "image/png",
      summary: "Browser screenshot",
      conversationId: "conversation-live",
      url: undefined,
    });
  });

  it("merges a sparse transcript artifact with its tool record", () => {
    const [artifact] = collectLiveArtifacts([message({
      artifacts: [{
        artifactId: "shot-live",
        kind: "artifact" as never,
        summary: " ",
      }],
    })], "conversation-live");
    expect(artifact.kind).toBe("image");
    expect(artifact.mediaType).toBe("image/png");
    expect(artifact.bytes).toBe(4096);
    expect(artifact.url).toBeUndefined();
  });

  it("projects browser screenshots from legacy toolCalls when blocks are absent", () => {
    const legacyMessage = Object.assign(message({ blocks: undefined }), {
      toolCalls: [{
        id: "browser-legacy-live",
        name: "browser_control",
        args: { action: "screenshot" },
        status: "success",
        artifactId: "legacy-shot-live",
        artifactKind: "browser_screenshot",
        artifactMediaType: "image/png",
        displaySummary: "浏览器截图",
      }],
    }) as ChatMessage;

    const [artifact] = collectLiveArtifacts([legacyMessage], "conversation-live", "session-live");
    expect(artifact).toMatchObject({
      artifactId: "legacy-shot-live",
      kind: "image",
      mediaType: "image/png",
      summary: "浏览器截图",
    });
  });

  it("keeps an existing signed URL when the socket handle is unavailable", () => {
    const [artifact] = collectLiveArtifacts([message({
      artifacts: [{
        artifactId: "shot-live",
        kind: "image",
        summary: "saved",
        mediaType: "image/png",
        url: "https://assets.example/saved.png",
      }],
    })], "conversation-live", "");
    expect(artifact.url).toBe("https://assets.example/saved.png");
  });

  it("keeps the same artifact id isolated by the caller's conversation owner", () => {
    const first = collectLiveArtifacts([message()], "conversation-one")[0];
    const second = collectLiveArtifacts([message()], "conversation-two")[0];

    expect(first).toMatchObject({ artifactId: "shot-live", conversationId: "conversation-one" });
    expect(second).toMatchObject({ artifactId: "shot-live", conversationId: "conversation-two" });
    expect(first.conversationId).not.toBe(second.conversationId);
  });
});
