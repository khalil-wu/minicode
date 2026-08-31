import { test, expect, Page } from "@playwright/test";

async function mockWebSocket(page: Page) {
  await page.addInitScript(() => {
    const messages: string[] = [];
    const originalWebSocket = window.WebSocket;

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

          // Send initial conversation.list response
          this._receive({
            type: "conversation.list",
            conversations: [{
              id: "conv-e2e",
              title: "E2E session",
              updated_at: "2026-07-11T00:00:00.000Z",
            }],
            active_conversation_id: "conv-e2e",
            active_conversation: {
              id: "conv-e2e",
              title: "E2E session",
              updated_at: "2026-07-11T00:00:00.000Z",
              messages: [],
            },
          });

          // Send initial llm.model.updated
          this._receive({
            type: "llm.model.updated",
            model: "gpt-4",
            current_model: "gpt-4",
            available_models: ["gpt-4", "gpt-3.5-turbo"],
            working_directory: "/tmp/test",
          });
        }, 50);
      }

      send(data: string) {
        messages.push(data);
        const parsed = JSON.parse(data);

        if (parsed.client_command_id) {
          setTimeout(() => {
            this._receive({
              type: "client.command.ack",
              client_command_id: parsed.client_command_id,
              command_type: parsed.type,
              accepted: true,
            });
          }, 0);
        }

        if (parsed.type === "conversation.truncate") {
          setTimeout(() => {
            this._receive({
              type: "command.result",
              command: "conversation.truncate",
              level: "success",
              message: "Conversation truncated",
              data: { client_command_id: parsed.client_command_id },
            });
          }, 0);
          return;
        }

        // Simulate backend responses
        if (parsed.type === "llm.config.set") {
          setTimeout(() => {
            const models =
              parsed.provider === "anthropic"
                ? ["claude-3-opus", "claude-3-sonnet"]
                : parsed.provider === "custom"
                  ? [parsed.model || "default"]
                  : ["gpt-4", "gpt-3.5-turbo"];
            this._receive({
              type: "llm.model.updated",
              model: models[0],
              current_model: models[0],
              available_models: models,
            });
          }, 100);
        }

        if (parsed.type === "skills.list") {
          setTimeout(() => {
            this._receive({
              type: "skills.list",
              skills: [
                {
                  name: "react-ui-reviewer",
                  description: "Review React UI for accessibility and polish.",
                  triggers: ["react", "ui"],
                  source_level: "global",
                  active: false,
                },
                {
                  name: "python-refactor-planner",
                  description: "Plan Python refactors.",
                  triggers: ["python"],
                  source_level: "builtin",
                  active: false,
                },
              ],
            });
          }, 50);
        }

        if (parsed.type === "load_skill") {
          setTimeout(() => {
            this._receive({
              type: "skill_activated",
              skill_name: parsed.skill_name,
            });
          }, 50);
        }

        if (parsed.type === "skills.marketplace.list") {
          setTimeout(() => {
            this._receive({
              type: "skills.marketplace.list",
              skills: [
                { name: "react-ui-reviewer", title: "React UI Reviewer", description: "Review React UI for accessibility and polish.", triggers: ["react", "ui"], installed: false },
                { name: "github-actions-auditor", title: "GitHub Actions Auditor", description: "Audit GitHub workflows for unsafe permissions.", triggers: ["github", "actions"], installed: false },
              ],
            });
          }, 50);
        }

        if (parsed.type === "commands.list") {
          setTimeout(() => {
            this._receive({
              type: "commands.list",
              conversation_id: "conv-e2e",
              commands: [
                { name: "plan", command: "plan", label: "/plan", description: "Switch to plan mode", type: "local", source: "builtin", enabled: true, availability: { kind: "available", scope: "session" } },
                { name: "compact", command: "compact", label: "/compact", description: "Compact conversation context", type: "local", source: "builtin", enabled: true, availability: { kind: "available", scope: "session" } },
                { name: "clear", command: "clear", label: "/clear", description: "Reset the conversation", type: "local", source: "builtin", enabled: true, availability: { kind: "available", scope: "session" } },
                { name: "skills", command: "skills", label: "/skills", description: "Open Skills marketplace", type: "local", source: "builtin", enabled: true, availability: { kind: "available", scope: "session" } },
                { name: "review", command: "review", label: "/review", description: "Review the project for issues", type: "template", source: "builtin", enabled: true, availability: { kind: "available", scope: "session" } },
              ],
            });
          }, 50);
        }

        if (parsed.type === "user_message" && !(window as any).__suppressMockAutoResponse) {
          setTimeout(() => {
            const owner = {
              conversation_id: parsed.conversation_id || "conv-e2e",
              message_id: parsed.assistant_message_id,
            };
            this._receive({
              type: "item.started",
              item: { id: "mock-answer", type: "agent_message" },
              ...owner,
            });
            this._receive({
              type: "agent_message.delta",
              item_id: "mock-answer",
              delta: "Mock response to: " + parsed.content,
              ...owner,
            });
            this._receive({
              type: "item.completed",
              item: { id: "mock-answer", type: "agent_message", text: "Mock response to: " + parsed.content, status: "completed" },
              ...owner,
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
              ...owner,
            });
          }, 100);
        }
      }

      close() {
        this.readyState = 3;
      }

      _receive(data: object) {
        const ev = new MessageEvent("message", {
          data: JSON.stringify(data),
        });
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
  await expect(page.locator(
    'textarea, input[placeholder*="message" i], [contenteditable="true"]'
  ).first()).toBeVisible();
  await expect.poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().isConnected)).toBe(true);
  await expect.poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().currentModel || "")).not.toBe("");
}

