import { expect, Page, test } from "@playwright/test";

// 仿真 WebSocket 交互，模拟流式事件并拦截客户端发送的命令
async function mockCellWebSocket(page: Page) {
  await page.addInitScript(() => {
    const messages: string[] = [];

    // 手写坚若磐石的模拟 WebSocket 事件机制，消除任何继承兼容性断层
    class MockWebSocket {
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

      listeners: Record<string, Function[]> = {};

      constructor(url: string) {
        this.url = url;
        (window as any).__mockWs = this;
        (window as any).__mockWsMessages = messages;

        // 在 20ms 后自动触发成功握手事件以点亮 UI 连接指示灯
        setTimeout(() => {
          this.readyState = 1;
          const ev = new Event("open");
          this.onopen?.(ev);
          this.dispatchEvent(ev);
          
          this._receive({
            type: "conversation.list",
            conversations: [{ id: "conv-cells", title: "Cells", updated_at: "2026-07-11T00:00:00.000Z" }],
            active_conversation_id: "conv-cells",
            active_conversation: { id: "conv-cells", title: "Cells", updated_at: "2026-07-11T00:00:00.000Z", messages: [] },
          });
          this._receive({
            type: "llm.model.updated",
            model: "gpt-4",
            current_model: "gpt-4",
            available_models: ["gpt-4"],
            working_directory: "C:\\Desktop\\MiniCode",
          });
        }, 30);
      }

      addEventListener(type: string, handler: Function) {
        if (!this.listeners[type]) this.listeners[type] = [];
        this.listeners[type].push(handler);
      }

      removeEventListener(type: string, handler: Function) {
        if (!this.listeners[type]) return;
        this.listeners[type] = this.listeners[type].filter(h => h !== handler);
      }

      dispatchEvent(event: Event): boolean {
        const type = event.type;
        if (this.listeners[type]) {
          for (const handler of this.listeners[type]) {
            try {
              handler(event);
            } catch (e) {
              console.error(e);
            }
          }
        }
        return true;
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

        // 如果用户发送了消息，我们模拟下发带有 planning 活动和 tool/diff 的响应流
        if (parsed.type === "user_message") {
          const extendedToolChain = String(parsed.content || parsed.message || "").includes("完整工具链");
          const owner = {
            conversation_id: parsed.conversation_id || "conv-cells",
            message_id: parsed.assistant_message_id,
          };
          // 1. 在 10ms 时，首先下发流式开启包，建立 Assistant 消息
          setTimeout(() => {
            this._receive({
              type: "thinking_delta",
              content: "好的，让我来分析当前的前端渲染逻辑。",
              ...owner,
            });
          }, 10);

          // 2. 接着下发 planning progress 和 plan.update
          setTimeout(() => {
            this._receive({
              type: "agent.progress",
              id: "planning-step",
              stage: "planning",
              status: "running",
              message: "正在制定执行计划",
              ...owner,
            });
            
            this._receive({
              type: "plan_updated",
              plan_id: "plan-12345",
              status: "executing",
              steps: [
                { id: "step-1", title: "Inspect frontend", status: "running" },
                { id: "step-2", title: "Refine rendering", status: "pending" }
              ],
              ...owner,
            });
          }, 50);

          setTimeout(() => {
            // 3. 模拟工具调用 (read_file) 过程
            this._receive({
              type: "tool_call",
              id: "read-readme",
              name: "read_file",
              args: { path: "README.md" },
              started_at: Date.now(),
              ...owner,
            });
            this._receive({
              type: "tool_call",
              id: "read-package",
              name: "read_file",
              args: { path: "package.json" },
              started_at: Date.now(),
              ...owner,
            });
          }, 90);

          setTimeout(() => {
            // 4. 模拟工具调用结果
            this._receive({
              type: "tool_result",
              id: "read-readme",
              summary: "Read README.md successfully",
              content_preview: "# MiniCode\nIndependent harness.",
              display_summary: "已读取 README.md",
              result_kind: "file",
              evidence_type: "file",
              extraction_status: "ok",
              activity_kind: "fileRead",
              duration_ms: 150,
              ...owner,
            });
            this._receive({
              type: "tool_result",
              id: "read-package",
              summary: "Read package.json successfully",
              content_preview: "{\"name\":\"minicode-frontend\"}",
              display_summary: "已读取 package.json",
              result_kind: "file",
              evidence_type: "file",
              extraction_status: "ok",
              activity_kind: "fileRead",
              duration_ms: 120,
              ...owner,
            });

            if (extendedToolChain) {
              this._receive({
                type: "tool_call",
                id: "search-frontend",
                name: "search_files",
                args: { query: "ActivityCell", path: "frontend/src.v2" },
                started_at: Date.now(),
                result_kind: "search",
                activity_kind: "workspaceSearch",
                ...owner,
              });
              this._receive({
                type: "tool_call",
                id: "fetch-docs",
                name: "web_fetch",
                args: { url: "https://example.com/minicode" },
                started_at: Date.now(),
                result_kind: "web",
                activity_kind: "webSearch",
                ...owner,
              });
              this._receive({
                type: "tool_call",
                id: "command-status",
                name: "run_command",
                args: { command: "git status --short" },
                started_at: Date.now(),
                result_kind: "command",
                activity_kind: "commandExecution",
                ...owner,
              });
            }

            // 5. 模拟文件修改产生 diff 的记录
            this._receive({
              type: "tool_call",
              id: "edit-app",
              name: "edit_file",
              args: { path: "src.v2/App.tsx" },
              started_at: Date.now(),
              ...owner,
            });
          }, 130);

          if (extendedToolChain) {
            setTimeout(() => {
              this._receive({
                type: "tool_result",
                id: "search-frontend",
                summary: "frontend/src.v2/chat/cells/ActivityCell.tsx:1:ActivityCell",
                content_preview: "frontend/src.v2/chat/cells/ActivityCell.tsx:1:ActivityCell",
                display_summary: "已搜索 ActivityCell",
                result_kind: "search",
                activity_kind: "workspaceSearch",
                duration_ms: 90,
                ...owner,
              });
              this._receive({
                type: "tool_result",
                id: "fetch-docs",
                summary: "MiniCode harness rendering guide",
                content_preview: "MiniCode harness rendering guide",
                display_summary: "已获取网页",
                result_kind: "web",
                activity_kind: "webSearch",
                source_url: "https://example.com/minicode",
                duration_ms: 110,
                ...owner,
              });
              this._receive({
                type: "tool_result",
                id: "command-status",
                summary: "?? frontend/src.v2/chat/cells/ActivityCell.tsx",
                display_summary: "已运行命令 git status --short",
                result_kind: "command",
                activity_kind: "commandExecution",
                duration_ms: 130,
                ...owner,
              });
            }, 150);
          }

          setTimeout(() => {
            this._receive({
              type: "tool_result",
              id: "edit-app",
              summary: "Modified App.tsx",
              display_summary: "已修改 App.tsx",
              result_kind: "edit",
              evidence_type: "file",
              extraction_status: "ok",
              activity_kind: "fileChange",
              diff: { plus: 15, minus: 5, patch: "@@ -1,5 +1,15 @@\n+added lines\n-deleted lines" },
              duration_ms: 200,
              ...owner,
            });

            this._receive({
              type: "tool_call",
              id: "edit-index",
              name: "edit_file",
              args: { path: "index.html" },
              started_at: Date.now(),
              ...owner,
            });
          }, 170);

          setTimeout(() => {
            if ((window as any).__suppressMockCompletion) return;
            this._receive({
              type: "tool_result",
              id: "edit-index",
              summary: "Modified index.html",
              display_summary: "已修改 index.html",
              result_kind: "edit",
              evidence_type: "file",
              extraction_status: "ok",
              activity_kind: "fileChange",
              diff: { plus: 3, minus: 1, patch: "@@ -1,2 +1,3 @@\n+welcome text" },
              duration_ms: 80,
              ...owner,
            });

            // 6. 结束流式，生成最终回答
            this._receive({
              type: "item.started",
              item: { id: "cell-answer", type: "agent_message" },
              ...owner,
            });
            this._receive({
              type: "agent_message.delta",
              item_id: "cell-answer",
              delta: "我已经为您成功制定了计划并分析了当前结构。",
              ...owner,
            });
            this._receive({
              type: "item.completed",
              item: { id: "cell-answer", type: "agent_message", text: "我已经为您成功制定了计划并分析了当前结构。", status: "completed" },
              ...owner,
            });

            this._receive({
              type: "done",
              status: "completed",
              usage: { input_tokens: 10, output_tokens: 15, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, input_includes_cache_read: false },
              ...owner,
            });
          }, 210);
        }
      }

      close() {
        this.readyState = 3;
        const ev = new Event("close");
        this.onclose?.(ev as CloseEvent);
        this.dispatchEvent(ev);
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

test.describe("MiniCode New Cell UI & Interactive Flow E2E Tests", () => {
  test.beforeEach(async ({ page }) => {
    // 1. 无死角精确匹配拦截所有以 /api/ 开头的网络请求
    await page.route(url => url.toString().includes("/api/"), async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true, status: "ok", available_models: ["gpt-4"] }),
      });
    });

    // 2. 强制设定不使用 legacy UI，确保必定渲染我们的新版 Cell UI
    await page.addInitScript(() => {
      localStorage.setItem("minicode.legacyMessageUi", "0");
    });
    await mockCellWebSocket(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => Boolean((window as any).__mockWs));
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().isConnected)).toBe(true);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().currentModel || "")).not.toBe("");
  });

  test("verifies inline plan progress and context routing", async ({ page }) => {
    await page.evaluate(() => {
      (window as any).__suppressMockCompletion = true;
    });
    const composer = page.locator('textarea, input[placeholder*="message" i], [contenteditable="true"]').first();
    await composer.fill("分析前端输出，不要修改文件");
    await composer.press("Enter");
    await page.waitForTimeout(120);

    await page.evaluate(() => {
      const store = (window as any).__zustandStore;
      const state = store.getState();
      const threadId = String(state.conversationId || "conv-cells");
      const turnId = "turn-plan-ui";
      const messages = state.messages.map((message: any) => (
        message.role === "assistant" && message.isStreaming
          ? { ...message, turnId, isStreaming: true }
          : message
      ));
      store.setState({ messages, isStreaming: true });
      store.getState().setPlan({
        threadId,
        turnId,
        plan: [
          { step: "Inspect frontend", status: "in_progress" },
          { step: "Refine rendering", status: "pending" },
        ],
      }, threadId);
    });

    const planProgress = page.getByRole("button", { name: /第 1 \/ 2 步/ });
    await expect(planProgress).toBeVisible({ timeout: 8000 });
    await planProgress.click();
    const planRegion = page.getByRole("dialog", { name: "当前计划" });
    await expect(planRegion).toBeVisible();
    await expect(planRegion.getByText("Inspect frontend")).toBeVisible();
    await expect(planRegion.getByText("Refine rendering")).toBeVisible();

    await page.getByRole("button", { name: "打开右侧栏" }).click();
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().rightStackTab)).toBe("tasks");
  });

  test("verifies ActivityCell flat tool projection and inline read records", async ({ page }) => {
    const composer = page.locator('textarea, input[placeholder*="message" i], [contenteditable="true"]').first();
    await composer.fill("分析前端输出，不要修改文件");
    await composer.press("Enter");

    // Completed activity is collapsed by default, like Codex Desktop.
    const activityHeader = page.getByRole("button", { name: "展开处理步骤" });
    await expect(activityHeader).toBeVisible({ timeout: 8000 });
    await expect(activityHeader).toHaveAttribute("aria-expanded", "false");

    // The work group expands independently; each Read/command/edit row keeps
    // its own disclosure panel so ordered records remain inspectable without
    // flooding the timeline.
    await activityHeader.click();
    const collapseActivity = page.getByRole("button", { name: "收起处理步骤" });
    await expect(collapseActivity).toHaveAttribute("aria-expanded", "true");

    const workGroup = page.locator('.agent-loop-timeline-group[data-group-kind="work"]');
    await expect(workGroup).toHaveCount(1);
    await workGroup.getByRole("button").click();

    const readCells = page.locator('.activity-cell[data-activity-kind="fileRead"]');
    await expect(readCells).toHaveCount(2);
    const readReadme = readCells.nth(0);
    const readPackage = readCells.nth(1);
    await expect(readReadme.locator(".activity-cell-tool-detail-card")).toHaveCount(0);
    await expect(readPackage.locator(".activity-cell-tool-detail-card")).toHaveCount(0);
    await readReadme.getByRole("button", { name: "展开活动详情" }).click();
    await readPackage.getByRole("button", { name: "展开活动详情" }).click();
    await expect(readReadme.locator(".activity-cell-tool-detail-card")).toHaveCount(1);
    await expect(readPackage.locator(".activity-cell-tool-detail-card")).toHaveCount(1);
    await expect(readReadme.getByText("README.md").first()).toBeVisible();
    await expect(readPackage.getByText("package.json").first()).toBeVisible();
    await expect(readReadme.locator(".activity-cell-tool-detail-card")).toContainText("# MiniCode");
    await expect(readReadme.locator(".activity-cell-tool-detail-card")).not.toContainText("minicode-frontend");
    await expect(readPackage.locator(".activity-cell-tool-detail-card")).toContainText("minicode-frontend");
    await expect(readPackage.locator(".activity-cell-tool-detail-card")).not.toContainText("Independent harness");
    await expect(page.locator(".activity-cell-inline-records")).toHaveCount(0);

    const readPanelStyle = await readReadme.locator(".activity-cell-tool-expanded").evaluate((node) => {
      const style = getComputedStyle(node);
      const copy = getComputedStyle(node.querySelector(".activity-cell-inline-output")!);
      const row = getComputedStyle(node.closest(".activity-cell")!.querySelector(".activity-cell-main-button")!);
      return {
        borderWidth: style.borderWidth,
        boxShadow: style.boxShadow,
        copyFontSize: Number.parseFloat(copy.fontSize),
        rowFontSize: Number.parseFloat(row.fontSize),
      };
    });
    expect(readPanelStyle.borderWidth).toBe("1px");
    expect(readPanelStyle.boxShadow).toBe("none");
    expect(readPanelStyle.copyFontSize).toBeGreaterThanOrEqual(15);
    expect(readPanelStyle.rowFontSize).toBeGreaterThanOrEqual(16);

    await page.evaluate(() => (window as any).__zustandStore.getState().setThemeMode("dark"));
    const darkReadPanelStyle = await readReadme.locator(".activity-cell-tool-expanded").evaluate((node) => {
      const panel = getComputedStyle(node);
      const output = getComputedStyle(node.querySelector(".activity-cell-inline-output")!);
      return {
        panelBorderWidth: panel.borderWidth,
        panelBoxShadow: panel.boxShadow,
        outputBorderWidth: output.borderWidth,
        outputBackground: output.backgroundColor,
      };
    });
    expect(darkReadPanelStyle.panelBorderWidth).toBe("1px");
    expect(darkReadPanelStyle.panelBoxShadow).toBe("none");
    expect(darkReadPanelStyle.outputBorderWidth).toBe("1px 0px 0px");
    expect(darkReadPanelStyle.outputBackground).toBe("rgba(0, 0, 0, 0)");

    const firstEdit = page.locator('.activity-cell[data-activity-kind="fileChange"]').first();
    await firstEdit.getByRole("button", { name: "展开活动详情" }).click();
    await expect(firstEdit.locator(".activity-cell-change-card")).toHaveCount(1);
    await expect(firstEdit.locator(".inline-diff")).toBeVisible();
    await collapseActivity.click();
    await expect(readCells.locator(".activity-cell-tool-detail-card")).toHaveCount(0);
  });

  test("keeps one inline aggregate diff at the end of the process trace", async ({ page }) => {
    const composer = page.locator('textarea, input[placeholder*="message" i], [contenteditable="true"]').first();
    await composer.fill("分析前端输出，不要修改文件");
    await composer.press("Enter");

    const processToggle = page.getByRole("button", { name: "展开处理步骤" });
    await expect(processToggle).toBeVisible({ timeout: 8000 });
    await processToggle.click();
    const processArea = page.getByRole("region", { name: "Agent 处理进度" });
    const replyArea = page.getByRole("region", { name: "Agent 回复" });
    // File mutations remain chronological activity rows in the process trace.
    // The aggregate diff is the durable outcome rendered after the answer.
    const workGroup = processArea.getByRole("region", { name: "读取了文件并编辑了文件" });
    await expect(workGroup).toBeVisible();
    await workGroup.getByRole("button").click();
    await expect(processArea.getByText("已编辑", { exact: true })).toHaveCount(2);
    await expect(processArea.locator(".diff-cell")).toHaveCount(0);
    await expect(replyArea.locator(".diff-cell")).toHaveCount(0);
    const outcome = page.locator('[data-zone="diff"]');
    await expect(outcome.locator(".diff-cell")).toHaveCount(1);
    await expect(outcome.getByText("已编辑 2 个文件")).toBeVisible();

    await expect(outcome.getByText("src.v2/App.tsx", { exact: true })).toBeVisible();
    await expect(outcome.getByText("index.html", { exact: true })).toBeVisible();
    await expect(outcome.locator(".diff-file-section")).toHaveCount(2);
    await expect(outcome.locator(".diff-file-toggle")).toHaveCount(0);
    await expect(outcome.locator(".inline-diff-line-added")).toHaveCount(0);
    await expect(outcome.getByRole("button", { name: "审核" })).toBeVisible();
    await expect(outcome.getByRole("button", { name: "撤销" })).toBeVisible();

    const outcomeStyle = await outcome.evaluate((node) => {
      const style = getComputedStyle(node);
      const title = getComputedStyle(node.querySelector(".diff-cell-title")!);
      const file = getComputedStyle(node.querySelector(".diff-cell-file-path")!);
      return {
        borderWidth: style.borderWidth,
        boxShadow: style.boxShadow,
        titleFontSize: Number.parseFloat(title.fontSize),
        fileFontSize: Number.parseFloat(file.fontSize),
      };
    });
    expect(outcomeStyle.borderWidth).toBe("0px");
    expect(outcomeStyle.boxShadow).toBe("none");
    expect(outcomeStyle.titleFontSize).toBeGreaterThanOrEqual(16);
    expect(outcomeStyle.fileFontSize).toBeGreaterThanOrEqual(16);
  });

  test("projects Search, Fetch, and git status in one ordered expandable tool stream", async ({ page }) => {
    const composer = page.locator('textarea, input[placeholder*="message" i], [contenteditable="true"]').first();
    await composer.fill("验证完整工具链投影");
    await composer.press("Enter");

    const processToggle = page.getByRole("button", { name: "展开处理步骤" });
    await expect(processToggle).toBeVisible({ timeout: 8000 });
    await processToggle.click();

    const workGroup = page.locator('.agent-loop-timeline-group[data-group-kind="work"]');
    await expect(workGroup).toHaveCount(1);
    await expect(workGroup).toContainText("读取了文件");
    await expect(workGroup).toContainText("获取网页");
    await expect(workGroup).toContainText("运行了命令");
    await expect(workGroup).toContainText("编辑了文件");
    await workGroup.getByRole("button").click();

    const searchCell = page.locator('.activity-cell[data-activity-kind="workspaceSearch"]');
    await expect(searchCell).toHaveCount(1);
    await searchCell.getByRole("button", { name: "展开活动详情" }).click();
    await expect(searchCell.locator(".activity-cell-tool-detail-card")).toHaveCount(1);
    await expect(page.locator('.activity-cell[data-activity-kind="fileRead"]')).toHaveCount(2);
    await expect(searchCell.getByText("Search").first()).toBeVisible();
    await expect(searchCell).toContainText("ActivityCell");

    const fetchCell = page.locator('.activity-cell[data-activity-kind="webSearch"]');
    await expect(fetchCell).toHaveCount(1);
    await fetchCell.getByRole("button", { name: "展开活动详情" }).click();
    await expect(fetchCell.locator(".activity-cell-tool-detail-card")).toHaveCount(1);
    await expect(fetchCell).toContainText("https://example.com/minicode");
    await expect(fetchCell).toContainText("MiniCode harness rendering guide");

    const commandCell = page.locator('.exec-cell[data-status="success"]');
    await expect(commandCell).toHaveCount(1);
    await commandCell.getByRole("button", { name: "展开命令详情" }).click();
    await expect(commandCell.locator(".exec-cell-expanded")).toBeVisible();
    await expect(commandCell).toContainText("git status --short");
    await expect(commandCell).toContainText("frontend/src.v2/chat/cells/ActivityCell.tsx");

    const order = await page.evaluate(() => [...document.querySelectorAll(".agent-loop-process-cell")]
      .map((node) => node.querySelector("[data-activity-kind]")?.getAttribute("data-activity-kind")
        || (node.querySelector(".exec-cell") ? "exec-cell" : node.className))
      .filter(Boolean));
    expect(order.findIndex((value) => value.includes("workspaceSearch"))).toBeLessThan(order.findIndex((value) => value.includes("webSearch")));
    expect(order.findIndex((value) => value.includes("webSearch"))).toBeLessThan(order.findIndex((value) => value.includes("exec-cell")));
  });
});
