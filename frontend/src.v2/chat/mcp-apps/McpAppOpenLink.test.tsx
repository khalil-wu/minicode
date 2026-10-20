/* @vitest-environment jsdom */
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { McpAppRenderer } from "./McpAppRenderer";
import type { McpAppReceipt } from "../../protocol/mcp-app-types";

const mocks = vi.hoisted(() => ({ confirm: vi.fn(), external: vi.fn(), request: vi.fn(), prepare: vi.fn(), release: vi.fn(),
  bridge: {} as { onopenlink?: (request: { url: string }) => Promise<{ isError: boolean }>; [key: string]: unknown } }));
vi.mock("../../desktop/runtime", () => ({ isDesktop: () => true, openExternal: mocks.external,
  desktop: () => ({ mcpAppSandbox: { prepare: mocks.prepare, release: mocks.release } }) }));
vi.mock("../../overlays/DialogService", () => ({ showConfirm: mocks.confirm }));
vi.mock("./app-host", () => ({ appHostRequest: mocks.request, appHostContext: () => ({}), createAppBridge: () => mocks.bridge,
  AppWebviewTransport: class {}, AppFrameTransport: class {}, moveAppContainer: vi.fn() }));

const app: McpAppReceipt = { id: "fixture-app", title: "Fixture App", server: "fixture", tool: "fixture",
  resourceUri: "ui://fixture/view", displayMode: "inline", readOnly: false };
beforeEach(() => {
  vi.clearAllMocks();
  mocks.confirm.mockResolvedValue(true);
  mocks.external.mockResolvedValue(true);
  mocks.prepare.mockResolvedValue({ id: "sandbox", src: "minicode-mcp-host://view/fixture", partition: "persist:minicode-mcp-apps" });
  mocks.release.mockResolvedValue(undefined);
  mocks.request.mockImplementation(async (_app, operation) => operation === "open" ? { viewId: "view", frameUrl: "http://mcp-app-fixture.localhost:8000/mcp-app/frame/fixture",
    frameOrigin: "http://mcp-app-fixture.localhost:8000", readOnly: false, prefersBorder: false, toolInput: {}, toolResult: {}, toolDefinition: { name: "fixture" } } : {});
  mocks.bridge = { connect: vi.fn(async () => {}), teardownResource: vi.fn(async () => {}), close: vi.fn(async () => {}),
    setHostContext: vi.fn(), sendToolInput: vi.fn(async () => {}), sendToolResult: vi.fn(async () => {}) };
});
afterEach(async () => { cleanup(); await Promise.resolve(); await Promise.resolve(); });

const openLink = async (url: string) => {
  render(<McpAppRenderer app={app} conversationId="owner" />);
  await waitFor(() => expect(mocks.bridge.onopenlink).toBeTypeOf("function"));
  return mocks.bridge.onopenlink!({ url });
};

it.each(["http://localhost:8080/", "mailto:user@example.com", "codex://threads/fixture", "codex-dev://threads/fixture"])(
  "rejects unsupported MCP link %s before consent or an OS action", async url => {
    expect(await openLink(url)).toEqual({ isError: true });
    expect(mocks.confirm).not.toHaveBeenCalled();
    expect(mocks.external).not.toHaveBeenCalled();
  });

it("opens an HTTPS MCP link only after the visible consent and returns the native receipt", async () => {
  const url = "https://example.com/docs?from=app";
  expect(await openLink(url)).toEqual({ isError: false });
  expect(mocks.confirm).toHaveBeenCalledWith({ title: "Fixture App", message: `打开此链接？\n${url}`, confirmLabel: "打开" });
  expect(mocks.external).toHaveBeenCalledWith(url);
  expect(mocks.confirm.mock.invocationCallOrder[0]).toBeLessThan(mocks.external.mock.invocationCallOrder[0]);
});

it("keeps a denied HTTPS request from reaching the native opener", async () => {
  mocks.confirm.mockResolvedValue(false);
  expect(await openLink("https://example.com/denied")).toEqual({ isError: true });
  expect(mocks.external).not.toHaveBeenCalled();
});

it("returns a rejected native receipt as an actual MCP link failure", async () => {
  mocks.external.mockResolvedValue(false);
  expect(await openLink("https://example.com/rejected")).toEqual({ isError: true });
});

it("preserves a native opener exception for the App RPC error response", async () => {
  mocks.external.mockRejectedValue(new Error("OS rejected the external URL"));
  await expect(openLink("https://example.com/error")).rejects.toThrow("OS rejected the external URL");
});