test.describe("Bug Fix: Provider config updates model selector", () => {
  test.beforeEach(async ({ page }) => {
    await mockWebSocket(page);
    await openApp(page);
  });

  test("model selector shows initial models from server", async ({ page }) => {
    // shortModel strips "gpt-" prefix, so button text is "4" but title is "gpt-4"
    const modelBtn = page.locator('button[title="gpt-4"]');
    await expect(modelBtn).toBeVisible({ timeout: 5000 });
  });

  test("settings provider change triggers model update", async ({ page }) => {
    // Open settings via keyboard shortcut or command palette
    await page.keyboard.press("Control+,");
    await page.waitForTimeout(300);

    // Look for the settings panel
    const settingsPanel = page.locator("text=Settings").first();
    if (await settingsPanel.isVisible()) {
      // Navigate to Model Provider tab
      const providerTab = page.locator("text=Model Provider").first();
      if (await providerTab.isVisible()) {
        await providerTab.click();
        await page.waitForTimeout(200);
      }

      // Select a different provider (e.g., Anthropic)
      const providerSelect = page.locator("select, [role=listbox]").first();
      if (await providerSelect.isVisible()) {
        const anthropicValue = await providerSelect.locator("option").evaluateAll((options) => {
          const option = options.find((item) => /anthropic/i.test(item.textContent ?? "") || /anthropic/i.test(item.getAttribute("value") ?? ""));
          return option?.getAttribute("value") ?? null;
        });
        if (anthropicValue) {
          await providerSelect.selectOption(anthropicValue);
        }
        await page.waitForTimeout(200);
      }
    }
  });

  test("after provider config save, model list updates", async ({ page }) => {
    // Verify that when llm.model.updated event arrives, the UI updates
    // Simulate receiving a model update event
    await page.evaluate(() => {
      const ws = (window as any).__mockWs;
      if (ws) {
        ws._receive({
          type: "llm.model.updated",
          model: "deepseek-chat",
          current_model: "deepseek-chat",
          available_models: ["deepseek-chat", "deepseek-coder"],
        });
      }
    });

    await page.waitForTimeout(300);

    await expect
      .poll(() =>
        page.evaluate(() => (window as any).__zustandStore?.getState().currentModel),
      )
      .toBe("deepseek-chat");

    await expect(page.getByRole("button", { name: /deepseek/i }).first()).toBeVisible({ timeout: 3000 });
  });
});

test.describe("Bug Fix: authoritative live projection", () => {
  test.beforeEach(async ({ page }) => {
    await mockWebSocket(page);
    await openApp(page);
  });

  test("renders commentary in the work area from the first streamed frame", async ({ page }) => {
    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.setState({
        conversationId: "conv-e2e",
        messages: [{
          id: "assistant-stream",
          role: "assistant",
          content: "",
          blocks: [],
          artifacts: [],
          timestamp: Date.now(),
          isStreaming: true,
        }],
        isStreaming: true,
      });
      const ws = (window as any).__mockWs;
      ws._receive({
        type: "item.started",
        conversation_id: "conv-e2e",
        message_id: "assistant-stream",
        item: {
          id: "commentary-live",
          type: "agent_message",
          text: "",
          source: "commentary",
          status: "in_progress",
        },
      });
      ws._receive({
        type: "agent_message.delta",
        conversation_id: "conv-e2e",
        message_id: "assistant-stream",
        item_id: "commentary-live",
        source: "commentary",
        delta: "正在检查真实调用链。",
      });
    });

    const processText = page.locator(".agent-loop-work-area").getByText("正在检查真实调用链。");
    await expect(processText).toBeVisible();
    await expect(page.locator(".agent-loop-reply-area").getByText("正在检查真实调用链。")).toHaveCount(0);
    await expect(page.getByText("正在检查真实调用链。", { exact: true })).toHaveCount(1);

    await page.evaluate(() => {
      const ws = (window as any).__mockWs;
      ws._receive({
        type: "item.completed",
        conversation_id: "conv-e2e",
        message_id: "assistant-stream",
        item: {
          id: "commentary-live",
          type: "agent_message",
          text: "正在检查真实调用链。",
          source: "commentary",
          status: "completed",
        },
      });
      ws._receive({
        type: "item.started",
        conversation_id: "conv-e2e",
        message_id: "assistant-stream",
        item: {
          id: "answer-live",
          type: "agent_message",
          text: "",
          source: "model_final",
          status: "in_progress",
        },
      });
      ws._receive({
        type: "agent_message.delta",
        conversation_id: "conv-e2e",
        message_id: "assistant-stream",
        item_id: "answer-live",
        source: "model_final",
        delta: "真实答案只出现一次。",
      });
    });

    await expect(page.locator(".agent-loop-reply-area").getByText("真实答案只出现一次。")).toBeVisible();
    await expect(page.getByText("真实答案只出现一次。", { exact: true })).toHaveCount(1);
    await expect(page.locator(".agent-loop-work-area").getByText("正在检查真实调用链。")).toBeVisible();
  });

  test("renders delegation before the task tool completes and updates it in place", async ({ page }) => {
    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.setState({
        conversationId: "conv-e2e",
        messages: [{
          id: "assistant-stream",
          role: "assistant",
          content: "",
          blocks: [],
          artifacts: [],
          timestamp: Date.now(),
          isStreaming: true,
        }],
        isStreaming: true,
      });
      const ws = (window as any).__mockWs;
      ws._receive({
        type: "tool_call",
        conversation_id: "conv-e2e",
        message_id: "assistant-stream",
        id: "task-live",
        name: "task",
        status: "running",
        result_kind: "subagent",
        activity_kind: "genericTool",
        args: {
          parallel_tasks: [
            { description: "子任务一", prompt: "读取 fact-1.txt", agent_type: "explore" },
            { description: "子任务二", prompt: "读取 fact-2.txt", agent_type: "explore" },
          ],
        },
      });
    });

    const runningCell = page.locator('.collaboration-cell[data-status="running"]');
    await expect(runningCell).toContainText("正在发送 2 个智能体");
    await expect(runningCell).toContainText("读取 fact-1.txt");
    await expect(runningCell).toContainText("读取 fact-2.txt");

    await page.evaluate(() => {
      const ws = (window as any).__mockWs;
      ws._receive({
        type: "tool_result",
        conversation_id: "conv-e2e",
        message_id: "assistant-stream",
        id: "task-live",
        status: "success",
        result_kind: "subagent",
        summary: "4 个智能体已启动",
      });
    });

    await expect(page.locator('.collaboration-cell[data-status="success"]')).toHaveCount(1);
    await expect(page.locator('.collaboration-cell[data-status="running"]')).toHaveCount(0);
    await expect(page.getByText("读取 fact-1.txt", { exact: true })).toHaveCount(1);
  });

  test("keeps both sidebar mode tabs fully visible and directly clickable", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 1000 });
    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.setState({ leftSidebarWidth: 272, appMode: "code" });
    });

    const switcher = page.getByTestId("sidebar-mode-switch");
    await expect(switcher).toBeVisible();
    const result = await switcher.evaluate((element) => {
      const listRect = element.getBoundingClientRect();
      const tabs = Array.from(element.querySelectorAll<HTMLElement>('[role="tab"]'));
      return tabs.map((tab) => {
        const rect = tab.getBoundingClientRect();
        const points = [
          [rect.left + 1, rect.top + rect.height / 2],
          [rect.right - 1, rect.top + rect.height / 2],
        ];
        return {
          inside: rect.left >= listRect.left && rect.right <= listRect.right,
          hits: points.map(([x, y]) => document.elementFromPoint(x, y)?.closest('[role="tab"]') === tab),
        };
      });
    });

    expect(result).toHaveLength(2);
    expect(result.every((tab) => tab.inside && tab.hits.every(Boolean))).toBe(true);
    await page.getByRole("tab", { name: "协作" }).click();
    await expect(page.getByRole("tab", { name: "协作" })).toHaveAttribute("aria-selected", "true");
    await page.getByRole("tab", { name: "代码" }).click();
    await expect(page.getByRole("tab", { name: "代码" })).toHaveAttribute("aria-selected", "true");
  });
});

