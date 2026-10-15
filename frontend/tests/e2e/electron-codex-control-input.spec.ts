/** Only model replies are controlled; desktop, sidecar, tools, journal, WS and UI are production code. */
import { _electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import { createRequire } from "node:module";
import { createServer } from "node:http";
import { createServer as portServer } from "node:net";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

type Fact = Record<string, any>;
const root = path.resolve(import.meta.dirname, "../../..");
const evidence = path.resolve(process.env.MINICODE_CONTROL_E2E_EVIDENCE_ROOT || path.join(root, "output/playwright/codex-control-audit-20261013/production-control-run01"));
const model = "control-loopback";
const questionArgs = { questions: [
  { id: "area", header: "Area", question: "Which area should the harness verify?", options: [
    { label: "Parser", description: "Read and edit a real file." }, { label: "Runner", description: "Execute a real command." },
  ] },
  { id: "detail", header: "Detail", question: "Which evidence should be retained?", options: [
    { label: "File bytes", description: "Retain the exact file contents." }, { label: "Exit status", description: "Retain the actual exit code." },
  ] },
] };
const text = (message: Fact): string => typeof message.content === "string" ? message.content : (message.content || []).map((part: Fact) => part.text || "").join("\n");

class ControlledProvider {
  readonly requests: Fact[] = [];
  readonly counts = new Map<string, number>();
  readonly server = createServer(async (request, response) => {
    if (request.method === "GET") { response.setHeader("content-type", "application/json"); response.end(JSON.stringify({ data: [{ id: model, object: "model" }] })); return; }
    const bytes = []; for await (const chunk of request) bytes.push(Buffer.from(chunk));
    const body = JSON.parse(Buffer.concat(bytes).toString("utf8"));
    const user = body.messages.filter((item: Fact) => item.role === "user").map(text)
      .findLast((value: string) => /HARNESS_(?:PLAN|CANCEL_INPUT|CANONICAL_INPUT|CONTEXT_RESET|SIDE_CHAT)|PLEASE IMPLEMENT THIS PLAN:/.test(value)) || "";
    const route = user.includes("PLEASE IMPLEMENT THIS PLAN:") ? "implement" : /HARNESS_PLAN/.test(user) ? "plan"
      : /HARNESS_CANCEL_INPUT/.test(user) ? "cancel" : /HARNESS_CANONICAL_INPUT/.test(user) ? "input"
      : /HARNESS_CONTEXT_RESET/.test(user) ? "context" : /HARNESS_SIDE_CHAT/.test(user) ? "side"
      : body.messages.some((item: Fact) => ["developer", "system"].includes(item.role) && text(item).includes("Previous context window id:")) ? "context-followup" : "auxiliary";
    const tools = body.tools?.map((tool: Fact) => tool.function.name) || [];
    const index = this.counts.get(route) || 0; this.counts.set(route, index + 1);
    this.requests.push({ route, index, tools, messages: body.messages });
    let content = "", calls: Fact[] = [];
    if (!tools.length || route === "auxiliary") content = "Codex control audit";
    else if ((route === "input" || route === "cancel") && index === 0) {
      if (!tools.includes("request_user_input")) {
        response.writeHead(400, { "content-type": "application/json" }); response.end(JSON.stringify({ error: { message: "Canonical input missing from actual provider schema" } })); return;
      }
      calls = [{ id: `control-${route}`, type: "function", function: { name: "request_user_input", arguments: JSON.stringify(questionArgs) } }];
    } else if (route === "input") content = "HARNESS_INPUT_COMPLETE";
    else if (route === "cancel") content = "HARNESS_CANCEL_MUST_NOT_CONTINUE";
    else if (route === "plan") content = "<proposed_plan>\nHARNESS_PLAN_BODY\n- Read a real file.\n- Retain actual results.\n</proposed_plan>";
    else if (route === "implement") content = "HARNESS_PLAN_IMPLEMENTED\n\n**Semantic reply** with [documentation](https://example.test/docs).\n\n```ts\nconst value = 1;\nconsole.log(value);\n```\n\n| Name | Value |\n| --- | --- |\n| One | Two |";
    else if (route === "context" && index === 0) calls = [{ id: "context-inspect", type: "function", function: { name: "tool_exec",
      arguments: JSON.stringify({ code: 'text(await tools.get_context_remaining({})); text(await tools.read_file({file_path:"context-fact.txt"}));', yield_time_ms: 10000 }) } }];
    else if (route === "context") calls = [{ id: "context-window-reset", type: "function", function: { name: "new_context", arguments: "{}" } }];
    else if (route === "context-followup") content = "HARNESS_CONTEXT_WINDOW_COMPLETE";
    else if (route === "side") content = "HARNESS_SIDE_CHAT_COMPLETE";
    response.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
    const delta = calls.length ? { tool_calls: calls.map((call, index) => ({ index, ...call })) } : { content };
    response.write(`data: ${JSON.stringify({ id: `response-${route}-${index}`, object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason: null }] })}\n\n`);
    response.write(`data: ${JSON.stringify({ id: `response-${route}-${index}`, object: "chat.completion.chunk", choices: [{ index: 0, delta: {}, finish_reason: calls.length ? "tool_calls" : "stop" }] })}\n\n`);
    response.end("data: [DONE]\n\n");
  });
  async start() { await new Promise<void>(resolve => this.server.listen(0, "127.0.0.1", resolve)); return `http://127.0.0.1:${(this.server.address() as { port: number }).port}`; }
  async close() { await new Promise<void>((resolve, reject) => this.server.close(error => error ? reject(error) : resolve())); }
}
async function port() { const server = portServer(); await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve)); const value = (server.address() as { port: number }).port; await new Promise<void>(resolve => server.close(() => resolve())); return value; }
async function capture(app: ElectronApplication, page: Page, filename: string) {
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await page.evaluate(async () => {
    const transitions = document.getAnimations().filter(animation => animation.playState === "running"
      && animation.effect?.getComputedTiming().iterations !== Infinity);
    await Promise.allSettled(transitions.map(animation => animation.finished));
  });
  const styles = await page.locator('.inline-request-input').evaluateAll(elements => elements.map(element => {
    const style = getComputedStyle(element), matching: string[] = [];
    const visit = (rules: CSSRuleList) => { for (const rule of rules) {
      if (rule instanceof CSSStyleRule && element.matches(rule.selectorText) && (rule.style.borderRadius || rule.style.outline)) matching.push(rule.cssText);
      if ('cssRules' in rule) visit((rule as CSSGroupingRule).cssRules);
    } };
    for (const sheet of document.styleSheets) visit(sheet.cssRules);
    return { borderRadius: style.borderRadius, border: style.border, outline: style.outline,
      background: style.backgroundColor, fontFamily: style.fontFamily, fontSize: style.fontSize, matching };
  }));
  await writeFile(path.join(evidence, filename.replace(/\.png$/, '-style.json')), JSON.stringify(styles, null, 2));
  const png = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].capturePage()).toPNG().toString("base64"));
  await writeFile(path.join(evidence, filename), Buffer.from(png, "base64"));
}

