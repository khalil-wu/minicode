export const mcpServerNeedsAttention = (server: { status?: string; phase?: string }): boolean =>
  ["error", "failed", "auth_required", "expired"].includes(server.phase || server.status || "");
