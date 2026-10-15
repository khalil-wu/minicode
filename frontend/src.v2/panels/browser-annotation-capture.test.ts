// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ get: vi.fn(), capture: vi.fn(), upload: vi.fn() }));
vi.mock("../desktop/runtime", () => ({ getDesktopPreferences: mocks.get, embeddedBrowserCapture: mocks.capture }));
vi.mock("../protocol/api", () => ({ uploadAttachment: mocks.upload }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "actual-session-owner" }) }));
import { captureBrowserAnnotationAttachments } from "./browser-annotation-capture";

const png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aFfkAAAAASUVORK5CYII=";
const base = { conversationId: "original-owner", workspaceRoot: "C:/task", targetId: "native-target", url: "https://example.com/page", elements: [] };
beforeEach(() => {
  vi.resetAllMocks();
  mocks.get.mockResolvedValue({ annotationScreenshotsMode: "always" });
  mocks.capture.mockResolvedValue({ data: png, mimeType: "image/png", width: 1, height: 1, url: base.url });
  mocks.upload.mockResolvedValue({ conversation_id: "original-owner", file_name: "captured.png", artifact_id: "actual-upload-id", doc_id: "", attachment: { kind: "image", artifact_id: "actual-upload-id", media_type: "image/png", file_name: "captured.png" } });
});
it("uploads actual native bytes to the original owner and returns the corresponding sent and draft attachments", async () => {
  const result = await captureBrowserAnnotationAttachments(base);
  expect(mocks.capture).toHaveBeenCalledWith({ id: "native-target", conversationId: "original-owner", expectedUrl: base.url });
  expect(mocks.upload).toHaveBeenCalledWith("actual-session-owner", "original-owner", expect.any(File), { workspaceRoot: "C:/task" });
  const file = mocks.upload.mock.calls[0][2] as File;
  expect(file.type).toBe("image/png");
  expect(file.size).toBeGreaterThan(0);
  expect(result.attachments[0].artifact_id).toBe("actual-upload-id");
  expect(result.attachmentRefs[0]).toMatchObject({ artifactId: "actual-upload-id", dataUrl: `data:image/png;base64,${png}` });
  expect(result.composerAttachments[0]).toMatchObject({ conversationId: "original-owner", status: "ready", artifactId: "actual-upload-id" });
});
it("only includes a screenshot for a real drag region in necessary mode", async () => {
  mocks.get.mockResolvedValue({ annotationScreenshotsMode: "necessary" });
  await captureBrowserAnnotationAttachments({ ...base, elements: [{ selector: "#element", rect: { x: 1, y: 2, width: 3, height: 4 }, viewport: { width: 100, height: 100 } }] });
  expect(mocks.capture).not.toHaveBeenCalled();
  const region = { selector: "", rect: { x: 5, y: 6, width: 30, height: 40 }, viewport: { width: 100, height: 100 } };
  await captureBrowserAnnotationAttachments({ ...base, elements: [region] });
  expect(mocks.capture).toHaveBeenCalledWith(expect.objectContaining({ rect: region.rect, viewport: region.viewport }));
});
it("does not upload a fabricated screenshot after native capture fails", async () => {
  mocks.capture.mockRejectedValue(new Error("actual compositor failure"));
  await expect(captureBrowserAnnotationAttachments(base)).rejects.toThrow("actual compositor failure");
  expect(mocks.upload).not.toHaveBeenCalled();
});
it("rejects an upload belonging to another owner instead of fabricating a ready attachment", async () => {
  mocks.upload.mockResolvedValue({ conversation_id: "another-owner", attachment: {} });
  await expect(captureBrowserAnnotationAttachments(base)).rejects.toThrow("所属对话已变化");
});