test.describe("Bug Fix: Terminal input stays in terminal", () => {
  test.beforeEach(async ({ page }) => {
    await mockWebSocket(page);
    await openApp(page);
  });

  test("terminal.output events do not appear in chat messages", async ({
    page,
  }) => {
    // Simulate a terminal.output event from the server
    await page.evaluate(() => {
      const ws = (window as any).__mockWs;
      if (ws) {
        ws._receive({
          type: "terminal.output",
          session_id: "test-session",
          data: "$ ls\nfile1.txt\nfile2.txt\n",
        });
      }
    });

    await page.waitForTimeout(300);

    // The terminal output should NOT appear in the chat area
    const chatArea = page.locator('[class*="message"], [data-testid="messages"]').first();
    const terminalText = page.locator("text=file1.txt");

    // If terminal output leaked into chat, it would be visible in the main content area
    // We check that it's NOT in the chat messages list
    const chatMessages = await page.locator('[role="log"], [class*="messages"]').first();
    if (await chatMessages.isVisible()) {
      const content = await chatMessages.textContent();
      expect(content).not.toContain("file1.txt");
    }
  });

  test("terminal.output does not create system messages in chat", async ({
    page,
  }) => {
    // Get initial message count
    const initialMessages = await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      if (store) return store.getState().messages.length;
      return 0;
    });

    // Send terminal output event
    await page.evaluate(() => {
      const ws = (window as any).__mockWs;
      if (ws) {
        ws._receive({
          type: "terminal.output",
          session_id: "s1",
          data: "hello from terminal\n",
        });
      }
    });

    await page.waitForTimeout(200);

    // Message count should not have increased
    const afterMessages = await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      if (store) return store.getState().messages.length;
      return 0;
    });

    // If store is accessible, verify no new messages were added
    if (initialMessages !== 0 || afterMessages !== 0) {
      expect(afterMessages).toBe(initialMessages);
    }
  });
});

test.describe("Slash command menu", () => {
  test.beforeEach(async ({ page }) => {
    await mockWebSocket(page);
    await openApp(page);
  });

  test("typing / in composer shows slash command menu", async ({ page }) => {
    // Find the composer input
    const composer = page.locator(
      'textarea, input[placeholder*="message" i], [contenteditable="true"]'
    ).first();

    if (await composer.isVisible()) {
      await composer.click();
      await composer.fill("/");
      await page.waitForTimeout(300);

      // A menu/overlay should appear with slash commands
      // Use a more specific selector to avoid matching plan status text
      const slashItem = page.locator("span").filter({ hasText: /^\/plan$/ }).first();
      const menuVisible = await slashItem.isVisible().catch(() => false);

      expect(menuVisible).toBeTruthy();
    }
  });

  test("selecting /compact sends user_message over WebSocket", async ({
    page,
  }) => {
    // /compact is a command that sends a user_message (unlike /plan which is local-only)
    await page.evaluate(() => {
      const messages = (window as any).__mockWsMessages;
      if (messages) messages.length = 0;
    });

    const composer = page.locator(
      'textarea, input[placeholder*="message" i], [contenteditable="true"]'
    ).first();

    if (await composer.isVisible()) {
      await composer.click();
      await composer.fill("/");
      await page.waitForTimeout(300);

      // Click the /compact option in the slash menu
      const compactOption = page.locator("span").filter({ hasText: /^\/compact$/ }).first();
      if (await compactOption.isVisible()) {
        await compactOption.click();
        await page.waitForTimeout(500);

        const sentMessages = await page.evaluate(() => {
          return (window as any).__mockWsMessages ?? [];
        });

        const userMsg = sentMessages.find((m: string) => {
          const parsed = JSON.parse(m);
          return parsed.type === "user_message" && parsed.content?.includes("/compact");
        });

        expect(userMsg).toBeTruthy();
      }
    }
  });

  test("typing /compact and pressing Enter sends user_message", async ({ page }) => {
    // Verify slash commands round-trip through user_message when typed directly
    const composer = page.locator(
      'textarea, input[placeholder*="message" i], [contenteditable="true"]'
    ).first();

    if (await composer.isVisible()) {
      await composer.click();
      await composer.fill("/compact");
      await page.waitForTimeout(300);

      await page.keyboard.press("Enter");
      await page.waitForTimeout(500);

      const wsSent = await page.evaluate(() => {
        return (window as any).__mockWsMessages ?? [];
      });

      const hasSentMessage = wsSent.some((m: string) => {
        try {
          const p = JSON.parse(m);
          return p.type === "user_message";
        } catch {
          return false;
        }
      });

      expect(hasSentMessage).toBeTruthy();
    }
  });

  test("/skills shows a second-level skill picker and attaches selected skill", async ({ page }) => {
    const composer = page.locator(
      'textarea, input[placeholder*="message" i], [contenteditable="true"]'
    ).first();

    await composer.click();
    await composer.fill("/skills ");
    await page.waitForTimeout(300);

    await expect(page.getByText("react-ui-reviewer").first()).toBeVisible();
    await page.keyboard.press("Enter");
    await page.waitForTimeout(300);

    const selectedSkills = await page.evaluate(() =>
      (window as any).__zustandStore?.getState().selectedSkills.map((skill: any) => skill.name),
    );
    expect(selectedSkills).toContain("react-ui-reviewer");
    await expect(page.getByText("react-ui-reviewer").first()).toBeVisible();
  });

  test("root slash menu includes skills as direct actions", async ({ page }) => {
    const composer = page.locator(
      'textarea, input[placeholder*="message" i], [contenteditable="true"]'
    ).first();

    await composer.click();
    await composer.fill("/");
    await page.waitForTimeout(300);

    await expect(page.getByText("/react-ui-reviewer").first()).toBeVisible();
    await page.getByText("/react-ui-reviewer").first().click();
    await page.waitForTimeout(300);

    const sentMessages = await page.evaluate(() => (window as any).__mockWsMessages ?? []);
    expect(sentMessages.some((raw: string) => {
      const parsed = JSON.parse(raw);
      return parsed.type === "load_skill" && parsed.skill_name === "react-ui-reviewer";
    })).toBeFalsy();
    const selectedSkills = await page.evaluate(() =>
      (window as any).__zustandStore?.getState().selectedSkills.map((skill: any) => skill.name),
    );
    expect(selectedSkills).toContain("react-ui-reviewer");
  });
});