test("Source controls, context branches and compaction through the production desktop", async () => {
  test.setTimeout(180_000);
  const profile = await mkdtemp(path.join(tmpdir(), "minicode-control-qa-"));
  const workspaceBase = path.join(path.parse(process.env.SystemRoot!).root, "Temp");
  await mkdir(workspaceBase, { recursive: true });
  const workspace = await mkdtemp(path.join(workspaceBase, "minicode-control-work-"));
  await mkdir(path.join(profile, "data"), { recursive: true }); await mkdir(evidence, { recursive: true });
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", questionNotifications: false, turnCompletionNotifications: "never" }));
  await writeFile(path.join(profile, "data/active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(profile, "data/trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(workspace, "context-fact.txt"), "WINDOW_FILE_FACT\n");
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: true }, memory: { enabled: false },
    feature_flags: { token_budget: true }, model_context_window: 100000 }));
  const provider = new ControlledProvider(); const providerUrl = await provider.start(); const wire: Fact[] = [], pageErrors: string[] = [], checks: Fact[] = [], desktopOutput: string[] = [], rendererWarnings: string[] = [];
  const { ELECTRON_RUN_AS_NODE: _node, ...environment } = process.env;
  let app: ElectronApplication | undefined;
  try {
    app = await _electron.launch({ executablePath: createRequire(path.join(root, "desktop/package.json"))("electron"),
      args: [path.join(root, "output/playwright/codex-control-audit-20261013/offscreen-desktop-entry.cjs")], cwd: path.join(root, "desktop"),
      env: { ...environment, MINICODE_USER_DATA_DIR: profile, MINICODE_BACKEND_PORT: String(await port()), MINICODE_FRONTEND_URL: "",
        MINICODE_DISABLE_HARDWARE_ACCELERATION: "1", MINICODE_SYSTEM_CONFIG_FILE: path.join(profile, "absent-system.toml"),
        MINICODE_REQUIREMENTS_FILE: path.join(profile, "absent-requirements.toml"), MINICODE_MANAGED_SETTINGS_DIR: path.join(profile, "managed"),
        LLM_PROVIDER: "custom", CUSTOM_API_KEY: "controlled-loopback-key", CUSTOM_BASE_URL: providerUrl, CUSTOM_MODEL: model, CUSTOM_WIRE_API: "chat",
        CUSTOM_REASONING_EFFORT: "", CUSTOM_SMALL_FAST_MODEL: "", OPENAI_API_KEY: "", OPENAI_BASE_URL: "", OPENAI_MODEL: "",
        ANTHROPIC_API_KEY: "", ANTHROPIC_BASE_URL: "", ANTHROPIC_MODEL: "" } });
    app.process().stdout?.on("data", chunk => desktopOutput.push(String(chunk)));
    app.process().stderr?.on("data", chunk => desktopOutput.push(String(chunk)));
    const page = await app.firstWindow(); page.on("pageerror", error => pageErrors.push(error.message));
    page.on("console", message => { if (["warning", "error"].includes(message.type())) rendererWarnings.push(message.text()); });
    const cdp = await page.context().newCDPSession(page); await cdp.send("Network.enable");
    cdp.on("Network.webSocketFrameReceived", ({ response }) => { if (response.opcode === 1) wire.push(JSON.parse(response.payloadData)); });
    cdp.on("Network.webSocketFrameSent", ({ response }) => { if (response.opcode === 1) wire.push({ outbound: true, ...JSON.parse(response.payloadData) }); });
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    expect(page.url()).toContain("/dist/index.html");
    const fresh = async () => {
      const previous = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
      await page.evaluate(root => (window as any).__zustandStore.getState().createConversation({ bindWorkspace: true, workspaceRoot: root, appMode: "cowork" }), workspace);
      await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).not.toBe(previous);
      expect(await page.evaluate(() => (window as any).__zustandStore.getState().setAgentMode("plan"))).toBe(true);
    };
    const send = async (message: string) => {
      const previousRequests = provider.requests.length;
      const composer = page.locator(".composer-input-region textarea").first(); await composer.fill(message); await composer.press("Enter");
      if (!message.startsWith("/")) await expect.poll(() => provider.requests.slice(previousRequests).some(request => request.route !== "auxiliary"), { timeout: 30_000 }).toBe(true);
    };
    if (process.env.MINICODE_CONTROL_E2E_START_AT !== "context") {
    await fresh(); await send("HARNESS_CANONICAL_INPUT: Ask the exact structured questions.");
    await expect(page.getByText(questionArgs.questions[0].question, { exact: true }).first()).toBeVisible();
    await expect(page.locator(".composer-input-region")).toBeHidden();
    expect(await page.locator('.inline-request-input').evaluate(element => ({ radius: getComputedStyle(element).borderRadius, outline: getComputedStyle(element).outlineStyle })))
      .toEqual({ radius: "24px", outline: "none" });
    await page.evaluate(() => (window as any).__zustandStore.getState().setDraft("UNSENT_CONTROL_DRAFT"));
    await capture(app, page, "01-canonical-question-one.png");
    await page.getByRole("radio", { name: /Parser/ }).click();
    await expect(page.getByText(questionArgs.questions[1].question, { exact: true }).first()).toBeVisible();
    await page.getByRole("textbox", { name: "回答当前问题" }).fill("Literal custom evidence 原文");
    await capture(app, page, "02-canonical-question-two-custom.png");
    await page.getByRole("textbox", { name: "回答当前问题" }).press("Enter");
    await expect(page.getByText("HARNESS_INPUT_COMPLETE", { exact: true })).toBeVisible();
    expect(await page.evaluate(() => (window as any).__zustandStore.getState().draft)).toBe("UNSENT_CONTROL_DRAFT");
    const inputReceipt = provider.requests.find(request => request.route === "input" && request.index === 1)!;
    const result = inputReceipt.messages.findLast((item: Fact) => item.role === "tool" && item.name === "request_user_input");
    expect(JSON.parse(text(result))).toEqual({ answers: { area: { answers: ["Parser"] }, detail: { answers: ["Literal custom evidence 原文"] } } });
    const controlResponse = wire.find(event => event.type === "command.result" && event.command === "control_response");
    expect(controlResponse).toBeTruthy();
    checks.push({ scenario: "canonical questions actual provider schema, structured answers, WS admission and draft restore", passed: true });
    await capture(app, page, "03-canonical-completed-draft-restored.png");

    await fresh(); await send("HARNESS_CANCEL_INPUT: Ask then await cancellation.");
    await expect(page.getByText(questionArgs.questions[0].question, { exact: true }).first()).toBeVisible();
    await page.locator(".inline-request-input").press("Escape");
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().isStreaming)).toBe(false);
    expect(provider.requests.filter(request => request.route === "cancel")).toHaveLength(1);
    await expect(page.getByText("HARNESS_CANCEL_MUST_NOT_CONTINUE", { exact: true })).toHaveCount(0);
    checks.push({ scenario: "Escape cancels the actual turn without continuing the model", passed: true });
    await capture(app, page, "04-canonical-cancelled-turn.png");

    await fresh(); await send("HARNESS_PLAN: Return the complete proposed plan.");
    await expect(page.getByText("执行这个计划吗？", { exact: true })).toBeVisible();
    await expect(page.locator(".composer-input-region")).toBeHidden();
    await page.evaluate(() => (window as any).__zustandStore.getState().setDraft("UNSENT_PLAN_DRAFT"));
    await capture(app, page, "05-plan-bottom-request.png");
    await page.getByRole("radio", { name: /是的，执行这个计划/ }).click();
    await expect(page.getByText("HARNESS_PLAN_IMPLEMENTED", { exact: true })).toBeVisible();
    const implementation = provider.requests.find(request => request.route === "implement")!;
    const implementationUser = implementation.messages.filter((item: Fact) => item.role === "user").map(text).at(-1);
    const implementationContent = "PLEASE IMPLEMENT THIS PLAN:\nHARNESS_PLAN_BODY\n- Read a real file.\n- Retain actual results.";
    const implementationMessages = wire.filter(event => event.outbound && event.type === "user_message" && event.content?.startsWith("PLEASE IMPLEMENT THIS PLAN:"));
    expect(implementationMessages).toHaveLength(1);
    expect(implementationMessages[0].content).toBe(implementationContent);
    expect(implementationMessages[0].quoted_message).toBeNull();
    // The harness's separate environment reminder is retained in the provider
    // evidence; test the actual submitted plan content rather than its envelope.
    expect(implementationUser.endsWith(implementationContent)).toBe(true);
    expect(implementationUser.split("HARNESS_PLAN_BODY")).toHaveLength(2);
    expect(await page.evaluate(() => (window as any).__zustandStore.getState().draft)).toBe("UNSENT_PLAN_DRAFT");
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().agentMode)).toBe("build");
    checks.push({ scenario: "Plan bottom surface submits the source prefix and plan once with actual default-mode admission", passed: true });
    await capture(app, page, "06-plan-implemented-draft-restored.png");

    const parent = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
    const answer = page.locator('.assistant-cell-wrap').filter({ has: page.getByText("HARNESS_PLAN_IMPLEMENTED", { exact: true }) });
    await expect(answer.locator('.assistant-cell-action-buttons button')).toHaveCount(2);
    await expect(answer.getByRole("button", { name: "引用回复" })).toHaveCount(0);
    await expect(answer.getByRole("button", { name: "重新生成" })).toHaveCount(0);
    // Intercept the OS clipboard destination; preserve the user's clipboard.
    // Real production Markdown DOM, serializer, MIME Blobs and button await
    // remain in the chain, while no hidden QA window requires OS focus.
    await page.evaluate(() => {
      (window as any).__qaClipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
      (window as any).__qaClipboard = [];
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
        write: async (items: ClipboardItem[]) => {
          for (const item of items) {
            const contents: Record<string, string> = {};
            for (const type of item.types) contents[type] = await (await item.getType(type)).text();
            (window as any).__qaClipboard.push(contents);
          }
        },
        writeText: async (value: string) => (window as any).__qaClipboard.push({ "text/plain": value }),
      } });
    });
    await answer.locator(".assistant-cell-actions").hover();
    await answer.getByRole("button", { name: "复制回复" }).click();
    await expect(answer.getByRole("button", { name: "已复制" })).toBeVisible();
    const clipboard = await page.evaluate(() => (window as any).__qaClipboard);
    expect(clipboard).toHaveLength(1);
    expect(clipboard[0]["text/plain"]).toContain("Semantic reply with documentation.");
    expect(clipboard[0]["text/plain"]).toContain("const value = 1;\nconsole.log(value);");
    expect(clipboard[0]["text/plain"]).toContain("Name\tValue\nOne\tTwo");
    expect(clipboard[0]["text/html"]).toContain("<strong>Semantic reply</strong>");
    expect(clipboard[0]["text/html"]).toContain('<pre dir="ltr"><code>const value = 1;\nconsole.log(value);</code></pre>');
    expect(clipboard[0]["text/html"]).not.toMatch(/class=|style=|<button|line-number/);
    await page.getByRole("button", { name: "聊天操作", exact: true }).click();
    await page.getByRole("menuitem", { name: "复制", exact: true }).hover();
    await capture(app, page, "12-source-copy-submenu.png");
    await page.getByRole("menuitem", { name: "复制深度链接", exact: true }).click();
    await expect.poll(() => page.evaluate(() => (window as any).__qaClipboard.at(-1)?.["text/plain"]))
      .toBe(`minicode://conversation?conversation_id=${encodeURIComponent(parent)}`);
    await page.getByRole("button", { name: "聊天操作", exact: true }).click();
    await page.getByRole("menuitem", { name: "复制", exact: true }).hover();
    await page.getByRole("menuitem", { name: "复制为 Markdown", exact: true }).click();
    await expect.poll(() => page.evaluate(() => (window as any).__qaClipboard.at(-1)?.["text/plain"] || "")).toContain("HARNESS_PLAN_IMPLEMENTED");
    const copiedThread = await page.evaluate(() => (window as any).__qaClipboard.at(-1)["text/plain"]);
    expect(copiedThread).toContain("HARNESS_PLAN_BODY");
    expect(copiedThread.startsWith("# ")).toBe(true);
    expect(copiedThread).toMatch(/^> HARNESS_PLAN:/m);
    checks.push({ scenario: "actual Source Copy submenu, valid MiniCode deep-link producer and complete parent Markdown through history read and clipboard destination", passed: true });
    const allClipboard = await page.evaluate(() => (window as any).__qaClipboard);
    await writeFile(path.join(evidence, "reply-clipboard-destination.json"), JSON.stringify(allClipboard, null, 2));
    await page.evaluate(() => {
      const descriptor = (window as any).__qaClipboardDescriptor;
      if (descriptor) Object.defineProperty(navigator, "clipboard", descriptor);
      else delete (navigator as any).clipboard;
    });
    checks.push({ scenario: "real rich reply Copy serializes visible prose, code and table into plain/HTML MIME Blobs and success Check; OS destination intercepted", passed: true });
    // The destination alone is intercepted; actual menu, preload, native
    // installed discovery, IPC and preference persistence stay in the chain.
    await app.evaluate(({ shell }) => {
      (globalThis as any).__offscreenQa.externalCalls = [];
      shell.openPath = async (file: string) => { (globalThis as any).__offscreenQa.externalCalls.push({ action: "openPath", file }); return ""; };
      shell.showItemInFolder = (file: string) => { (globalThis as any).__offscreenQa.externalCalls.push({ action: "showItemInFolder", file }); };
    });
    await page.getByRole("button", { name: "聊天操作", exact: true }).click();
    const openIn = page.getByRole("menuitem", { name: "打开方式", exact: true });
    await openIn.hover();
    await expect(page.getByRole("menuitem", { name: "File Explorer", exact: true })).toBeVisible();
    await expect(page.getByRole("menuitem", { name: "VS Code", exact: true })).toHaveCount(0);
    await expect(page.getByRole("menuitem", { name: "Cursor", exact: true })).toHaveCount(0);
    await capture(app, page, "07-source-open-in-workspace-menu.png");
    await page.getByRole("menuitem", { name: "File Explorer", exact: true }).click();
    await expect.poll(() => app!.evaluate(() => (globalThis as any).__offscreenQa.externalCalls.map((call: Fact) => ({ ...call, file: call.file.toLowerCase() }))))
      .toEqual([{ action: "openPath", file: workspace.toLowerCase() }]);
    const preferences = await page.evaluate(() => (window as any).__MINICODE_RUNTIME__.desktop.preferences.get());
    expect(preferences.fileOpenTarget).toBe("fileManager");
    expect(Object.entries(preferences.fileOpenTargetPerPath).find(([key]) => key.toLowerCase() === workspace.toLowerCase())?.[1]).toBe("fileManager");
    checks.push({ scenario: "real installed Open in submenu, Work filtering and workspace global/perPath IPC persistence; external OS launch intercepted", passed: true });

    await answer.locator(".assistant-cell-actions").hover();
    await answer.getByRole("button", { name: "从此处分支" }).click();
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).not.toBe(parent);
    await expect.poll(() => wire.some(event => event.type === "command.result" && event.command === "context.fork" && event.level === "success"
      && event.conversation_id === parent && event.data?.parent_conversation_id === parent)).toBe(true);
    expect(await page.evaluate(owner => (window as any).__zustandStore.getState().conversationWorkbenchStates[owner]?.draft, parent)).toBe("UNSENT_PLAN_DRAFT");
    await expect(page.getByText("HARNESS_PLAN_IMPLEMENTED", { exact: true })).toBeVisible();
    await expect(page.locator(".message-list-content")).not.toContainText(/已创建并切换到上下文分支|估算.*tokens/);
    checks.push({ scenario: "actual context fork receipt, activated child history and hidden parent draft retained", passed: true });
    await capture(app, page, "08-source-fork-completed-parent-draft-retained.png");

    // A side conversation really forks context. Its new input owns its answer;
    // inherited parent requests remain reference history in the provider.
    const mainOwner = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
    await page.getByRole("button", { name: "聊天操作", exact: true }).click();
    await page.getByRole("menuitem", { name: "打开侧边聊天", exact: true }).click();
    const sideOwner = await page.evaluate(() => (window as any).__zustandStore.getState().activeSideChatId);
    await expect.poll(() => wire.some(frame => frame.type === "command.result" && frame.command === "conversation.create"
      && frame.level === "success" && frame.data?.conversation_id === sideOwner && frame.data?.source_conversation_id === mainOwner)).toBe(true);
    const sideInput = page.locator(".side-chat-panel textarea").first();
    await expect(sideInput).toBeEnabled(); await sideInput.fill("HARNESS_SIDE_CHAT: inspect the inherited context without continuing its old task.");
    await sideInput.press("Enter");
    await expect.poll(() => provider.requests.some(request => request.route === "side"), { timeout: 30_000 }).toBe(true);
    await expect(page.locator(".side-chat-panel").getByText("HARNESS_SIDE_CHAT_COMPLETE", { exact: true })).toBeVisible();
    expect(await page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).toBe(mainOwner);
    const sideRequest = provider.requests.find(request => request.route === "side")!;
    expect(sideRequest.messages.some((message: Fact) => text(message).includes("HARNESS_PLAN_IMPLEMENTED"))).toBe(true);
    // This test uses the existing Chat adapter, whose request serializer joins
    // the leading developer blocks into one system instruction prefix.
    // The core pre-adapter developer role is separately verified by the WS/loop tests.
    expect(sideRequest.messages.filter((message: Fact) => ["developer", "system"].includes(message.role) && text(message).includes("You are in a side conversation, not the main thread."))).toHaveLength(1);
    checks.push({ scenario: "actual new side chat source fork, full inherited provider context, Source developer boundary and side-only reply; foreground owner retained", passed: true });
    await capture(app, page, "09-source-side-chat-context-boundary.png");
    }

    await fresh();
    const windowOwner = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
    expect(await page.evaluate(() => (window as any).__zustandStore.getState().setAgentMode("build"))).toBe(true);
    await send("HARNESS_CONTEXT_RESET: read the real file and context budget, then start a new context window.");
    await expect.poll(() => wire.some(frame => frame.type === "item.completed" && frame.conversation_id === windowOwner
      && frame.item?.type === "agent_message" && frame.item?.text === "HARNESS_CONTEXT_WINDOW_COMPLETE"), { timeout: 30_000 }).toBe(true);
    await expect(page.getByText("HARNESS_CONTEXT_WINDOW_COMPLETE", { exact: true })).toBeVisible();
    const resetItem = wire.find(frame => frame.conversation_id === windowOwner && frame.type === "item.completed" && frame.item?.type === "context_compaction");
    expect(resetItem).toBeTruthy(); expect(resetItem.item.source).toBe("auto");
    expect(wire.some(frame => frame.type === "tool_call" && frame.conversation_id === windowOwner && frame.name === "read_file")).toBe(true);
    const remainingCall = wire.find(frame => frame.type === "tool_call" && frame.conversation_id === windowOwner && frame.name === "get_context_remaining");
    expect(remainingCall).toBeTruthy();
    expect(wire.some(frame => frame.type === "tool_result" && frame.conversation_id === windowOwner && frame.id === remainingCall.id
      && frame.visibility === "debug" && frame.summary?.includes("tokens left"))).toBe(true);
    const resumedPrompt = provider.requests.find(request => request.route === "context-followup")!;
    expect(resumedPrompt.messages.filter((message: Fact) => message.role === "user").some((message: Fact) => text(message).includes("HARNESS_CONTEXT_RESET"))).toBe(false);
    expect(wire.some(frame => frame.type === "command.error" && frame.conversation_id === windowOwner)).toBe(false);
    const windowTurn = page.locator(".chat-turn").filter({ has: page.getByText("HARNESS_CONTEXT_WINDOW_COMPLETE", { exact: true }) });
    await windowTurn.locator(".agent-loop-process-summary").first().click();
    await expect(windowTurn.getByText("已优化对话", { exact: true })).toBeVisible();
    expect(await windowTurn.locator(".chat-turn-process-stack").innerText()).not.toMatch(/new_context|get_context_remaining|You have .*tokens left/);
    checks.push({ scenario: "feature-gated context tools through real VM/file/loop/WS; no-summary archived rollover, Source-visible lifecycle and internal tool receipts retained", passed: true });
    await capture(app, page, "10-source-context-window-completed.png");

    await send("/compact");
    await expect.poll(() => wire.some(frame => frame.type === "item.completed" && frame.conversation_id === windowOwner
      && frame.item?.type === "context_compaction" && frame.item?.source === "manual")).toBe(true);
    await expect(page.getByText("上下文已压缩", { exact: true }).last()).toBeVisible();
    expect(wire.filter(frame => frame.type === "item.started" && frame.item?.type === "context_compaction" && frame.item?.source === "manual")
      .every(frame => !frame.message_id)).toBe(true);
    checks.push({ scenario: "manual compact with empty Source window summary, real independent operation item and persisted semantic completion without fake assistant", passed: true });
    await capture(app, page, "11-source-manual-context-window-completed.png");

    await page.evaluate(() => (window as any).__zustandStore.getState().setDraft("UNSENT_ARCHIVE_DRAFT"));
    await page.getByRole("button", { name: "聊天操作", exact: true }).click();
    await page.getByRole("menuitem", { name: "归档", exact: true }).click();
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).toBeNull();
    await expect.poll(() => wire.some(frame => frame.type === "conversation.switched" && frame.archive_navigation === "home"
      && frame.conversation_id === null && frame.conversation === null && frame.session?.active_conversation_id === null)).toBe(true);
    expect(await page.evaluate(() => (window as any).__zustandStore.getState().appMode)).toBe("cowork");
    expect(await page.evaluate(owner => (window as any).__zustandStore.getState().conversationWorkbenchStates[owner]?.draft, windowOwner)).toBe("UNSENT_ARCHIVE_DRAFT");
    checks.push({ scenario: "real Header Archive canonical Home switch, frontend/backend empty owner and mode agree, archived draft retained", passed: true });
    await capture(app, page, "13-source-archive-home-draft-retained.png");
    expect(pageErrors).toEqual([]);
    const native = await app.evaluate(({ BrowserWindow }) => ({ visible: BrowserWindow.getAllWindows().map(window => window.isVisible()), qa: (globalThis as any).__offscreenQa }));
    expect(native.visible).toEqual([false]);
    await writeFile(path.join(evidence, "verdict.json"), JSON.stringify({ passed: true, controlledModelOnly: true, profile, workspace, checks, pageErrors, native }, null, 2));
  } finally {
    await writeFile(path.join(evidence, "provider-requests.json"), JSON.stringify(provider.requests, null, 2));
    await writeFile(path.join(evidence, "wire.json"), JSON.stringify(wire, null, 2));
    await writeFile(path.join(evidence, "partial-checks.json"), JSON.stringify({ checks, pageErrors }, null, 2));
    await writeFile(path.join(evidence, "desktop-output.log"), desktopOutput.join(""));
    await writeFile(path.join(evidence, "renderer-warnings.json"), JSON.stringify(rendererWarnings, null, 2));
    if (app) await app.close(); await provider.close();
  }
});
