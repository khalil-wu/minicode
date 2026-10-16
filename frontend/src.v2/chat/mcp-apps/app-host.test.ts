/* @vitest-environment jsdom */
import { expect, it, vi } from "vitest";
import { AppFrameTransport } from "./app-host";
import { reduceToolCallResult, reduceToolCallStart } from "../../lib/tool-call-reducer";
import { toToolCallRecord } from "../transcriptHydration";
import { mergeResumeToolCalls } from "../../stores/shared-helpers";
import type { McpAppReceipt } from "../../protocol/mcp-app-types";

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
