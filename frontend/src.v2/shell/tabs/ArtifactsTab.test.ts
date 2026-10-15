/* @vitest-environment jsdom */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { createElement } from "react";
import type { ChatMessage } from "../../stores/types";
import { useAppStore } from "../../stores";
import { ArtifactsTab, collectArtifacts, mergeResourceInventory } from "./ArtifactsTab";
import { collectAttachments } from "../../chat/ChatContextCard";
import { hydrateMessages } from "../../chat/transcriptHydration";
import { collectLiveArtifacts } from "../../overlays/LiveArtifacts";

afterEach(cleanup);

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
  it("keeps the Files panel free of unsupported inventory filters", () => {
    useAppStore.setState({ conversationId: "owner", isConnected: false, messages: [], previewArtifact: null });
    render(createElement(ArtifactsTab));
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.queryByText("执行结果")).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
  });
  it("excludes persisted transport text while retaining real deliverables and same-id uploads across collectors", () => {
    const startedAt = 1790857935157;
    const messages = hydrateMessages([
      {
        id: "user-reference", role: "user", content: "检查这些文件。", timestamp: startedAt - 1,
        attachmentRefs: [{ id: "reference", artifactId: "output-first", name: "reference.txt", kind: "file", mediaType: "text/plain" }],
      },
      {
        id: "assistant-audit", role: "assistant", content: "检查已完成。", timestamp: startedAt,
        artifacts: [
          { artifactId: "output-first", kind: "file", summary: "Script completed" },
          { artifactId: "output-second", kind: "text", summary: "Script yielded" },
          { artifactId: "generated-image", kind: "image", summary: "生成图片", mediaType: "image/png" },
          { artifactId: "generated-pdf", kind: "file", summary: "审计报告.pdf", mediaType: "application/pdf" },
        ],
        tool_calls: [
          { id: "script-first", name: "tool_exec", args: {}, status: "success", artifact_id: "output-first", display_summary: "Script completed", started_at: startedAt,
            output_files: [{ path: "reports/audit.svg", name: "Script completed", size: 120, is_image: true }] },
          { id: "script-second", name: "tool_wait", args: {}, status: "success", artifact_id: "output-second", artifact_kind: "text", display_summary: "Script completed", started_at: startedAt + 26000 },
          { id: "image-call", name: "tool_exec", args: {}, status: "success", artifact_id: "generated-image", display_summary: "Script completed", started_at: startedAt + 30000 },
          { id: "pdf-call", name: "tool_exec", args: {}, status: "success", artifact_id: "generated-pdf", display_summary: "Script completed", started_at: startedAt + 32000 },
        ],
        reply_attachments: [{ path: "reports/audit.svg", size: 120, is_image: true }],
      },
    ]);
    const owner = "conversation-audit";
    const original = JSON.stringify(messages);
    const contextItems = collectAttachments(messages, owner);
    const sidebarItems = collectArtifacts(messages, null, owner);
    const galleryItems = collectLiveArtifacts(messages, owner);

    for (const items of [contextItems, sidebarItems, galleryItems]) {
      expect(items.filter((item) => item.executionResult)).toEqual([]);
      expect(items.some((item) => item.artifactId === "output-second")).toBe(false);
      expect(items.find((item) => item.artifactId === "generated-image")).toMatchObject({
        kind: "image", executionResult: false, conversationId: owner,
      });
      expect(items.find((item) => item.artifactId === "generated-pdf")).toMatchObject({
        kind: "file", mediaType: "application/pdf", executionResult: false, conversationId: owner,
      });
      expect(items.every((item) => item.conversationId === owner)).toBe(true);
    }
    expect(sidebarItems).toHaveLength(4);
    expect(sidebarItems.filter((item) => item.kind !== "attachment" && !item.executionResult)).toHaveLength(3);
    expect(sidebarItems.filter((item) => item.kind === "attachment")).toMatchObject([{ label: "reference.txt" }]);
    expect(sidebarItems.find((item) => item.path)).toMatchObject({ label: "audit.svg", path: "reports/audit.svg", kind: "image" });
    expect(contextItems.find((item) => item.artifactId === "output-first")).toMatchObject({ source: "attachment", label: "reference.txt" });
    expect(sidebarItems.find((item) => item.artifactId === "output-first")).toMatchObject({ kind: "attachment", label: "reference.txt" });
    expect(galleryItems.some((item) => item.artifactId === "output-first")).toBe(false);
    expect(galleryItems.find((item) => item.artifactId === "generated-image")?.summary).toBe("生成图片");

    const previewItems = collectArtifacts(messages, {
      artifactId: "output-first", source: "artifact", name: "Script completed", kind: "text", content: "测试执行输出", loadedAt: startedAt,
    }, owner);
    expect(previewItems.filter((item) => item.artifactId === "output-first")).toMatchObject([{ kind: "attachment", label: "reference.txt" }]);
    expect(JSON.stringify(messages)).toBe(original);
  });

  it("preserves a specific artifact title and does not classify a status title without its producer", () => {
    const messages = hydrateMessages([{
      id: "assistant-names", role: "assistant", content: "", timestamp: 100,
      artifacts: [
        { artifactId: "named-output", kind: "text", summary: "审计执行记录.txt" },
        { artifactId: "unowned-status", kind: "file", summary: "Script completed" },
      ],
      tool_calls: [{ id: "named-call", name: "tool_exec", args: {}, status: "success", artifact_id: "named-output", display_summary: "Script completed", started_at: 100 }],
    }]);
    const items = collectArtifacts(messages, null, "conversation-names");
    expect(items.some((item) => item.artifactId === "named-output")).toBe(false);
    expect(items.find((item) => item.artifactId === "unowned-status")).toMatchObject({ label: "Script completed", kind: "file" });
    expect(items.find((item) => item.artifactId === "unowned-status")?.executionResult).toBeUndefined();
  });

  it.each([
    ["image", "image/png", "", "diagram.png"],
    ["file", "application/pdf", "", "report.pdf"],
    ["file", "audio/wav", "", "voice.wav"],
    ["file", "text/plain", "file", "report.txt"],
  ])("retains actual %s/%s deliverables with execution producer metadata", (kind, mediaType, producerKind, summary) => {
    const messages = hydrateMessages([{
      id: "actual-deliverable", role: "assistant", content: "", timestamp: 100,
      artifacts: [{ artifactId: "real-file", kind, mediaType, summary }],
      tool_calls: [{ id: "producer", name: "tool_exec", args: {}, status: "success", artifact_id: "real-file",
        artifact_kind: producerKind, display_summary: "Script completed", started_at: 100 }],
    }]);
    const original = JSON.stringify(messages);
    for (const items of [collectAttachments(messages, "owner"), collectArtifacts(messages, null, "owner"), collectLiveArtifacts(messages, "owner")]) {
      expect(items).toHaveLength(1);
      expect(items[0]).toMatchObject({ artifactId: "real-file", kind, mediaType, conversationId: "owner" });
      expect(items[0].executionResult).not.toBe(true);
    }
    expect(JSON.stringify(messages)).toBe(original);
  });

  it("classifies richer artifact metadata across messages before excluding a sparse code record", () => {
    const messages = hydrateMessages([
      { id: "tool-record", role: "assistant", content: "", timestamp: 1,
        tool_calls: [{ id: "exec", name: "tool_wait", args: {}, status: "success", artifact_id: "later-image", started_at: 1 }] },
      { id: "image-preview", role: "assistant", content: "", timestamp: 2,
        artifacts: [{ artifactId: "later-image", kind: "image", mediaType: "image/webp", summary: "Generated image" }] },
    ]);
    for (const items of [collectAttachments(messages, "owner"), collectArtifacts(messages, null, "owner"), collectLiveArtifacts(messages, "owner")]) {
      expect(items).toHaveLength(1);
      expect(items[0]).toMatchObject({ artifactId: "later-image", kind: "image", mediaType: "image/webp" });
    }
  });

  it("keeps historical execution wrappers out of the normal resource inventory while preserving same-id uploads and richer images", () => {
    const base = { conversation_id: "owner", workspace_root: "/workspace", size_bytes: 100, occurred_at: 1, media_type: "text/plain", kind: "text", source: "artifact" as const, execution_result: true };
    const resources = [
      { ...base, id: "artifact:wrapper", artifact_id: "wrapper", name: "Code output" },
      { ...base, id: "attachment:wrapper", artifact_id: "wrapper", name: "user.txt", source: "attachment" as const, execution_result: false },
      { ...base, id: "artifact:image", artifact_id: "image", name: "Sparse image" },
    ];
    const original = JSON.stringify(resources);
    const items = mergeResourceInventory(collectArtifacts([messageWithToolArtifact({ blocks: [],
      artifacts: [{ artifactId: "image", kind: "image", mediaType: "image/png", summary: "diagram.png" }],
    })], null, "owner"), resources);
    expect(items).toHaveLength(2);
    expect(items.find((item) => item.artifactId === "wrapper")).toMatchObject({ kind: "attachment", label: "user.txt" });
    expect(items.find((item) => item.artifactId === "image")).toMatchObject({ kind: "image", label: "diagram.png", mediaType: "image/png" });
    expect(JSON.stringify(resources)).toBe(original);
  });

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
    const message = messageWithToolArtifact();
    const upload: ChatMessage = {
      id: "user-upload",
      role: "user",
      content: "Review this image",
      artifacts: [],
      timestamp: 99,
      attachmentRefs: [{
        id: "upload-1",
        artifactId: "artifact-browser-shot",
        name: "same-id.png",
        kind: "image",
        mediaType: "image/png",
      }],
    };

    const items = collectArtifacts([upload, message], null, "conversation-browser");
    expect(items).toHaveLength(2);
    expect(items.map((item) => item.id)).toContain("attachment:artifact-browser-shot");
    expect(items.map((item) => item.kind)).toContain("attachment");
  });

  it("classifies assistant reply files as generated output instead of user attachments", () => {
    const message = messageWithToolArtifact({
      blocks: [],
      replyAttachments: [{
        path: "C:/Desktop/MiniCode/output/diagram.svg",
        size: 640,
        isImage: true,
      }],
    });

    expect(collectArtifacts([message], null, "conversation-browser")).toMatchObject([{
      id: "workspace:C:/Desktop/MiniCode/output/diagram.svg",
      label: "diagram.svg",
      kind: "image",
      path: "C:/Desktop/MiniCode/output/diagram.svg",
      mediaType: "image/svg+xml",
      conversationId: "conversation-browser",
    }]);
  });

  it("keeps a workspace output on its path route when its preview is opened", () => {
    const message = messageWithToolArtifact({ blocks: [], replyAttachments: [{ path: "diagram.svg", size: 100, isImage: true }] });
    const items = collectArtifacts([message], {
      artifactId: "workspace:diagram.svg", source: "workspace",
      content: "", mediaType: "image/svg+xml", loadedAt: 2,
    }, "conversation-browser");
    expect(items).toHaveLength(1);
    expect(items[0].path).toBe("diagram.svg");
    expect(items[0].artifactId).toBeUndefined();
    expect(collectArtifacts([], { artifactId: "upload", source: "attachment", content: "", loadedAt: 2 })).toEqual([]);
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

  it("keeps the current preview first while retaining older artifacts for search and source navigation", () => {
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

    expect(items).toHaveLength(32);
    expect(items.find((item) => item.artifactId === "artifact-0")).toMatchObject({ messageId: "assistant-0", conversationId: "conversation-browser" });
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
