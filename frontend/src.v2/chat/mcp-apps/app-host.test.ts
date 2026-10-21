/* @vitest-environment jsdom */
import { expect, it, vi } from "vitest";
import { AppFrameTransport, AppWebviewTransport, createAppBridge, type McpAppView, type McpAppWebview } from "./app-host";
import { App } from "@modelcontextprotocol/ext-apps";
import { InMemoryTransport } from "@modelcontextprotocol/client";
import { reduceToolCallResult, reduceToolCallStart } from "../../lib/tool-call-reducer";
import { toToolCallRecord } from "../transcriptHydration";
import { mergeResumeToolCalls } from "../../stores/shared-helpers";
import type { McpAppReceipt } from "../../protocol/mcp-app-types";

it.each([false, true])("publishes the resource's enforced CSP in the actual SDK handshake (readOnly=%s)", async readOnly => {
  const receipt: McpAppReceipt = { id: "owned", server: "owned", tool: "counter", title: "Counter",
    resourceUri: "ui://owned/counter", displayMode: "inline", readOnly };
  const csp = { connectDomains: ["https://api.owned.example"], resourceDomains: ["https://cdn.owned.example"],
    frameDomains: ["https://frame.owned.example"], baseUriDomains: ["https://base.owned.example"] };
  const view: McpAppView = { viewId: "view", frameUrl: "http://mcp-app-owned.localhost/frame", frameOrigin: "http://mcp-app-owned.localhost",
    mimeType: "text/html;profile=mcp-app", prefersBorder: false, readOnly, csp, toolInput: {}, toolResult: { content: [] },
    toolDefinition: { name: "counter", inputSchema: { type: "object" } }, widgetState: null };
  const bridge = createAppBridge(receipt, "original-conversation", view, {});
  const app = new App({ name: "Owned counter", version: "1" }, {}, { autoResize: false });
  const [hostTransport, appTransport] = InMemoryTransport.createLinkedPair();
  try {
    await bridge.connect(hostTransport);
    await app.connect(appTransport);
    expect(app.getHostCapabilities()?.sandbox).toEqual({ permissions: {}, csp });
    expect(app.getHostCapabilities()?.message).toEqual(readOnly ? undefined : { text: {} });
  } finally {
    await app.close();
    await bridge.close();
  }
});

it("binds App RPC to the actual iframe Window and exact sandbox origin", async () => {
  const frame = document.createElement("iframe");
  const other = document.createElement("iframe");
  document.body.append(frame, other);
  const origin = "http://mcp-app-owned.localhost:8799";
  const transport = new AppFrameTransport(frame, origin);
  const received = vi.fn(); transport.onmessage = received;
  await transport.start();
  const data = { jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} };
  window.dispatchEvent(new MessageEvent("message", { data, source: frame.contentWindow, origin: "http://127.0.0.1:8799" }));
  window.dispatchEvent(new MessageEvent("message", { data, source: other.contentWindow, origin }));
  expect(received).not.toHaveBeenCalled();
  window.dispatchEvent(new MessageEvent("message", { data, source: frame.contentWindow, origin }));
  expect(received).toHaveBeenCalledTimes(1);
  expect(received).toHaveBeenCalledWith(data);
  await transport.close();
  window.dispatchEvent(new MessageEvent("message", { data, source: frame.contentWindow, origin }));
  expect(received).toHaveBeenCalledTimes(1);
  frame.remove(); other.remove();
});

it("keeps the registered App receipt through live results, history and reconnect", () => {
  const app: McpAppReceipt = { id: "a".repeat(32), server: "owned", tool: "counter", title: "Counter",
    resourceUri: "ui://owned/counter", displayMode: "inline", readOnly: false };
  const started = reduceToolCallStart(new Map(), { type: "tool_call", id: "actual", name: "mcp__owned__counter", args: {},
    mcp_app_resource_uri: app.resourceUri });
  const live = reduceToolCallResult(started, { type: "tool_result", id: "actual", summary: "Actual result", mcp_app: app }).get("actual")!;
  expect(live.mcpApp).toEqual(app);
  expect(toToolCallRecord({ ...live, mcpApp: undefined, mcp_app: app })?.mcpApp).toEqual(app);
  expect(mergeResumeToolCalls([], [{ id: "actual", name: live.name, args: {}, mcp_app: app }])[0]).toMatchObject({ record: { mcpApp: app } });
  expect(toToolCallRecord({ ...live, mcpApp: undefined, summary: JSON.stringify({ _meta: { ui: { resourceUri: app.resourceUri } } }) })?.mcpApp).toBeUndefined();
});

it("routes desktop App RPC through only its guest preload channel", async () => {
  const webview = document.createElement("webview") as McpAppWebview;
  webview.send = vi.fn();
  const transport = new AppWebviewTransport(webview);
  const received = vi.fn(), errors = vi.fn();
  transport.onmessage = received; transport.onerror = errors;
  await transport.start();
  const message = { jsonrpc: "2.0" as const, method: "ui/notifications/initialized", params: {} };
  const dispatch = (channel: string, value: unknown) => webview.dispatchEvent(Object.assign(new Event("ipc-message"), { channel, args: [value] }));
  dispatch("other-app:message", message);
  expect(received).not.toHaveBeenCalled();
  dispatch("minicode:mcpApp:message", message);
  expect(received).toHaveBeenCalledWith(message);
  dispatch("minicode:mcpApp:message", "invalid json-rpc");
  expect(errors).toHaveBeenCalledTimes(1);
  const delivery = transport.send(message);
  await Promise.resolve();
  expect(webview.send).not.toHaveBeenCalled();
  webview.dispatchEvent(new Event("dom-ready"));
  await delivery;
  expect(webview.send).toHaveBeenCalledWith("minicode:mcpApp:message", message);
  await transport.close();
  dispatch("minicode:mcpApp:message", message);
  expect(received).toHaveBeenCalledTimes(1);
});
