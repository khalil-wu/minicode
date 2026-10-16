// Legacy text/html+skybridge uses the same isolated host bridge as MCP Apps.
(() => {
  let sequence = 0;
  const requests = new Map();
  const globals = { theme: "light", locale: navigator.language, ...initialGlobals };
  function request(method, params) {
    const id = `skybridge-${++sequence}`;
    return new Promise((resolve, reject) => {
      requests.set(id, { resolve, reject });
      window.parent.postMessage({ jsonrpc: "2.0", id, method, params }, "*");
    });
  }
  function update(values) {
    Object.assign(globals, values);
    window.dispatchEvent(new CustomEvent("openai:set_globals", { detail: { globals: values } }));
  }
  window.addEventListener("message", (event) => {
    if (event.source !== window.parent || event.data?.jsonrpc !== "2.0") return;
    const message = event.data;
    if (message.id != null && requests.has(message.id)) {
      const pending = requests.get(message.id);
      requests.delete(message.id);
      if (message.error) pending.reject(Object.assign(new Error(message.error.message), message.error));
      else pending.resolve(message.result);
      return;
    }
    if (message.method === "ui/notifications/tool-input") update({ toolInput: message.params.arguments });
    if (message.method === "ui/notifications/tool-result") update({ toolOutput: message.params.structuredContent,
      toolResponseMetadata: message.params._meta || {} });
    if (message.method === "ui/notifications/host-context-changed") update(message.params);
    if (message.method === "ui/resource-teardown") {
      window.parent.postMessage({ jsonrpc: "2.0", id: message.id, result: {} }, "*");
    }
  });
  const openai = {
    callTool: (name, args = {}) => request("tools/call", { name, arguments: args }),
    sendFollowUpMessage: ({ prompt }) => request("ui/message", { role: "user", content: [{ type: "text", text: prompt }] }),
    openExternal: ({ href }) => request("ui/open-link", { url: href }),
    requestDisplayMode: ({ mode }) => request("ui/request-display-mode", { mode }),
    requestClose: () => window.parent.postMessage({ jsonrpc: "2.0", method: "ui/notifications/request-teardown", params: {} }, "*"),
    setWidgetState: async (state) => {
      await request("ui/legacy/set-widget-state", { state });
      update({ widgetState: state });
    },
    notifyIntrinsicHeight: (height) => window.parent.postMessage({ jsonrpc: "2.0", method: "ui/notifications/size-changed", params: { height } }, "*"),
  };
  for (const key of ["toolInput", "toolOutput", "toolResponseMetadata", "widgetState", "displayMode", "theme", "locale", "maxHeight", "safeArea", "userAgent"]) {
    Object.defineProperty(openai, key, { get: () => globals[key], enumerable: true });
  }
  Object.defineProperty(window, "openai", { value: openai });
  request("ui/initialize", { appInfo: { name: "Skybridge App", version: "1" }, appCapabilities: {}, protocolVersion: "2026-01-26" })
    .then((result) => {
      update(result.hostContext);
      window.parent.postMessage({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} }, "*");
    });
})();