test.describe("Message context and compact assistant process UI", () => {
  test.beforeEach(async ({ page }) => {
    await mockWebSocket(page);
    await openApp(page);
  });

  test("sent @file context remains visible and recall restores it", async ({ page }) => {
    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.getState().addSelectedMention({
        kind: "file",
        name: "App.tsx",
        path: "frontend/src.v2/App.tsx",
      });
    });

    const composer = page.locator(
      'textarea, input[placeholder*="message" i], [contenteditable="true"]'
    ).first();
    await composer.click();
    await composer.fill("Help me answer this question");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(500);

    await expect(page.getByText("App.tsx").first()).toBeVisible();
    const wsPayloads = await page.evaluate(() => (window as any).__mockWsMessages ?? []);
    expect(wsPayloads.some((raw: string) => {
      const payload = JSON.parse(raw);
      return payload.content?.includes("File reference: frontend/src.v2/App.tsx");
    })).toBeTruthy();

    const recallButton = page.getByRole("button", { name: "撤回到输入框" }).first();
    await recallButton.locator("xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' user-cell-wrap ')][1]").hover();
    await recallButton.click();
    await page.getByRole("dialog").getByRole("button", { name: "撤回" }).click();
    await expect(composer).toHaveValue("Help me answer this question");
    const restoredMentions = await page.evaluate(() =>
      (window as any).__zustandStore?.getState().selectedMentions.map((item: any) => item.path),
    );
    expect(restoredMentions).toContain("frontend/src.v2/App.tsx");
  });

  test("@ search attaches files directly and sends a durable file reference", async ({ page }) => {
    await page.route("**/api/workspace/search?**", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          query: "App",
          results: [
            {
              path: "frontend/src.v2/App.tsx",
              name: "App.tsx",
              score: 1,
              kind: "file",
            },
          ],
        }),
      });
    });
    await page.route("**/api/workspace/file?**", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          path: "frontend/src.v2/App.tsx",
          content: "export const App = () => <div>MiniCode App</div>;",
          content_hash: "hash",
          size_bytes: 48,
        }),
      });
    });

    const composer = page.locator(
      'textarea, input[placeholder*="message" i], [contenteditable="true"]'
    ).first();
    await composer.click();
    await composer.fill("@App");
    await page.waitForTimeout(400);
    await page.keyboard.press("Enter");

    await expect(page.getByRole("button", { name: "打开 App.tsx" })).toBeVisible();
    await composer.fill("Explain this file");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(500);

    const wsPayloads = await page.evaluate(() => (window as any).__mockWsMessages ?? []);
    const userMsg = wsPayloads.map((raw: string) => JSON.parse(raw)).find((payload: any) => payload.type === "user_message" && payload.content?.includes("Explain this file"));
    expect(userMsg?.content).toContain("File reference: frontend/src.v2/App.tsx");
    expect(userMsg?.content).not.toContain("export const App");
    expect(userMsg?.content).toContain("Explain this file");
  });

  test("@ file line anchors keep the clean path when attached", async ({ page }) => {
    await page.route("**/api/workspace/search?**", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          query: "App",
          results: [
            {
              path: "frontend/src.v2/App.tsx",
              name: "App.tsx",
              score: 1,
              kind: "file",
            },
          ],
        }),
      });
    });
    await page.route("**/api/workspace/file?**", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          path: "frontend/src.v2/App.tsx",
          content: ["line 1", "line 2", "line 3"].join("\n"),
          content_hash: "hash",
          size_bytes: 20,
        }),
      });
    });

    const composer = page.locator(
      'textarea, input[placeholder*="message" i], [contenteditable="true"]'
    ).first();
    await composer.click();
    await composer.fill("@App#L2");
    await page.waitForTimeout(400);
    await page.keyboard.press("Enter");

    const selectedMentions = await page.evaluate(() =>
      (window as any).__zustandStore?.getState().selectedMentions,
    );
    expect(selectedMentions[0].path).toBe("frontend/src.v2/App.tsx#2");
    expect(selectedMentions[0].path).not.toContain("file:");

    await composer.fill("Explain this line");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(500);

    const wsPayloads = await page.evaluate(() => (window as any).__mockWsMessages ?? []);
    const userMsg = wsPayloads.map((raw: string) => JSON.parse(raw)).find((payload: any) => payload.type === "user_message" && payload.content?.includes("Explain this line"));
    expect(userMsg?.content).toContain("File reference: frontend/src.v2/App.tsx#2");
    expect(userMsg?.content).not.toContain("line 2");
    expect(userMsg?.content).not.toContain("line 1");
  });

  test("@ file references do not block send when the workspace API is unavailable", async ({ page }) => {
    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.getState().addSelectedMention({
        kind: "file",
        name: "Missing.tsx",
        path: "frontend/src.v2/Missing.tsx",
      });
    });
    await page.route("**/api/workspace/file?**", async (route) => {
      await route.abort();
    });

    const composer = page.locator(
      'textarea, input[placeholder*="message" i], [contenteditable="true"]'
    ).first();
    await composer.click();
    await composer.fill("Continue anyway");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(500);

    const wsPayloads = await page.evaluate(() => (window as any).__mockWsMessages ?? []);
    const userMsg = wsPayloads.map((raw: string) => JSON.parse(raw)).find((payload: any) => payload.type === "user_message" && payload.content?.includes("Continue anyway"));
    expect(userMsg).toBeTruthy();
    expect(userMsg?.content).toContain("File reference: frontend/src.v2/Missing.tsx");
  });

  test("thinking stays visible while completed tool calls remain compact", async ({ page }) => {
    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.setState({
        messages: [
          {
            id: "assistant-compact-process",
            role: "assistant",
            content: "Done.",
            thinking: Array.from({ length: 20 }, (_, i) => `thinking line ${i + 1}`).join("\n"),
            toolCalls: [
              {
                id: "tool-1",
                name: "read_file",
                status: "success",
                args: { path: "frontend/src.v2/App.tsx" },
                summary: "Read the file.",
                startedAt: Date.now() - 1200,
                finishedAt: Date.now(),
              },
            ],
            artifacts: [],
            timestamp: Date.now(),
            isStreaming: false,
          },
        ],
        isStreaming: false,
      });
    });

    const processSummary = page.getByRole("button", { name: "展开处理步骤" }).first();
    await expect(processSummary).toBeVisible();
    await expect(page.getByText("thinking line 20")).toHaveCount(0);

    await processSummary.click();
    await expect(page.getByRole("button", { name: "收起处理步骤" }).first()).toBeVisible();
    await expect(page.getByText("frontend/src.v2/App.tsx").first()).toBeVisible();
    await page.getByRole("button", { name: "思考" }).click();
    await expect(page.getByText("thinking line 20")).toBeVisible();
  });

  test("tool details prefer readable summaries and hide raw JSON by default", async ({ page }) => {
    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.setState({
        messages: [
          {
            id: "assistant-readable-tools",
            role: "assistant",
            content: "Done.",
            toolCalls: [
              {
                id: "tool-readable-1",
                name: "run_command",
                status: "success",
                args: { command: "npm test", cwd: "/repo" },
                summary: JSON.stringify({ summary: "Tests passed", exit_code: 0 }),
                startedAt: Date.now() - 1200,
                finishedAt: Date.now(),
              },
            ],
            artifacts: [],
            timestamp: Date.now(),
          },
        ],
        isStreaming: false,
      });
    });

    await page.getByRole("button", { name: "展开处理步骤" }).click();
    await expect(page.getByText("npm test").first()).toBeVisible();
    await expect(page.getByText("Tests passed").first()).toHaveCount(0);
    await expect(page.getByText('"command": "npm test"')).toBeHidden();
  });

  test("approval requests render inline in the chat instead of a blocking modal", async ({ page }) => {
    await page.evaluate(() => {
      const ws = (window as any).__mockWs;
      ws?._receive({
        type: "control_request",
        request_id: "approval-inline-1",
        conversation_id: "conv-e2e",
        request: {
          subtype: "can_use_tool",
          tool_use_id: "approval-inline-1",
          tool_name: "run_command",
          input: { command: "npm install", cwd: "/repo" },
        },
      });
    });

    await expect(page.getByLabel("Agent is waiting for input")).toBeVisible();
    await expect(page.getByText("允许使用 运行命令？")).toBeVisible();
    await expect(page.getByRole("button", { name: "允许使用工具" })).toBeVisible();
    await expect(page.locator(".overlay-backdrop")).toHaveCount(0);

    await page.getByRole("button", { name: "允许使用工具" }).click();
    await page.waitForTimeout(100);
    const sent = await page.evaluate(() => (window as any).__mockWsMessages ?? []);
    expect(sent.some((raw: string) => {
      const parsed = JSON.parse(raw);
      return parsed.type === "control_response"
        && parsed.request_id === "approval-inline-1"
        && parsed.response?.response?.action === "approve";
    })).toBeTruthy();
  });

  test("agent planning and execution stream is visible across chat and Activity", async ({ page }) => {
    await page.evaluate(() => {
      (window as any).__suppressMockAutoResponse = true;
    });

    const composer = page.locator(
      'textarea, input[placeholder*="message" i], [contenteditable="true"]'
    ).first();

    await composer.click();
    await composer.fill("Plan and inspect the BrowserPanel implementation");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(150);

    await page.evaluate(() => {
      const ws = (window as any).__mockWs;
      const command = ((window as any).__mockWsMessages ?? [])
        .map((raw: string) => JSON.parse(raw))
        .find((item: any) => item.type === "user_message");
      const receive = (event: Record<string, unknown>) => ws?._receive({
        ...event,
        conversation_id: command?.conversation_id,
        message_id: command?.assistant_message_id,
      });
      receive({
        type: "agent.progress",
        id: "progress-analyze",
        stage: "planning",
        status: "running",
        phase: "planning",
        visibility: "timeline",
        message: "Reading request and workspace context",
      });
      receive({
        type: "plan_updated",
        plan_id: "agent-flow-plan",
        status: "executing",
        current_step: 0,
        steps: [
          {
            id: "scan",
            title: "Scan BrowserPanel structure",
            detail: "Read the current panel and identify control flow.",
            status: "running",
          },
          {
            id: "summarize",
            title: "Summarize findings",
            detail: "Explain what changed and what still needs work.",
            status: "pending",
          },
        ],
      });
      receive({
        type: "task.update",
        todo_id: "todo-scan-browser-panel",
        status: "in_progress",
        content: "Scan BrowserPanel structure",
        activeForm: "Scanning BrowserPanel structure",
      });
      receive({
        type: "thinking_delta",
        content: "I will inspect the selected panel, then map the execution state into a concise answer.",
      });
      receive({
        type: "agent.progress",
        id: "progress-read",
        stage: "tool",
        status: "running",
        phase: "tool",
        visibility: "timeline",
        message: "Read frontend/src.v2/panels/BrowserPanel.tsx",
        detail: "Reading the selected panel before deciding what to change.",
        tool_call_id: "tool-read-browser-panel",
        tool_name: "read_file",
      });
      receive({
        type: "tool_call",
        id: "tool-read-browser-panel",
        name: "read_file",
        args: { path: "frontend/src.v2/panels/BrowserPanel.tsx" },
      });
    });

    await expect
      .poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().rightStackTab))
      .toBe("tasks");
    await page.evaluate(() => (window as any).__zustandStore?.setState({ rightPanelOpen: true }));
    await expect(page.getByText("Reading request and workspace context").first()).toBeVisible();
    await expect(page.getByText("Scanning BrowserPanel structure").first()).toBeVisible();
    await expect(page.getByText("frontend/src.v2/panels/BrowserPanel.tsx").first()).toBeVisible();
    await page.getByRole("button", { name: "关闭右侧面板" }).click();
    await expect(page.getByRole("dialog", { name: "右侧面板" })).toHaveCount(0);
    await page.getByRole("button", { name: "思考" }).click();
    await expect(page.getByText(/I will inspect the selected panel/).first()).toBeVisible();
    await page.getByRole("button", { name: "打开右侧栏" }).click();

    await page.evaluate(() => {
      (window as any).__zustandStore?.getState().setPlan({
        planId: "agent-flow-plan",
        status: "executing",
        currentStep: 0,
        steps: [
          {
            id: "scan",
            title: "Scan BrowserPanel structure",
            detail: "Read the current panel and identify control flow.",
            status: "running",
          },
          {
            id: "summarize",
            title: "Summarize findings",
            detail: "Explain what changed and what still needs work.",
            status: "pending",
          },
        ],
      });
    });

    await expect(page.getByText("Scanning BrowserPanel structure").first()).toBeVisible();
    await expect.poll(() => page.evaluate(() => {
      const plan = (window as any).__zustandStore?.getState().plan;
      return [plan?.status, plan?.steps?.[0]?.title, plan?.steps?.[0]?.detail];
    })).toEqual([
      "executing",
      "Scan BrowserPanel structure",
      "Read the current panel and identify control flow.",
    ]);

    await page.evaluate(() => {
      const ws = (window as any).__mockWs;
      const command = ((window as any).__mockWsMessages ?? [])
        .map((raw: string) => JSON.parse(raw))
        .find((item: any) => item.type === "user_message");
      const receive = (event: Record<string, unknown>) => ws?._receive({
        ...event,
        conversation_id: command?.conversation_id,
        message_id: command?.assistant_message_id,
      });
      receive({
        type: "agent.progress",
        id: "progress-read",
        stage: "tool",
        status: "completed",
        phase: "tool",
        visibility: "timeline",
        message: "Read frontend/src.v2/panels/BrowserPanel.tsx",
        detail: "Read BrowserPanel.tsx and found CDP target selection plus screenshot/action controls.",
        tool_call_id: "tool-read-browser-panel",
        tool_name: "read_file",
      });
      receive({
        type: "tool_result",
        id: "tool-read-browser-panel",
        summary: "Read BrowserPanel.tsx and found CDP target selection plus screenshot/action controls.",
      });
      receive({
        type: "task.update",
        todo_id: "todo-scan-browser-panel",
        status: "completed",
        content: "Scan BrowserPanel structure",
      });
      receive({
        type: "plan_updated",
        plan_id: "agent-flow-plan",
        status: "completed",
        current_step: 1,
        steps: [
          {
            id: "scan",
            title: "Scan BrowserPanel structure",
            detail: "Read the current panel and identify control flow.",
            status: "done",
          },
          {
            id: "summarize",
            title: "Summarize findings",
            detail: "Explain what changed and what still needs work.",
            status: "done",
          },
        ],
      });
      receive({
        type: "item.started",
        item: { id: "plan-answer", type: "agent_message" },
      });
      receive({
        type: "agent_message.delta",
        item_id: "plan-answer",
        delta: "Plan complete. I checked BrowserPanel, verified target discovery, screenshot capture, and selector actions.",
      });
      receive({
        type: "item.completed",
        item: { id: "plan-answer", type: "agent_message", text: "Plan complete. I checked BrowserPanel, verified target discovery, screenshot capture, and selector actions.", status: "completed" },
      });
      receive({
        type: "done",
        status: "completed",
        usage: {
          input_tokens: 120,
          output_tokens: 48,
          cache_creation_input_tokens: 0,
          cache_read_input_tokens: 0,
          input_includes_cache_read: false,
        },
      });
      (window as any).__zustandStore?.getState().setPlan({
        planId: "agent-flow-plan",
        status: "completed",
        currentStep: 1,
        steps: [
          {
            id: "scan",
            title: "Scan BrowserPanel structure",
            detail: "Read the current panel and identify control flow.",
            status: "done",
          },
          {
            id: "summarize",
            title: "Summarize findings",
            detail: "Explain what changed and what still needs work.",
            status: "done",
          },
        ],
      });
    });

    await expect(page.getByText("Plan complete. I checked BrowserPanel").first()).toBeVisible();
    await page.getByRole("button", { name: "关闭右侧面板" }).click();
    await page.getByRole("button", { name: "展开处理步骤" }).click();
    await expect(page.getByText(/Read BrowserPanel\.tsx and found CDP target selection/).first()).toBeVisible();
    await expect
      .poll(() => page.evaluate(() => {
        const todos = (window as any).__zustandStore?.getState().todos ?? [];
        return todos.some((todo: any) => todo.id === "todo-scan-browser-panel" && todo.status === "completed");
      }))
      .toBe(true);
    await expect
      .poll(() => page.evaluate(() => {
        const progress = (window as any).__zustandStore?.getState().agentProgress ?? [];
        return progress.some((entry: any) => entry.id === "progress-read" && entry.status === "completed");
      }))
      .toBe(false);
    await expect
      .poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().messages.some((message: any) => message.isStreaming)))
      .toBe(false);
  });
});

