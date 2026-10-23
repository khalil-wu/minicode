/* @vitest-environment jsdom */
import { afterEach, describe, expect, it } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import type { ChatMessage } from "../stores/types";
import { useAppStore } from "../stores";
import { collectLiveArtifacts, LiveArtifacts } from "./LiveArtifacts";

afterEach(cleanup);

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
  it("does not collect a closed gallery across 32 frames and reads the current history when opened", () => {
    let reads = 0;
    const messages = Array.from({ length: 256 }, (_, index) => {
      const entry = message({ id: `gallery-perf-${index}`, blocks: [] });
      Object.defineProperty(entry, "artifacts", { get: () => { reads += 1; return []; } });
      return entry;
    });
    useAppStore.setState({ liveArtifactsOpen: false, conversationId: "gallery-perf-owner", messages });
    render(createElement(LiveArtifacts));
    for (let index = 0; index < 32; index++) act(() => {
      useAppStore.setState({ messages: [...messages, message({ id: "gallery-tail", content: `frame-${index}`, blocks: [] })] });
    });
    console.info("PERF", JSON.stringify({ scenario: "closed-artifact-gallery", historyMessages: 256, frames: 32, closedArtifactReads: reads }));
    expect(reads).toBe(0);
    act(() => useAppStore.setState({ liveArtifactsOpen: true }));
    expect(reads).toBeGreaterThanOrEqual(256);
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("does not retain an empty execution-output filter in the normal gallery", () => {
    useAppStore.setState({ liveArtifactsOpen: true, conversationId: "owner", messages: [], isConnected: false });
    render(createElement(LiveArtifacts));
    fireEvent.click(screen.getByRole("button", { name: /^实时制品类型，当前：/ }));
    expect(screen.getByRole("listbox", { name: "实时制品类型", exact: true })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "执行结果", exact: true })).toBeNull();
    expect(screen.getByRole("option", { name: "全部类型", exact: true })).toBeTruthy();
  });
  it.each(["Script completed", "Script yielded", "Script running", "Script failed", "Script cancelled"])(
    "keeps the producer status %s in tool evidence instead of the normal gallery",
    (summary) => {
      const messages = [message({
        artifacts: [{ artifactId: "execution-output", kind: "text", summary }],
        blocks: [{ type: "tool_call", record: {
          id: "execution-call", name: "tool_wait", args: {}, status: "success", artifactId: "execution-output",
          artifactKind: "text", displaySummary: summary, startedAt: 100,
        } }],
      })];
      const original = JSON.stringify(messages);
      expect(collectLiveArtifacts(messages, "conversation-execution")).toEqual([]);
      expect(JSON.stringify(messages)).toBe(original);
    },
  );

  it("does not promote orchestration text to a file because its summary resembles a filename", () => {
    expect(collectLiveArtifacts([message({
      artifacts: [{ artifactId: "execution-output", kind: "text", summary: "Script completed.txt" }],
      blocks: [{ type: "tool_call", record: {
        id: "execution-call", name: "tool_exec", args: {}, status: "success", artifactId: "execution-output", displaySummary: "Script completed", startedAt: 100,
      } }],
    })], "conversation-execution")).toEqual([]);
  });

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
