import { expect, test, type Page } from "@playwright/test";
import { initialSessionSyncSnapshot } from "../../test/initial-session-sync";

test.use({ locale: "zh-CN" });

let unexpectedApiRequests: string[];
let holdUserReceipt = false;
let releaseUserReceipt: (() => void) | undefined;

// Exercise the complete browser shell and real CSS with an isolated transport.
test.beforeEach(async ({ page }) => {
  unexpectedApiRequests = [];
  holdUserReceipt = false;
  releaseUserReceipt = undefined;
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.route("**/api/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const payloads: Record<string, unknown> = {
      "/api/thread-metadata/sections": { sections: [] },
      "/api/settings/feature-flags": { flags: [{ name: "plugins", source: "default" }], effective: { plugins: { enabled: false } } },
      "/api/plugins": { plugins: [] },
      "/api/llm/settings": { provider: "custom", active_model: "fixture-model", provider_history: [],
        custom: { model: "fixture-model", available_models: ["fixture-model"], wire_api: "chat", has_api_key: false } },
      "/api/llm/draft-selection": { current_model: "fixture-model", available_models: ["fixture-model"], models_source: "configured",
        available_permission_modes: ["confirm"], config_version: "summary-fixture", config_cwd: url.searchParams.get("config_cwd") || "",
        feature_flags: {}, composer_commands: [] },
      "/api/workspace/git/worktree": { is_git_repo: false, current_path: url.searchParams.get("workspace_root"),
        current_branch: "", is_worktree: false, worktree_count: 0, worktrees: [] },
      "/api/workspace/local-environment": { config_path: "", revision: "", environment: null, actions: [] },
    };
    if (request.method() === "GET" && Object.hasOwn(payloads, url.pathname)) {
      await route.fulfill({ json: payloads[url.pathname] });
    } else {
      unexpectedApiRequests.push(`${request.method()} ${url.pathname}`);
      await route.fulfill({ status: 501, json: { error: `Unconfigured summary fixture endpoint: ${url.pathname}` } });
    }
  });
  await page.routeWebSocket(/\/ws(?:\?|$)/, socket => {
    socket.onMessage(raw => {
      const command = JSON.parse(String(raw));
      if (command.type === "user_message" && holdUserReceipt) {
        releaseUserReceipt = () => {
          socket.send(JSON.stringify({ type: "client.command.ack", client_command_id: command.client_command_id,
            command_type: command.type, accepted: true }));
          socket.send(JSON.stringify({ type: "agent.run.started", role: "main", conversation_id: command.conversation_id,
            message_id: command.assistant_message_id, run_id: "receipt-run", turn_id: "receipt-turn", status: "running", started_at: Date.now() }));
          socket.send(JSON.stringify({ type: "command.result", command: "user_message", level: "success", message: "",
            client_command_id: command.client_command_id, data: { admitted: true, conversation_id: command.conversation_id } }));
        };
        return;
      }
      if (command.client_command_id) socket.send(JSON.stringify({ type: "client.command.ack",
        client_command_id: command.client_command_id, command_type: command.type, accepted: true }));
      if (command.type === "session.sync" || command.type === "session.restore") {
        socket.send(JSON.stringify(initialSessionSyncSnapshot("summary-browser", command.client_command_id)));
      }
      if (command.type === "ping") socket.send(JSON.stringify({ type: "pong" }));
    });
  });
  await page.goto("/");
  await page.waitForFunction(() => (window as any).__zustandStore?.getState().isConnected);
  await page.evaluate(() => {
    const store = (window as any).__zustandStore;
    store.setState({ appMode: "cowork", workingDirectory: "C:/summary-workspace", conversationId: "summary-browser",
      conversations: [{ id: "summary-browser", title: "Summary browser", updatedAt: "2026-10-24T00:00:00Z" }],
      messages: [], subagents: [{ id: "summary-audit", status: "done", role: "reviewer", objective: "Layout audit" }],
      backgroundTasks: [], leftSidebarWidth: 0, rightPanelOpen: false,
      rightPanelExpanded: false, rightStackTab: "subagents", rightSidebarWidth: 420, rightSidebarWidthRatio: null,
      contextCardCollapsed: true, contextCardPinned: false, pendingConversationSwitchId: null,
      compactPanel: null,
      panelSlots: [{ id: "main-chat", kind: "chat", focused: true }], dockCollapsed: true,
    });
  });
  const input = page.getByRole("textbox", { name: "消息输入", exact: true });
  await input.fill("Keep this unsent summary draft");
  await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().draft))
    .toBe("Keep this unsent summary draft");
});
test.afterEach(() => expect(unexpectedApiRequests).toEqual([]));