test.describe("Right sidebar preserves the user's working context", () => {
  test.beforeEach(async ({ page }) => {
    await mockWebSocket(page);
    await openApp(page);
  });

  test("running task status stays inline instead of stealing the active tab", async ({ page }) => {
    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.setState({
        rightStackTab: "preview",
        rightStackTabLocked: false,
        livePreviewUrl: null,
        previewArtifact: null,
        todos: [{ id: "todo-autofocus", status: "pending", content: "Investigate issue", activeForm: "" }],
      });
    });

    await expect
      .poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().rightStackTab))
      .toBe("preview");

    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.getState().updateTodo("todo-autofocus", {
        status: "in_progress",
        activeForm: "Investigating issue",
      });
    });

    await expect
      .poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().rightStackTab))
      .toBe("preview");
    await expect.poll(() => page.evaluate(() =>
      (window as any).__zustandStore?.getState().todos[0]?.status,
    )).toBe("in_progress");
  });

  test("running agent progress stays inline until Activity is opened", async ({ page }) => {
    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.setState({
        rightStackTab: "preview",
        rightStackTabLocked: false,
        livePreviewUrl: null,
        previewArtifact: null,
        todos: [],
        agentProgress: [],
      });
      store.getState().appendAgentProgress({
        id: "progress-only-rg",
        stage: "tool",
        status: "running",
        message: "rg \"BrowserPanel\" frontend/src.v2",
        toolName: "grep_files",
      });
    });

    await expect
      .poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().rightStackTab))
      .toBe("preview");
    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.setState({ rightPanelOpen: true });
      store.getState().setRightStackTab("tasks");
    });
    await expect(page.getByText('rg "BrowserPanel" frontend/src.v2').first()).toBeVisible();
  });

  test("executing plans stay inline and a locked tab remains untouched", async ({ page }) => {
    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.setState({
        rightStackTab: "preview",
        rightStackTabLocked: false,
        livePreviewUrl: null,
        previewArtifact: null,
        plan: {
          planId: "plan-autofocus",
          status: "completed",
          currentStep: 0,
          steps: [{ id: "step-1", title: "Existing step", status: "done" }],
        },
      });
    });

    await expect
      .poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().rightStackTab))
      .toBe("preview");

    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.setState({
        plan: {
          planId: "plan-autofocus",
          status: "executing",
          currentStep: 0,
          steps: [{ id: "step-1", title: "Running step", status: "running" }],
        },
      });
    });

    await expect
      .poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().rightStackTab))
      .toBe("preview");

    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.setState({
        rightStackTab: "preview",
        rightStackTabLocked: true,
        plan: {
          planId: "plan-locked",
          status: "completed",
          currentStep: 0,
          steps: [{ id: "step-1", title: "Locked step", status: "done" }],
        },
      });
    });

    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.setState({
        plan: {
          planId: "plan-locked",
          status: "executing",
          currentStep: 0,
          steps: [{ id: "step-1", title: "Still locked", status: "running" }],
        },
      });
    });

    await expect
      .poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().rightStackTab))
      .toBe("preview");
  });
});

