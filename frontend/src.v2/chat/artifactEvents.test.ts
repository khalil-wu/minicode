/* @vitest-environment jsdom */

import { describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";
import { handleArtifactEvent as handleArtifactEventImpl } from "./artifactEvents";
import {
  beginPreviewRequest,
  resetPreviewRequestScopesForTests,
  setPreviewRequestId,
} from "./previewRequestScope";

const ACTIVE_PREVIEW_REQUEST_ID = "preview-request-active";

vi.hoisted(() => {
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
});

const resetMessages = () => {
  resetPreviewRequestScopesForTests();
  useAppStore.setState({
    conversationId: "conv-active",
    messages: [],
    conversationMessages: {},
    conversationWorkbenchStates: {},
    isStreaming: false,
    previewArtifact: null,
    inspectorEntries: [],
    inspectorFocus: null,
  });
  const lease = beginPreviewRequest("conv-active");
  setPreviewRequestId(lease, ACTIVE_PREVIEW_REQUEST_ID);
};

const handleArtifactEvent = (event: ServerEvent): boolean =>
  handleArtifactEventImpl(
    (event as unknown as { conversation_id?: unknown }).conversation_id
      ? event
      : { ...(event as object), conversation_id: useAppStore.getState().conversationId } as never,
  );

describe("handleArtifactEvent", () => {
  it("projects image artifact content into the unified Preview panel", () => {
    resetMessages();

    expect(handleArtifactEvent({
      type: "artifact_content",
      artifact_id: "artifact-image-1",
      content: "AA==",
      preview: "Dimensions: 1x1",
      media_type: "image/png",
      url: "data:image/png;base64,AA==",
      request_id: ACTIVE_PREVIEW_REQUEST_ID,
    } as never)).toBe(true);

    expect(useAppStore.getState().previewArtifact).toMatchObject({
      artifactId: "artifact-image-1",
      url: "data:image/png;base64,AA==",
      mediaType: "image/png",
      source: "artifact",
    });
  });

  it("keeps legacy image-preview reads in the unified Preview panel", () => {
    resetMessages();

    expect(handleArtifactEvent({
      type: "artifact_content",
      artifact_id: "artifact-image-2",
      content: "AA==",
      preview: "Dimensions: 1x1",
      media_type: "image/png",
      url: "data:image/png;base64,AA==",
      purpose: "image_preview",
      request_id: ACTIVE_PREVIEW_REQUEST_ID,
    } as never)).toBe(true);

    expect(useAppStore.getState().previewArtifact).toMatchObject({
      artifactId: "artifact-image-2",
      url: "data:image/png;base64,AA==",
      mediaType: "image/png",
      source: "artifact",
    });
  });

  it("projects attachment reads into the file preview instead of leaving the app preview visible", () => {
    resetMessages();

    expect(handleArtifactEvent({
      type: "artifact_content",
      artifact_id: "artifact-pdf-1",
      content: "Extracted PDF text",
      preview: "PDF document",
      media_type: "application/pdf",
      name: "report.pdf",
      purpose: "attachment",
      is_attachment: true,
      request_id: ACTIVE_PREVIEW_REQUEST_ID,
    } as never)).toBe(true);

    expect(useAppStore.getState().previewArtifact).toMatchObject({
      artifactId: "artifact-pdf-1",
      content: "Extracted PDF text",
      mediaType: "application/pdf",
      name: "report.pdf",
      source: "attachment",
    });
  });

  it("ignores artifact content that does not match the active preview request", () => {
    resetMessages();

    expect(handleArtifactEvent({
      type: "artifact_content",
      artifact_id: "artifact-stale",
      content: "stale",
      media_type: "text/plain",
      request_id: "preview-request-stale",
    } as never)).toBe(true);

    expect(useAppStore.getState().previewArtifact).toBeNull();
  });

  it("adds citations to the matching assistant message", () => {
    resetMessages();
    useAppStore.setState({
      messages: [{
        id: "assistant-1",
        role: "assistant",
        content: "Answer with source.",
        artifacts: [],
        timestamp: 1,
      }],
    });

    expect(handleArtifactEvent({
      type: "citation.add",
      message_id: "assistant-1",
      source: "https://example.test/weather",
      url: "https://example.test/weather",
      title: "Weather source",
      label: "example.test",
      range: [0, 0],
    } as never)).toBe(true);

    expect(useAppStore.getState().messages[0].citations).toEqual([{
      source: "https://example.test/weather",
      url: "https://example.test/weather",
      title: "Weather source",
      label: "example.test",
      range: [0, 0],
    }]);
  });

  it("does not fall back when an explicit citation message id is stale", () => {
    resetMessages();
    useAppStore.setState({
      messages: [{
        id: "local-streaming-assistant",
        role: "assistant",
        content: "",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
      isStreaming: true,
    });

    expect(handleArtifactEvent({
      type: "citation.add",
      message_id: "backend-stream-id",
      source: "https://example.test/source",
      url: "https://example.test/source",
      title: "Source",
      label: "example.test",
      range: [0, 0],
    } as never)).toBe(true);

    expect(useAppStore.getState().messages[0].citations).toBeUndefined();
    const diagnostic = useAppStore.getState().inspectorEntries[0];
    expect(diagnostic).toMatchObject({
      targetKind: "message",
      targetId: "backend-stream-id",
      payload: expect.objectContaining({
        projected: false,
        projection_reason: "assistant_message_not_found",
        source_available: true,
        url_available: true,
        source_characters: "https://example.test/source".length,
        url_characters: "https://example.test/source".length,
      }),
    });
    expect(diagnostic?.payload).not.toHaveProperty("source");
    expect(diagnostic?.payload).not.toHaveProperty("url");
  });

  it("records an unowned citation without retaining its source or URL", () => {
    resetMessages();

    expect(handleArtifactEventImpl({
      type: "citation.add",
      message_id: "assistant-unowned",
      source: "https://private.example/source",
      url: "https://private.example/source",
      title: "Private source",
      label: "private.example",
      range: [2, 4],
    } as never)).toBe(true);

    const diagnostic = useAppStore.getState().inspectorEntries[0];
    expect(diagnostic).toMatchObject({
      targetKind: "message",
      targetId: "assistant-unowned",
      payload: expect.objectContaining({
        projection_reason: "missing_conversation_owner",
        source_available: true,
        url_available: true,
        range: [2, 4],
      }),
    });
    expect(diagnostic?.payload).not.toHaveProperty("source");
    expect(diagnostic?.payload).not.toHaveProperty("url");
  });

  it("falls back to the only streaming assistant when citation message id is absent", () => {
    resetMessages();
    useAppStore.setState({
      messages: [{
        id: "local-streaming-assistant",
        role: "assistant",
        content: "",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
      isStreaming: true,
    });

    expect(handleArtifactEvent({
      type: "citation.add",
      source: "https://example.test/source",
      url: "https://example.test/source",
      title: "Source",
      label: "example.test",
      range: [0, 0],
    } as never)).toBe(true);

    expect(useAppStore.getState().messages[0].citations?.[0]?.url).toBe("https://example.test/source");
  });

  it("binds artifact previews to message_id before any other streaming assistant", () => {
    resetMessages();
    useAppStore.setState({
      messages: [
        {
          id: "assistant-streaming-other",
          role: "assistant",
          content: "",
          artifacts: [],
          timestamp: 1,
          isStreaming: true,
        },
        {
          id: "assistant-exact",
          role: "assistant",
          content: "Generated a chart.",
          artifacts: [],
          timestamp: 2,
        },
      ],
    });

    expect(handleArtifactEvent({
      type: "artifact.preview",
      message_id: "assistant-exact",
      artifact_id: "artifact-chart",
      kind: "image",
      summary: "Generated PNG chart",
      bytes: 8,
      media_type: "image/png",
      text_offset: 27,
    } as never)).toBe(true);

    const messages = useAppStore.getState().messages;
    expect(messages[0].artifacts).toEqual([]);
    expect(messages[1].artifacts).toEqual([
      expect.objectContaining({
        artifactId: "artifact-chart",
        kind: "image",
        summary: "Generated PNG chart",
        textOffset: 27,
      }),
    ]);
  });

  it("does not attach an artifact when an explicit message_id is stale", () => {
    resetMessages();
    useAppStore.setState({
      messages: [{
        id: "assistant-local-stream",
        role: "assistant",
        content: "",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
    });

    expect(handleArtifactEvent({
      type: "artifact.preview",
      message_id: "assistant-backend-stale",
      artifact_id: "artifact-fallback",
      kind: "json",
      summary: "Structured result",
      media_type: "application/json",
    } as never)).toBe(true);

    expect(useAppStore.getState().messages[0].artifacts).toEqual([]);
    expect(useAppStore.getState().inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "artifact",
        targetId: "artifact-fallback",
        payload: expect.objectContaining({
          projected: false,
          message_id: "assistant-backend-stale",
        }),
      }),
    ]);
  });

  it("falls back to the only streaming assistant when artifact message_id is absent", () => {
    resetMessages();
    useAppStore.setState({
      messages: [{
        id: "assistant-local-stream",
        role: "assistant",
        content: "",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
    });

    expect(handleArtifactEvent({
      type: "artifact.preview",
      artifact_id: "artifact-fallback",
      kind: "json",
      summary: "Structured result",
      media_type: "application/json",
    } as never)).toBe(true);

    expect(useAppStore.getState().messages[0].artifacts).toEqual([
      expect.objectContaining({ artifactId: "artifact-fallback", kind: "json" }),
    ]);
  });

  it("updates an existing artifact_id instead of appending a duplicate", () => {
    resetMessages();
    useAppStore.setState({
      messages: [{
        id: "assistant-1",
        role: "assistant",
        content: "",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
    });

    const first = {
      type: "artifact.preview",
      message_id: "assistant-1",
      artifact_id: "artifact-stable",
      kind: "image",
      summary: "Generating image",
      bytes: 8,
      media_type: "image/png",
      text_offset: 11,
    } as const;
    expect(handleArtifactEvent(first as never)).toBe(true);
    expect(handleArtifactEvent({
      ...first,
      summary: "Generated image",
      bytes: 12,
    } as never)).toBe(true);

    expect(useAppStore.getState().messages[0].artifacts).toEqual([
      expect.objectContaining({
        artifactId: "artifact-stable",
        summary: "Generated image",
        bytes: 12,
        textOffset: 11,
      }),
    ]);
  });

  it("projects inactive conversation artifacts only into their owner cache", () => {
    resetMessages();
    useAppStore.setState({
      conversationId: "conv-active",
      messages: [{ id: "assistant-active", role: "assistant", content: "Active", artifacts: [], timestamp: 1 }],
      conversationMessages: {
        "conv-other": [{ id: "assistant-other", role: "assistant", content: "Other", artifacts: [], timestamp: 2 }],
      },
    });

    expect(handleArtifactEventImpl({
      type: "artifact.preview",
      conversation_id: "conv-other",
      message_id: "assistant-other",
      artifact_id: "artifact-other",
      kind: "file",
      summary: "Generated report",
      bytes: 120,
      media_type: "text/markdown",
    } as never)).toBe(true);

    expect(useAppStore.getState().messages[0].artifacts).toEqual([]);
    expect(useAppStore.getState().conversationMessages["conv-other"]?.[0]?.artifacts).toEqual([
      expect.objectContaining({ artifactId: "artifact-other", summary: "Generated report" }),
    ]);
  });

  it("records safe metadata when no artifact target exists without copying a data URL", () => {
    resetMessages();
    const largeUrl = `data:image/png;base64,${"A".repeat(20_000)}`;

    expect(handleArtifactEvent({
      type: "artifact.preview",
      message_id: "assistant-missing",
      artifact_id: "artifact-unprojected",
      kind: "image",
      summary: "Generated image",
      bytes: 15_000,
      media_type: "image/png",
      url: largeUrl,
    } as never)).toBe(true);

    expect(useAppStore.getState().messages).toEqual([]);
    expect(useAppStore.getState().inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "artifact",
        targetId: "artifact-unprojected",
        payload: expect.objectContaining({
          conversation_id: "conv-active",
          message_id: "assistant-missing",
          url_available: true,
          url_characters: largeUrl.length,
          projected: false,
        }),
      }),
    ]);
    expect(useAppStore.getState().inspectorEntries[0]?.payload).not.toHaveProperty("url");
  });
});
