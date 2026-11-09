import { mockWorkspaceEnvironment } from "./http-fixtures";
import { expect, Page, test } from "@playwright/test";

// The composer draft on Home owns its model selection and reads it over HTTP;
// the mock WebSocket alone leaves currentModel empty, so answer those requests here.
async function mockDraftHttp(page: Page) {
  await mockWorkspaceEnvironment(page);
  const model = "gpt-4.1";
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

async function mockWebSocket(page: Page) {
  await page.addInitScript(() => {
    const messages: string[] = [];
    localStorage.setItem("minicode.conversation.active-id", "conv-activity");

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
        if (parsed.type === "user_message") {
          setTimeout(() => {
            const conversation_id = parsed.conversation_id || "conv-activity";
            const run_id = `turn-${parsed.assistant_message_id}`;
            this._receive({ type: "agent.run.started", role: "main", status: "running", run_id, turn_id: run_id,
              conversation_id, message_id: parsed.assistant_message_id, started_at: Date.now() });
            this._receive({ type: "command.result", command: "user_message", level: "success", message: "",
              client_command_id: parsed.client_command_id, conversation_id,
              data: { admitted: true, conversation_id, run_id, user_message_id: parsed.user_message_id,
                assistant_message_id: parsed.assistant_message_id } });
          }, 0);
        }
        if (parsed.type === "session.sync" || parsed.type === "session.restore") {
          // The renderer only reports itself connected once the session
          // handshake is answered; this mirrors the canonical empty-session
          // snapshot (test/initial-session-sync.ts).
          const restore = parsed.type === "session.restore";
          const session = { active_conversation_id: "conv-activity", active_conversation: { id: "conv-activity", title: "Fixture chat", workspace_root: "C:/Desktop/MiniCode", transcript: [] }, active_stream_conversation_ids: [], selected_model: "gpt-4.1", workspace_root: "C:/Desktop/MiniCode" };
          setTimeout(() => {
            this._receive({
              type: restore ? "session.restored" : "session.synced",
              session_id: "e2e-session",
              synced: true,
              client_command_id: parsed.client_command_id,
              active_conversation_id: session.active_conversation_id,
              ...(restore ? { conversation: session.active_conversation, messages: [] } : {}),
              current_model: "gpt-4.1",
              model: "gpt-4.1",
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
            item: { id: "activity-answer", type: "agent_message", source: "model_final" },
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
            item: { id: "activity-answer", type: "agent_message", text: "MiniCode 是一个本地 AI 编程工作台。", status: "completed", source: "model_final" },
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
  await mockDraftHttp(page);
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