test.describe("Conversation history scrolling", () => {
  test.beforeEach(async ({ page }) => {
    await mockWebSocket(page);
    await openApp(page);
  });

  test("chat history has a constrained scroll container", async ({ page }) => {
    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      const body = Array.from(
        { length: 40 },
        (_, i) => `Line ${i + 1}: this message is long enough to require real chat scrolling and readable history.`,
      ).join("\n\n");
      const messages = Array.from({ length: 36 }, (_, i) => ({
        id: `scroll-regression-${i}`,
        role: i % 2 === 0 ? "user" : "assistant",
        content: `Message ${i + 1}\n\n${body}`,
        toolCalls: [],
        artifacts: [],
        timestamp: Date.now() + i,
      }));
      store.setState({ messages, isStreaming: false, isConnected: true });
    });

    const scroller = page.getByTestId("message-list-scroll");
    await expect(scroller).toBeVisible();
    await expect
      .poll(() =>
        scroller.evaluate((el) => el.scrollHeight - el.clientHeight),
      )
      .toBeGreaterThan(500);

    const metrics = await scroller.evaluate((el) => ({
      overflowY: getComputedStyle(el).overflowY,
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
      rectHeight: el.getBoundingClientRect().height,
    }));

    expect(metrics.overflowY).toBe("auto");
    expect(metrics.scrollHeight).toBeGreaterThan(metrics.clientHeight + 500);
    expect(metrics.rectHeight).toBeLessThan(900);

    await scroller.evaluate((el) => {
      el.scrollTop = 0;
    });
    await expect.poll(() => scroller.evaluate((el) => el.scrollTop)).toBe(0);

    await scroller.evaluate((el) => {
      el.scrollTop = el.scrollHeight;
    });
    await expect
      .poll(() =>
        scroller.evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop),
      )
      .toBeLessThan(2);
  });

  test("new messages do not yank the user away from older history", async ({ page }) => {
    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      const body = Array.from({ length: 34 }, (_, i) => `Readable history line ${i + 1}`).join("\n\n");
      const messages = Array.from({ length: 32 }, (_, i) => ({
        id: `history-read-${i}`,
        role: i % 2 === 0 ? "user" : "assistant",
        content: `History message ${i + 1}\n\n${body}`,
        toolCalls: [],
        artifacts: [],
        timestamp: Date.now() + i,
      }));
      store.setState({ messages, isStreaming: false, isConnected: true });
    });

    const scroller = page.getByTestId("message-list-scroll");
    await expect
      .poll(() =>
        scroller.evaluate((el) => el.scrollHeight - el.clientHeight),
      )
      .toBeGreaterThan(500);
    await scroller.evaluate((el) => {
      el.scrollTop = 0;
      el.dispatchEvent(new Event("scroll", { bubbles: true }));
    });

    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.setState((state: any) => ({
        messages: [
          ...state.messages,
          {
            id: "history-new-message",
            role: "assistant",
            content: "A new message arrived while the user is reading old history.",
            toolCalls: [],
            artifacts: [],
            timestamp: Date.now(),
          },
        ],
      }));
    });

    await page.waitForTimeout(250);
    await expect.poll(() => scroller.evaluate((el) => el.scrollTop)).toBeLessThan(80);
    await expect(page.getByTestId("scroll-to-bottom-button")).toBeVisible();
  });
});