test("the user prompt is painted while receipt is held without inventing an assistant run", async ({ page }, testInfo) => {
  holdUserReceipt = true;
  const prompt = "Keep this unsent summary draft";
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.locator(".user-cell-bubble")).toHaveText(prompt);
  expect(releaseUserReceipt).toBeDefined();
  expect(await page.evaluate(() => {
    const state = (window as any).__zustandStore.getState();
    return { roles: state.messages.map((message: any) => message.role), streaming: state.isStreaming, draft: state.draft };
  })).toEqual({ roles: ["user"], streaming: false, draft: "" });
  await expect(page.getByRole("textbox", { name: "消息输入", exact: true })).toHaveText("");
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
  await page.screenshot({ path: testInfo.outputPath("prompt-before-receipt.png") });
  releaseUserReceipt!();
  await expect(page.getByRole("textbox", { name: "消息输入", exact: true })).toHaveText("");
  await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().messages.map((message: any) => message.role)))
    .toEqual(["user", "assistant"]);
  await expect(page.locator(".user-cell-bubble")).toHaveCount(1);
});

test("a delayed receipt cannot erase newly typed text identical to the submitted message", async ({ page }) => {
  holdUserReceipt = true;
  const input = page.getByRole("textbox", { name: "消息输入", exact: true });
  const prompt = await input.innerText();
  await input.press("Enter");
  await expect(input).toHaveText("");
  await expect(page.locator(".user-cell-bubble")).toHaveText(prompt);
  await input.fill(prompt);
  releaseUserReceipt!();
  await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().messages.map((message: any) => message.role)))
    .toEqual(["user", "assistant"]);
  await expect(input).toHaveText(prompt);
  await expect(page.locator(".user-cell-bubble")).toHaveCount(1);
});

async function finishMotion(page: Page) {
  await page.evaluate(async () => {
    await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
    await Promise.allSettled(document.getAnimations().filter(animation => animation.playState === "running"
      && animation.effect?.getComputedTiming().iterations !== Infinity).map(animation => animation.finished));
  });
}

test("summary and right pane coexist across real ResizeObserver thresholds without changing widths or drafts", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const toggle = page.getByRole("button", { name: "切换摘要", exact: true });
  const summary = page.locator("#thread-summary-panel");
  const sidebar = page.locator("#workbench-right-sidebar");
  const input = page.getByRole("textbox", { name: "消息输入", exact: true });
  await expect(input).toHaveText("Keep this unsent summary draft");
  await toggle.click();
  await expect(summary).toHaveAttribute("data-surface", "inline");
  await summary.getByRole("button", { name: "子智能体", exact: true }).click();
  await expect(summary).toHaveCount(0);
  await expect(toggle).toHaveAttribute("aria-pressed", "false");
  expect(await page.evaluate(() => (window as any).__zustandStore.getState().contextCardPinned)).toBe(true);
  await toggle.click();
  await expect(summary).toHaveAttribute("data-display-mode", "overlay");
  await expect(summary).toHaveAttribute("data-surface", "popover");
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  await finishMotion(page);
  await page.screenshot({ path: testInfo.outputPath("summary-right-popover.png") });
  const widths = await page.evaluate(() => {
    const card = document.querySelector("#thread-summary-panel")!.getBoundingClientRect();
    const pane = document.querySelector(".chat-pane-main")!.getBoundingClientRect();
    const chat = document.querySelector(".chat-pane")!.getBoundingClientRect();
    const sidebar = document.querySelector("#workbench-right-sidebar")!.getBoundingClientRect();
    return { cardWidth: card.width, mainWidth: pane.width, chatWidth: chat.width,
      cardRight: card.right, sidebarLeft: sidebar.left, sidebarWidth: sidebar.width };
  });
  expect(widths.cardWidth).toBeCloseTo(306, 0);
  expect(widths.mainWidth).toBeGreaterThan(350);
  expect(widths.mainWidth).toBeCloseTo(widths.chatWidth, 0);
  expect(widths.cardRight).toBeLessThanOrEqual(widths.sidebarLeft);
  expect(widths.sidebarWidth).toBeCloseTo(420, 0);
  const tab = sidebar.getByRole("tab").first();
  await tab.click();
  await tab.focus();
  await expect(tab).toBeFocused();
  await expect(summary).toBeVisible();
  await input.focus();
  await expect(input).toBeFocused();
  await expect(input).toHaveText("Keep this unsent summary draft");
  for (const [width, mode] of [[2200, "gutter"], [1600, "shift"], [1440, "overlay"]] as const) {
    await page.setViewportSize({ width, height: 1000 });
    await expect(summary).toHaveAttribute("data-display-mode", mode);
    await expect(summary).toHaveAttribute("data-surface", mode === "overlay" ? "popover" : "inline");
    await expect(toggle).toHaveAttribute("aria-pressed", "true");
    await finishMotion(page);
    await page.screenshot({ path: testInfo.outputPath(`summary-right-${mode}.png`) });
  }
  await toggle.click();
  await expect(summary).toHaveCount(0);
  await expect(sidebar).toBeVisible();
  expect(await page.evaluate(() => {
    const state = (window as any).__zustandStore.getState();
    return { rightPanelOpen: state.rightPanelOpen, width: state.rightSidebarWidth, ratio: state.rightSidebarWidthRatio,
      draft: state.draft, pinned: state.contextCardPinned };
  })).toEqual({ rightPanelOpen: true, width: 420, ratio: null, draft: "Keep this unsent summary draft", pinned: true });
  expect(errors).toEqual([]);
});

