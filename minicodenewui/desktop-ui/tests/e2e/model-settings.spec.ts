import { expect, Page, test } from "@playwright/test";

// The composer draft on Home owns its model selection and reads it over HTTP;
// the mock WebSocket alone leaves currentModel empty, so answer those requests here.
async function mockDraftHttp(page: Page) {
  const model = "deepseek-v4-flash";
  const selection = {
    provider: "custom", current_model: model, model, available_models: [model], models_source: "configured",
    available_model_options: [{ model, reasoning_effort_levels: ["low", "medium", "high"], default_reasoning_effort: "medium" }],
    effective_reasoning_effort: "medium", model_selection_mode: "default",
    default_model_selection: { provider: "custom", model, reasoning_effort: "medium", conversation_id: "", working_directory: "" },
    available_permission_modes: ["confirm", "auto", "bypass"], feature_flags: {}, composer_commands: [],
    config_version: "e2e-config", config_cwd: "",
  };
  await page.route("**/api/llm/draft-selection**", route => route.fulfill({ json: selection }));
  await page.route("**/api/thread-metadata/sections**", route => route.fulfill({ json: { sections: [] } }));
}

async function mockRuntime(page: Page) {
  await page.addInitScript(() => {
    class MockWebSocket extends EventTarget {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSING = 2;
      static CLOSED = 3;
      CONNECTING = 0;
      OPEN = 1;
      CLOSING = 2;
      CLOSED = 3;

      readyState = 1;
      url: string;
      protocol = "";
      extensions = "";
      bufferedAmount = 0;
      binaryType: BinaryType = "blob";
      onopen: ((ev: Event) => void) | null = null;
      onclose: ((ev: CloseEvent) => void) | null = null;
      onmessage: ((ev: MessageEvent) => void) | null = null;
      onerror: ((ev: Event) => void) | null = null;

      constructor(url: string) {
        super();
        this.url = url;
        (window as any).__mockWs = this;
        setTimeout(() => {
          const ev = new Event("open");
          this.onopen?.(ev);
          this.dispatchEvent(ev);
          this._receive({ type: "conversation.list", conversations: [], active_conversation_id: null });
          this._receive({
            type: "llm.model.updated",
            model: "deepseek-v4-flash",
            current_model: "deepseek-v4-flash",
            available_models: ["deepseek-v4-flash"],
          });
        }, 20);
      }

      send(data: string) {
        const parsed = JSON.parse(data);
        if (parsed.type === "session.sync" || parsed.type === "session.restore") {
          // The renderer only reports itself connected once the session
          // handshake is answered; this mirrors the canonical empty-session
          // snapshot (test/initial-session-sync.ts).
          const restore = parsed.type === "session.restore";
          const session = { active_conversation_id: null, active_stream_conversation_ids: [], selected_model: "deepseek-v4-flash", workspace_root: "" };
          setTimeout(() => {
            this._receive({
              type: restore ? "session.restored" : "session.synced",
              session_id: "e2e-session",
              synced: true,
              client_command_id: parsed.client_command_id,
              active_conversation_id: null,
              ...(restore ? { conversation: null, messages: [] } : {}),
              current_model: "deepseek-v4-flash",
              model: "deepseek-v4-flash",
              current_seq: 0,
              last_seq: 0,
              replayed_events: 0,
              event_log_gap: false,
              snapshot_required: false,
              cursor_reset: false,
              requested_last_seq: 0,
              session,
            });
          }, 0);
          return;
        }
      }
      close() {
        this.readyState = 3;
      }
      _receive(data: object) {
        const ev = new MessageEvent("message", { data: JSON.stringify(data) });
        this.onmessage?.(ev);
        this.dispatchEvent(ev);
      }
    }

    const originalFetch = window.fetch.bind(window);
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/llm/settings")) {
        return new Response(JSON.stringify({
          provider: "custom",
          active_model: "deepseek-v4-flash",
          openai: { has_api_key: true, base_url: "https://lucen.cc/v1", model: "gpt-5.4", available_models: ["gpt-5.4"], wire_api: "responses" },
          anthropic: { has_api_key: false, base_url: "", model: "claude-sonnet-4-6", available_models: ["claude-sonnet-4-6"] },
          custom: { has_api_key: false, base_url: "https://api.deepseek.com/v1", model: "deepseek-v4-flash", available_models: ["deepseek-v4-flash"], wire_api: "chat" },
          provider_history: [{
            provider: "custom",
            provider_id: "deepseek",
            display_name: "DeepSeek",
            has_api_key: false,
            base_url: "https://api.deepseek.com/v1",
            model: "deepseek-v4-flash",
            available_models: ["deepseek-v4-flash"],
            wire_api: "chat",
          }],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return originalFetch(input, init);
    };

    (window as any).WebSocket = MockWebSocket;
  });
}

async function openApp(page: Page) {
  await mockDraftHttp(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => Boolean((window as any).__mockWs));
  await expect(page.locator('textarea, input[placeholder*="message" i], [contenteditable="true"]').first()).toBeVisible();
  await expect.poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().isConnected)).toBe(true);
  await expect.poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().currentModel || "")).not.toBe("");
}

test.describe("Model settings diagnostics", () => {
  test.beforeEach(async ({ page }) => {
    await mockRuntime(page);
    await openApp(page);
  });

  test("shows provider settings without a generation check", async ({ page }) => {
    const checks: string[] = [];
    page.on("request", (request) => { if (request.url().includes("/api/llm/check")) checks.push(request.url()); });
    await page.keyboard.press("Control+,");
    await expect(page.getByRole("heading", { name: "常规" })).toBeVisible();
    await page.getByRole("button", { name: "模型", exact: true }).click();
    await page.getByRole("button", { name: "编辑 DeepSeek" }).click();
    await expect(page.getByRole("textbox", { name: "接口地址" })).toHaveValue("https://api.deepseek.com/v1");
    await expect(page.getByPlaceholder("API 密钥")).toBeVisible();
    await expect(page.getByRole("button", { name: "鉴权" })).toHaveCount(0);
    expect(checks).toEqual([]);
  });
});
