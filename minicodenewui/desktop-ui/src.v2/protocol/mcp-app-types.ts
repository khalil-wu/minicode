/** Issued only after a registered MCP tool invocation actually succeeds. */
export interface McpAppReceipt {
  id: string;
  server: string;
  tool: string;
  resourceUri: string;
  displayMode: "inline" | "fullscreen";
  title: string;
  readOnly: boolean;
}

export const isMcpAppReceipt = (value: unknown): value is McpAppReceipt => {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const app = value as Record<string, unknown>;
  return ["id", "server", "tool", "resourceUri", "title"].every(key => typeof app[key] === "string")
    && /^[a-f0-9]{32}$/.test(String(app.id))
    && ["inline", "fullscreen"].includes(String(app.displayMode)) && typeof app.readOnly === "boolean";
};
