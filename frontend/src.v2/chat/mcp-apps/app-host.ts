import { AppBridge, type McpUiHostContext, type McpUiStyles } from "@modelcontextprotocol/ext-apps/app-bridge";
import { parseJSONRPCMessage, type JSONRPCMessage, type ListToolsResult, type Transport } from "@modelcontextprotocol/client";
import { z } from "zod/v4";
import { apiBase, authHeaders, fetchWithTimeout } from "../../protocol/api";
import type { McpAppReceipt } from "../../protocol/mcp-app-types";

export interface McpAppView {
  viewId: string;
  frameUrl: string;
  frameOrigin: string;
  mimeType: string;
  prefersBorder: boolean;
  readOnly: boolean;
  toolInput: Record<string, unknown>;
  toolResult: Parameters<AppBridge["sendToolResult"]>[0];
  toolDefinition: NonNullable<McpUiHostContext["toolInfo"]>["tool"];
  widgetState: unknown;
}

export async function appHostRequest<T>(appId: string, operation: "open" | "request" | "close", body: object): Promise<T> {
  const response = await fetchWithTimeout(`${apiBase()}/api/mcp/apps/${encodeURIComponent(appId)}/${operation}`, {
    method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(body),
  }, { timeoutMs: 30_000 });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail?.message || `MCP App request returned HTTP ${response.status}`);
  }
  return await response.json() as T;
}

/** The frame's Window and its exact isolated origin jointly own this channel. */
export class AppFrameTransport implements Transport {
  onmessage?: (message: JSONRPCMessage) => void;
  onclose?: () => void;
  onerror?: (error: Error) => void;
  constructor(private frame: HTMLIFrameElement, private origin: string) {}
  private receive = (event: MessageEvent) => {
    if (event.source !== this.frame.contentWindow || event.origin !== this.origin) return;
    try { this.onmessage?.(parseJSONRPCMessage(event.data)); }
    catch (error) { this.onerror?.(error as Error); }
  };
  async start() { window.addEventListener("message", this.receive); }
  async send(message: JSONRPCMessage) { this.frame.contentWindow!.postMessage(message, this.origin); }
  async close() { window.removeEventListener("message", this.receive); this.onclose?.(); }
}

export function appHostContext(view: McpAppView, container: HTMLElement, fullscreen: boolean): McpUiHostContext {
  const style = getComputedStyle(document.documentElement);
  return {
    toolInfo: { tool: view.toolDefinition },
    theme: document.documentElement.dataset.theme === "light" ? "light" : "dark",
    locale: document.documentElement.lang || navigator.language,
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    platform: "desktop", userAgent: "MiniCode",
    displayMode: fullscreen ? "fullscreen" : "inline", availableDisplayModes: ["inline", "fullscreen"],
    containerDimensions: fullscreen ? { width: container.clientWidth, height: container.clientHeight }
      : { width: container.clientWidth, maxHeight: 600 },
    styles: { variables: {
      "--color-background-primary": style.getPropertyValue("--surface-base").trim(),
      "--color-background-secondary": style.getPropertyValue("--surface-panel").trim(),
      "--color-text-primary": style.getPropertyValue("--text-primary").trim(),
      "--color-text-secondary": style.getPropertyValue("--text-secondary").trim(),
      "--color-border-primary": style.getPropertyValue("--border-default").trim(),
      "--font-sans": style.getPropertyValue("--font-sans").trim() || style.fontFamily,
    } as McpUiStyles },
  };
}

export { moveAppContainer } from "./app-container";

export function createAppBridge(app: McpAppReceipt, conversationId: string, view: McpAppView, context: McpUiHostContext) {
  const request = <T,>(method: string, params: object) => appHostRequest<T>(app.id, "request", {
    conversation_id: conversationId, view_id: view.viewId, method, params,
  });
  const bridge = new AppBridge(null, { name: "MiniCode", version: "0.2.0" }, {
    openLinks: {}, downloadFile: {}, serverTools: {}, serverResources: {}, logging: {},
    updateModelContext: view.readOnly ? undefined : { text: {}, image: {}, resource: {}, resourceLink: {}, structuredContent: {} },
    message: view.readOnly ? undefined : { text: {} }, sandbox: { permissions: {} },
  }, { hostContext: context });
  bridge.oncalltool = params => request("tools/call", params);
  bridge.setRequestHandler("tools/list", { params: z.object({ cursor: z.string().optional() }).optional() },
    params => request<ListToolsResult>("tools/list", params || {}));
  bridge.onreadresource = params => request("resources/read", params);
  bridge.onlistresources = params => request("resources/list", params || {});
  bridge.onlistresourcetemplates = params => request("resources/templates/list", params || {});
  bridge.onupdatemodelcontext = params => request("ui/update-model-context", params);
  bridge.setRequestHandler("ui/legacy/set-widget-state", { params: z.object({ state: z.unknown() }) },
    params => request<Record<string, unknown>>("ui/legacy/set-widget-state", params));
  return bridge;
}