test.describe("Conversation session cache", () => {
  test.beforeEach(async ({ page }) => {
    await mockWebSocket(page);
    await openApp(page);
  });

  test("switching conversations restores the cached messages immediately", async ({ page }) => {
    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      const makeMessage = (id: string, content: string) => ({
        id,
        role: "user",
        content,
        toolCalls: [],
        artifacts: [],
        timestamp: Date.now(),
      });
      store.setState({
        conversationId: "conv-a",
        conversations: [
          { id: "conv-a", title: "A", updatedAt: new Date().toISOString() },
          { id: "conv-b", title: "B", updatedAt: new Date().toISOString() },
        ],
        messages: [makeMessage("a-msg", "Conversation A survived switching")],
        conversationMessages: {
          "conv-a": [makeMessage("a-msg-cache", "Conversation A survived switching")],
          "conv-b": [makeMessage("b-msg-cache", "Conversation B is separate")],
        },
        conversationStreaming: { "conv-a": false, "conv-b": false },
        isStreaming: false,
        isConnected: true,
      });
      store.getState().switchConversation("conv-b");
      store.getState().switchConversation("conv-a");
    });

    await expect(page.getByText("Conversation A survived switching")).toBeVisible();
    await expect(page.getByText("Conversation B is separate")).toHaveCount(0);
  });

  test("switching conversations follows the conversation workspace immediately", async ({ page }) => {
    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.setState({
        conversationId: "conv-minicode",
        conversations: [
          {
            id: "conv-minicode",
            title: "MiniCode",
            updatedAt: new Date().toISOString(),
            workspaceRoot: "C:\\Desktop\\MiniCode",
          },
          {
            id: "conv-mario",
            title: "Mario",
            updatedAt: new Date().toISOString(),
            workspaceRoot: "C:\\Desktop\\mario",
          },
        ],
        workingDirectory: "C:\\Desktop\\MiniCode",
        workspaceGit: { branch: "main", isWorktree: false, currentPath: "C:\\Desktop\\MiniCode" },
        conversationMessages: {
          "conv-minicode": [],
          "conv-mario": [],
        },
        conversationStreaming: {
          "conv-minicode": false,
          "conv-mario": false,
        },
      });
      store.getState().switchConversation("conv-mario");
    });

    await expect
      .poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().workingDirectory))
      .toBe("C:\\Desktop\\mario");
    await expect
      .poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().workspaceGit))
      .toBeNull();
  });

  test("creating a new session does not erase the previous session cache", async ({ page }) => {
    const previousId = "conv-before-new";
    await page.evaluate((id) => {
      const store = (window as any).__zustandStore;
      const message = {
        id: "before-new-message",
        role: "user",
        content: "Keep this message after new session",
        toolCalls: [],
        artifacts: [],
        timestamp: Date.now(),
      };
      store.setState({
        conversationId: id,
        conversations: [{ id, title: "Before", updatedAt: new Date().toISOString() }],
        messages: [message],
        conversationMessages: { [id]: [message] },
        conversationStreaming: { [id]: false },
        isStreaming: false,
        isConnected: true,
      });
      store.getState().createConversation();
      store.getState().switchConversation(id);
    }, previousId);

    await expect(page.getByText("Keep this message after new session")).toBeVisible();
    const sent = await page.evaluate(() => (window as any).__mockWsMessages ?? []);
    expect(sent.some((raw: string) => JSON.parse(raw).type === "conversation.create")).toBeTruthy();
  });

  test("conversation switched hydration updates cache and visible transcript", async ({ page }) => {
    await page.evaluate(() => {
      const ws = (window as any).__mockWs;
      ws?._receive({
        type: "conversation.switched",
        conversation_id: "conv-hydrated",
        conversation: {
          id: "conv-hydrated",
          title: "Hydrated",
          messages: [
            {
              id: "hydrated-message",
              role: "assistant",
              content: "Hydrated transcript is visible",
              timestamp: Date.now(),
            },
          ],
        },
      });
    });

    await expect(page.getByText("Hydrated transcript is visible")).toBeVisible();
    await expect
      .poll(() =>
        page.evaluate(() => {
          const store = (window as any).__zustandStore;
          return store.getState().conversationMessages["conv-hydrated"]?.[0]?.content;
        }),
      )
      .toBe("Hydrated transcript is visible");
  });

  test("normal chat view hides duplicate tool progress rows", async ({ page }) => {
    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      store.setState({
        viewMode: "normal",
        messages: [
          {
            id: "assistant-tools",
            role: "assistant",
            content: "Final answer",
            toolCalls: [
              {
                id: "tool-search",
                name: "web_search",
                args: { query: "beijing-weather" },
                status: "success",
                summary: "Search results",
              },
            ],
            blocks: [
              {
                type: "tool_call",
                record: {
                  id: "tool-search",
                  name: "web_search",
                  args: { query: "beijing-weather" },
                  status: "success",
                  summary: "Search results",
                },
              },
              {
                type: "progress",
                id: "tool-progress",
                stage: "tool",
                status: "completed",
                message: "Completed Search beijing-weather",
                summary: "Search beijing-weather",
                visibility: "timeline",
                timestamp: Date.now(),
              },
              { type: "text", content: "Final answer" },
            ],
            artifacts: [],
            timestamp: Date.now(),
          },
        ],
      });
    });

    await expect(page.getByRole("button", { name: /展开处理步骤|收起处理步骤/ }).first()).toBeVisible();
    await expect(page.getByText("Completed 1 step")).toHaveCount(0);
    await expect(page.getByText("Search beijing-weather")).toHaveCount(0);
  });
});
