/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAppStore } from "../stores";
import {
  openAttachmentPreview,
  openLocalFilePreview,
} from "./openAttachmentPreview";
import { resetPreviewRequestScopesForTests } from "./previewRequestScope";

const mocks = vi.hoisted(() => ({
  fetchAttachmentPreview: vi.fn(),
  fetchWorkspaceFilePreview: vi.fn(),
}));

vi.mock("../hooks/useWebSocket", () => ({
  getWebSocket: () => ({ sessionId: "session-preview" }),
}));

vi.mock("../protocol/api", () => ({
  attachmentRawResourceUrlWithToken: () => "",
  fetchAttachmentPreview: (...args: unknown[]) => mocks.fetchAttachmentPreview(...args),
  workspaceRawResourceUrlWithToken: () => "",
}));

vi.mock("../protocol/workspace", () => ({
  fetchWorkspaceFilePreview: (...args: unknown[]) => mocks.fetchWorkspaceFilePreview(...args),
}));

vi.mock("../protocol/ws-outbox", () => ({
  commandResultSucceeded: () => true,
  createClientCommandId: () => "preview-command",
  sendClientCommandAwaitResult: vi.fn(),
}));

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
};

describe("attachment preview request generation", () => {
  beforeEach(() => {
    resetPreviewRequestScopesForTests();
    mocks.fetchAttachmentPreview.mockReset();
    mocks.fetchWorkspaceFilePreview.mockReset();
    useAppStore.setState({
      conversationId: "conv-preview",
      conversations: [],
      conversationWorkbenchStates: {},
      previewArtifact: null,
      panelSlots: [],
      rightPanelOpen: false,
    });
  });

  afterEach(() => {
    resetPreviewRequestScopesForTests();
    vi.unstubAllGlobals();
  });

  it("does not let a slow HTTP preview overwrite the newer selection", async () => {
    const first = deferred<Record<string, unknown>>();
    const second = deferred<Record<string, unknown>>();
    mocks.fetchAttachmentPreview.mockImplementation(
      (_sessionId: string, _conversationId: string, artifactId: string) => (
        artifactId === "artifact-a" ? first.promise : second.promise
      ),
    );

    expect(openAttachmentPreview({ artifactId: "artifact-a", conversationId: "conv-preview" })).toBe(true);
    expect(openAttachmentPreview({ artifactId: "artifact-b", conversationId: "conv-preview" })).toBe(true);
    const firstSignal = mocks.fetchAttachmentPreview.mock.calls[0]?.[3] as AbortSignal;
    expect(firstSignal.aborted).toBe(true);

    first.resolve({ artifact_id: "artifact-a", content: "stale", media_type: "text/plain" });
    await Promise.resolve();
    expect(useAppStore.getState().previewArtifact).toMatchObject({
      artifactId: "artifact-b",
      loading: true,
    });

    second.resolve({ artifact_id: "artifact-b", content: "current", media_type: "text/plain" });
    await vi.waitFor(() => {
      expect(useAppStore.getState().previewArtifact).toMatchObject({
        artifactId: "artifact-b",
        content: "current",
        loading: false,
      });
    });
  });

  it("does not let an older local File.text result overwrite a newer file", async () => {
    const first = deferred<string>();
    const second = deferred<string>();
    const firstFile = { type: "text/plain", size: 5, text: () => first.promise } as File;
    const secondFile = { type: "text/plain", size: 6, text: () => second.promise } as File;

    openLocalFilePreview({ id: "local-a", name: "a.txt", file: firstFile, conversationId: "conv-preview" });
    openLocalFilePreview({ id: "local-b", name: "b.txt", file: secondFile, conversationId: "conv-preview" });
    first.resolve("stale");
    await Promise.resolve();
    expect(useAppStore.getState().previewArtifact).toMatchObject({
      artifactId: "local:local-b",
      loading: true,
    });

    second.resolve("current");
    await vi.waitFor(() => {
      expect(useAppStore.getState().previewArtifact).toMatchObject({
        artifactId: "local:local-b",
        content: "current",
        loading: false,
      });
    });
  });

  it("keeps object URLs independent across conversation preview slots", () => {
    const createObjectURL = vi.fn()
      .mockReturnValueOnce("blob:conv-a-first")
      .mockReturnValueOnce("blob:conv-b")
      .mockReturnValueOnce("blob:conv-a-second");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });
    const imageFile = { type: "image/png", size: 1 } as File;

    openLocalFilePreview({ id: "a-1", name: "a.png", file: imageFile, conversationId: "conv-a" });
    openLocalFilePreview({ id: "b-1", name: "b.png", file: imageFile, conversationId: "conv-b" });
    expect(revokeObjectURL).not.toHaveBeenCalled();

    openLocalFilePreview({ id: "a-2", name: "a2.png", file: imageFile, conversationId: "conv-a" });
    expect(revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:conv-a-first");
  });
});
