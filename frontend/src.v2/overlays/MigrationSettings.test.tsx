// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MigrationSettings } from "./MigrationSettings";
const mocks = vi.hoisted(() => ({ fetch: vi.fn(), send: vi.fn() }));
vi.mock("../stores", () => ({ useAppStore: Object.assign((selector: (state: { workingDirectory: string }) => unknown) => selector({ workingDirectory: "/audit" }), { getState: () => ({}) }) }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "owned-session" }) }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: mocks.send }));
vi.mock("../protocol/api", () => ({ apiBase: () => "http://audit.test", authHeaders: () => ({}), fetchWithTimeout: mocks.fetch, errorMessageFromResponseText: (text: string) => text }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
it("reports missing original files while retaining the imported conversations", async () => {
  mocks.fetch.mockResolvedValue(new Response(JSON.stringify({ count: 1, unavailable_artifacts: [{ conversation_id: "new", artifact_id: "missing", reason: "missing_bytes" }] })));
  const { container } = render(<MigrationSettings />);
  const file = new File(["fixture"], "conversation.json", { type: "application/json" });
  Object.defineProperty(file, "text", { value: async () => JSON.stringify({ schema: "minicode.conversation.export", version: 1, conversations: [{ title: "保留历史" }] }) });
  fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [file] } });
  fireEvent.click(await screen.findByRole("button", { name: "确认导入" }));
  const result = await screen.findByRole("status");
  expect(result.textContent).toContain("已导入 1 个新会话");
  expect(result.textContent).toContain("1 个附件或文件引用无法读取原文件，请重新上传");
  expect(mocks.send).toHaveBeenCalledWith({ type: "conversation.list" }, { silent: true });
});
