import { expect, Page, test } from "@playwright/test";

async function mockWebSocket(page: Page) {
  await page.addInitScript(() => {
    const messages: string[] = [];

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
        (window as any).__mockWsMessages = messages;

        setTimeout(() => {
          const ev = new Event("open");
          this.onopen?.(ev);
          this.dispatchEvent(ev);
          this._receive({
            type: "conversation.list",
            conversations: [{ id: "conv-activity", title: "Activity", updated_at: "2026-07-11T00:00:00.000Z" }],
            active_conversation_id: "conv-activity",
            active_conversation: { id: "conv-activity", title: "Activity", updated_at: "2026-07-11T00:00:00.000Z", messages: [] },
          });
          this._receive({
            type: "llm.model.updated",
            model: "gpt-4.1",
            current_model: "gpt-4.1",
            available_models: ["gpt-4.1"],
            working_directory: "C:\\Desktop\\MiniCode",
          });
        }, 20);
      }

      send(data: string) {
        messages.push(data);
        const parsed = JSON.parse(data);
        if (parsed.client_command_id) {
          setTimeout(() => this._receive({
            type: "client.command.ack",
            client_command_id: parsed.client_command_id,
            command_type: parsed.type,
            accepted: true,
          }), 0);
        }
        if (parsed.type !== "user_message") return;

        setTimeout(() => {
          this._receive({ type: "thinking_delta", content: "我先读取 README。", conversation_id: "conv-activity", message_id: parsed.assistant_message_id });
          this._receive({
            type: "tool_call",
            id: "read-readme",
            name: "read_file",
            args: { path: "README.md" },
            started_at: Date.now() - 1200,
            input_summary: "README.md",
            conversation_id: "conv-activity",
            message_id: parsed.assistant_message_id,
          });
        }, 30);

        setTimeout(() => {
          this._receive({
            type: "tool_result",
            id: "read-readme",
            summary: "Read file: README.md",
            display_summary: "已读取 README.md",
            result_kind: "file",
            evidence_type: "file",
            extraction_status: "ok",
            duration_ms: 420,
            conversation_id: "conv-activity",
            message_id: parsed.assistant_message_id,
          });
          this._receive({
            type: "item.started",
            item: { id: "activity-answer", type: "agent_message" },
            conversation_id: "conv-activity",
            message_id: parsed.assistant_message_id,
          });
          this._receive({
            type: "agent_message.delta",
            item_id: "activity-answer",
            delta: "MiniCode 是一个本地 AI 编程工作台。",
            conversation_id: "conv-activity",
            message_id: parsed.assistant_message_id,
          });
          this._receive({
            type: "item.completed",
            item: { id: "activity-answer", type: "agent_message", text: "MiniCode 是一个本地 AI 编程工作台。", status: "completed" },
            conversation_id: "conv-activity",
            message_id: parsed.assistant_message_id,
          });
          this._receive({
            type: "done",
            status: "completed",
            usage: {
              input_tokens: 10,
              output_tokens: 12,
              cache_creation_input_tokens: 0,
              cache_read_input_tokens: 0,
              input_includes_cache_read: false,
            },
            conversation_id: "conv-activity",
            message_id: parsed.assistant_message_id,
          });
        }, 90);
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

test.describe("MiniCode activity flow", () => {
  test.beforeEach(async ({ page }) => {
    await mockWebSocket(page);
    await openApp(page);
  });

  test("collapses completed activity and keeps final answer visible", async ({ page }) => {
    const composer = page.locator('textarea, input[placeholder*="message" i], [contenteditable="true"]').first();
    await composer.fill("请读取 README.md，然后一句话说明 MiniCode 是什么。");
    await composer.press("Enter");

    const finalAnswer = page.getByText("MiniCode 是一个本地 AI 编程工作台。");
    await expect(finalAnswer).toBeVisible({ timeout: 5000 });

    const activityHeader = page.getByRole("button", { name: "展开处理步骤" });
    await expect(activityHeader).toBeVisible();
    await expect(activityHeader).toHaveAttribute("aria-expanded", "false");
    const readRow = page.getByText("README.md", { exact: true }).first();
    await expect(readRow).toHaveCount(0);

    await activityHeader.click();
    await expect(page.getByRole("button", { name: "收起处理步骤" })).toHaveAttribute("aria-expanded", "true");
    await expect(readRow).toBeVisible();
    await expect(finalAnswer).toBeVisible();
  });
});