test("manual unpinned summary coexists with an already open right pane and keeps its selection on tab clicks", async ({ page }) => {
  const summary = page.locator("#thread-summary-panel");
  const toggle = page.getByRole("button", { name: "切换摘要", exact: true });
  await page.getByRole("button", { name: "视图", exact: true }).click();
  await page.getByRole("menuitem", { name: "打开右侧栏", exact: true }).click();
  await expect(summary).toHaveCount(0);
  await expect.poll(() => page.locator(".chat-pane").evaluate(element => element.clientWidth)).toBeLessThan(1096);
  await toggle.click();
  await expect(summary).toHaveAttribute("data-surface", "popover");
  await expect(summary).toHaveCSS("width", "306px");
  await summary.getByRole("button", { name: "子智能体", exact: true }).click();
  await expect(summary).toHaveAttribute("data-surface", "popover");
  await page.locator("#workbench-right-sidebar").getByRole("tab").first().click();
  await expect(summary).toBeVisible();
  expect(await page.evaluate(() => (window as any).__zustandStore.getState().contextCardPinned)).toBe(false);
  await page.getByRole("textbox", { name: "消息输入", exact: true }).click();
  await expect(summary).toHaveCount(0);
  await expect(toggle).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator("#workbench-right-sidebar")).toBeVisible();
  await toggle.click();
  await expect(summary).toHaveAttribute("data-surface", "popover");
  await page.locator("#workbench-right-sidebar").getByRole("button", { name: "关闭右侧栏", exact: true }).click();
  await expect(summary).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).__zustandStore.getState().contextCardPinned)).toBe(false);
});

test("reopening the selected compact drawer hides the manually reopened summary without changing the saved pane choice", async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 900 });
  const summary = page.locator("#thread-summary-panel");
  const toggle = page.getByRole("button", { name: "切换摘要", exact: true });
  await toggle.click();
  await expect(summary).toHaveAttribute("data-surface", "popover");
  await summary.getByRole("button", { name: "子智能体", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "右侧面板", exact: true })).toBeVisible();
  await expect(summary).toHaveCount(0);
  await page.getByRole("button", { name: "关闭右侧面板", exact: true }).click();
  await toggle.click();
  await expect(summary).toHaveAttribute("data-surface", "popover");
  await summary.getByRole("button", { name: "子智能体", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "右侧面板", exact: true })).toBeVisible();
  await expect(summary).toHaveCount(0);
  expect(await page.evaluate(() => {
    const state = (window as any).__zustandStore.getState();
    return { rightPanelOpen: state.rightPanelOpen, tab: state.rightStackTab, compactPanel: state.compactPanel,
      pinned: state.contextCardPinned, draft: state.draft };
  })).toEqual({ rightPanelOpen: true, tab: "subagents", compactPanel: "right", pinned: false, draft: "Keep this unsent summary draft" });
});

test("restored coordination waits add no transcript rows beside real work", async ({ page }, testInfo) => {
  await page.evaluate(() => {
    const waits = Array.from({ length: 5 }, (_, index) => ({ type: "tool_call", record: {
      id: `wait-${index}`, name: "wait_agent", args: { timeout_ms: 30000 }, status: "success",
      activityKind: "commandExecution", displaySummary: "Wait for agents", startedAt: 1000 + index * 1000,
      finishedAt: 2000 + index * 1000, outputPreview: '{"message":"Wait completed.","timed_out":false}',
    } }));
    (window as any).__zustandStore.setState({ messages: [
      { id: "wait-user", role: "user", content: "Review the actual file", timestamp: 1000 },
      { id: "wait-answer", role: "assistant", content: "The review is complete.", timestamp: 8000,
        isStreaming: false, terminalStatus: "completed", blocks: [
          ...waits,
          { type: "tool_call", record: { id: "actual-read", name: "read_file", args: { file_path: "fact.txt" },
            status: "success", activityKind: "fileRead", startedAt: 6000, finishedAt: 7000,
            outputPreview: "Actual file content" } },
          { type: "text", itemId: "wait-final", content: "The review is complete.", source: "reply", status: "completed" },
        ] },
    ] });
  });
  await expect(page.getByText("The review is complete.", { exact: true })).toBeVisible();
  const process = page.getByRole("region", { name: "Agent 处理进度", exact: true });
  const toggle = process.locator("button[aria-expanded]").first();
  if (await toggle.getAttribute("aria-expanded") === "false") await toggle.click();
  await expect(process.getByText("fact.txt", { exact: true })).toBeVisible();
  await expect(page.getByText("Wait for agents", { exact: true })).toHaveCount(0);
  await expect(page.getByText('"timed_out"', { exact: false })).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).__zustandStore.getState().messages[1].blocks
    .filter((block: any) => block.type === "tool_call" && block.record.name === "wait_agent").length)).toBe(5);
  await finishMotion(page);
  await page.screenshot({ path: testInfo.outputPath("coordination-waits-omitted.png") });
});
