import { expect, Page, test } from "@playwright/test";

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

      send() {}
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
      if (url.includes("/api/llm/check")) {
        return new Response(JSON.stringify({
          ok: false,
          provider: "custom",
          provider_id: "deepseek",
          base_url: "https://api.deepseek.com/v1",
          model: "deepseek-v4-flash",
          wire_api: "chat",
          has_api_key: false,
          status_code: null,
          message: "Missing API key for current provider.",
          hint: "没有可用的 API key。请为当前 provider 保存 key，或切回已配置 key 的 provider。",
          models: [],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return originalFetch(input, init);
    };

    (window as any).WebSocket = MockWebSocket;
  });
}

async function openApp(page: Page) {
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

  test("shows provider/base URL/key diagnostics when auth check fails", async ({ page }) => {
    await page.keyboard.press("Control+,");
    await expect(page.getByRole("heading", { name: "常规" })).toBeVisible();
    await page.getByRole("button", { name: "模型", exact: true }).click();
    await page.getByRole("button", { name: "编辑 DeepSeek" }).click();
    await page.getByRole("button", { name: "鉴权" }).click();

    const settings = page.getByRole("main", { name: "设置" });
    await expect(page.getByText(/deepseek-v4-flash：Missing API key/)).toBeVisible();
    await expect(page.getByRole("textbox", { name: "接口地址" })).toHaveValue("https://api.deepseek.com/v1");
    await expect(page.getByPlaceholder("API 密钥")).toBeVisible();
    await expect(page.getByRole("button", { name: "重试" })).toBeVisible();
  });
});
