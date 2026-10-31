/** The model and image API are loopback fixtures; desktop, tools, stores, WS and UI are production code. */
import { _electron as electron, expect, test, type Page } from "@playwright/test";
import { createServer } from "node:http";
import { createServer as portServer } from "node:net";
import { createRequire } from "node:module";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

type Fact = Record<string, any>;
const root = path.resolve(import.meta.dirname, "../../..");
const evidence = path.resolve(process.env.MINICODE_IMAGE_E2E_EVIDENCE_ROOT || path.join(root, "output/playwright/multi-agent-boundary-20261031/image"));
const fixtureModel = "image-boundary-loopback";
const text = (message: Fact) => typeof message.content === "string" ? message.content
  : (message.content || []).map((part: Fact) => part.text || "").join("\n");

class ImageProvider {
  png = "";
  baseUrl = "";
  readonly requests: Fact[] = [];
  readonly imageRequests: Fact[] = [];
  readonly counts = new Map<string, number>();
  readonly issued = new Map<string, number>();
  releaseImage: (() => void) | undefined;
  readonly server = createServer(async (request, response) => {
    if (request.method === "GET") {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ data: [{ id: fixtureModel, object: "model" }] })); return;
    }
    const chunks: Buffer[] = []; for await (const chunk of request) chunks.push(Buffer.from(chunk));
    const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    if (request.url?.startsWith("/v1/images/generations")) {
      this.imageRequests.push({ at: Date.now(), path: request.url, authorization: request.headers.authorization || "", body });
      if (["IMAGE_E2E_SUCCESS", "IMAGE_E2E_MULTIPLE_SECOND"].includes(body.prompt)) await new Promise<void>(resolve => { this.releaseImage = resolve; });
      response.writeHead(body.prompt === "IMAGE_E2E_FAILURE" ? 401 : body.prompt === "IMAGE_E2E_UNSUPPORTED" ? 404 : 200, { "content-type": "application/json" });
      response.end(JSON.stringify(body.prompt === "IMAGE_E2E_UNSUPPORTED"
        ? { error: { message: "This text endpoint does not support image generation", code: "unsupported_image_api" } }
        : body.prompt === "IMAGE_E2E_FAILURE"
        ? { error: { message: "IMAGE_API_EXPECTED_FAILURE", type: "fixture_rejection" } }
        : { data: [{ b64_json: body.prompt === "IMAGE_E2E_MALFORMED" ? Buffer.from("not an image").toString("base64") : this.png }] })); return;
    }
    const source = body.messages.filter((message: Fact) => message.role === "user").map(text)
      .findLast((value: string) => /IMAGE_E2E_(SUCCESS|FAILURE|UNSUPPORTED|MALFORMED|UNSELECTED|MULTIPLE|MIXED_(?:SUCCESS|FAILURE)_FIRST)/.test(value)) || "";
    const route = source.match(/IMAGE_E2E_(SUCCESS|FAILURE|UNSUPPORTED|MALFORMED|UNSELECTED|MULTIPLE|MIXED_(?:SUCCESS|FAILURE)_FIRST)/)?.[0] || "auxiliary";
    const index = this.counts.get(route) || 0; this.counts.set(route, index + 1);
    const tools = body.tools?.map((tool: Fact) => tool.function.name) || [];
    const trace = { at: Date.now(), route, index, tools, selectedTool: "" }; this.requests.push(trace);
    let content = route === "auxiliary" ? "Image boundary audit" : `${route}_DONE`;
    let calls: Fact[] = [];
    const call = (name: string, args: Fact) => {
      expect(tools).toContain(name);
      calls = [{ id: `image-${route}-${index}`, type: "function", function: { name, arguments: JSON.stringify(args) } }];
    };
    const resultText = text(body.messages.findLast((message: Fact) => message.role === "tool") || {});
    const runningCell = resultText.match(/Script running with cell ID (cell_[a-z0-9]+)/i)?.[1];
    const report = resultText.includes('"cell_id"') ? JSON.parse(resultText.slice(resultText.indexOf("{"), resultText.lastIndexOf("}") + 1))
      : runningCell ? { status: "running", cell_id: runningCell } : undefined;
    if (route !== "auxiliary" && tools.length) {
      if (report?.status === "running") call("tool_wait", { cell_id: report.cell_id, yield_time_ms: 10000, max_tokens: 1000 });
      else if ((this.issued.get(route) || 0) === 0 || ((route === "IMAGE_E2E_MULTIPLE" || route.startsWith("IMAGE_E2E_MIXED")) && this.issued.get(route) === 1)) {
        const args = (prompt: string) => JSON.stringify({ prompt, api_base_url: this.baseUrl + "/v1", model: "fixture-image", ...(route === "IMAGE_E2E_SUCCESS" ? { output_path: "outputs/generated.png" } : {}) });
        const issued = this.issued.get(route) || 0;
        const code = route.startsWith("IMAGE_E2E_MIXED")
          ? `const result = await tools.generate_image(${args((route === "IMAGE_E2E_MIXED_FAILURE_FIRST") === (issued === 0) ? "IMAGE_E2E_FAILURE" : "IMAGE_E2E_MIXED_VALID")}); if (result.is_error) text(result.content); else generatedImage(result.images[0]);`
          : route === "IMAGE_E2E_MULTIPLE"
          ? `const result = await tools.generate_image(${args(issued === 0 ? "IMAGE_E2E_MULTIPLE_FIRST" : "IMAGE_E2E_MULTIPLE_SECOND")}); generatedImage(result.images[0]);`
          : `const result = await tools.generate_image(${args(route)}); if (result.is_error) text(result.content); else ${route === "IMAGE_E2E_UNSELECTED" ? 'text("generated for inspection only")' : "generatedImage(result.images[0])"};`;
        this.issued.set(route, issued + 1);
        call("tool_exec", { yield_time_ms: 10000, code });
      }
    }
    trace.selectedTool = calls[0]?.function.name || "";
    response.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
    const delta = calls.length ? { tool_calls: calls.map((item, index) => ({ index, ...item })) } : { content };
    response.write(`data: ${JSON.stringify({ id: `response-${route}-${index}`, object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason: null }] })}\n\n`);
    response.write(`data: ${JSON.stringify({ id: `response-${route}-${index}`, object: "chat.completion.chunk", choices: [{ index: 0, delta: {}, finish_reason: calls.length ? "tool_calls" : "stop" }] })}\n\n`);
    response.end("data: [DONE]\n\n");
  });
  async start() { await new Promise<void>(resolve => this.server.listen(0, "127.0.0.1", resolve)); this.baseUrl = `http://127.0.0.1:${(this.server.address() as { port: number }).port}`; return this.baseUrl + "/v1"; }
  async close() { this.releaseImage?.(); this.server.closeAllConnections(); await new Promise<void>(resolve => this.server.close(() => resolve())); }
}

async function freePort() { const server = portServer(); await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve)); const port = (server.address() as { port: number }).port; await new Promise<void>(resolve => server.close(() => resolve())); return port; }
async function ready(page: Page) { await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true); }

test("image API ownership, progress, hidden drafts, persisted projection and real errors", async () => {
  test.setTimeout(240_000);
  await mkdir(evidence, { recursive: true });
  const profile = await mkdtemp(path.join(tmpdir(), "minicode-image-boundary-"));
  const workspace = path.join(profile, "workspace"), config = path.join(profile, "config");
  await Promise.all([mkdir(path.join(profile, "data"), { recursive: true }), mkdir(config), mkdir(workspace)]);
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: true }, memory: { enabled: false }, features: { memories: false } }));
  await writeFile(path.join(profile, "data/trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", confirmBeforeClose: "never", turnCompletionNotifications: "never", questionNotifications: false, permissionNotifications: false, defaultPermissionMode: "bypass" }));
  const attachment = path.join(workspace, "keep-draft.txt"); await writeFile(attachment, "Preserve this actual attachment.\n");
  const shim = path.join(profile, "background-entry.cjs");
  await writeFile(shim, `const {app,BrowserWindow}=require('electron'); app.commandLine.appendSwitch('disable-renderer-backgrounding'); BrowserWindow.prototype.show=function(){this.showInactive()}; BrowserWindow.prototype.focus=function(){}; require(${JSON.stringify(path.join(root, "desktop/main.js"))});`);
  const provider = new ImageProvider(); const providerUrl = await provider.start();
  const { ELECTRON_RUN_AS_NODE: _node, ...environment } = process.env;
  const app = await electron.launch({ executablePath: createRequire(path.join(root, "desktop/package.json"))("electron"), args: [shim], cwd: path.join(root, "desktop"), env: {
    ...environment, MINICODE_USER_DATA_DIR: profile, MINICODE_STATE_ROOT: profile, MINICODE_CONFIG_DIR: config,
    MINICODE_FRONTEND_URL: "", MINICODE_BACKEND_PORT: String(await freePort()), MINICODE_DISABLE_HARDWARE_ACCELERATION: "1",
    MINICODE_SYSTEM_CONFIG_FILE: path.join(profile, "no-system.toml"), MINICODE_REQUIREMENTS_FILE: path.join(profile, "no-requirements.toml"), MINICODE_MANAGED_SETTINGS_DIR: path.join(profile, "managed"),
    LLM_PROVIDER: "custom", CUSTOM_API_KEY: "loopback-model-key", CUSTOM_BASE_URL: providerUrl, CUSTOM_MODEL: fixtureModel, CUSTOM_WIRE_API: "chat", CUSTOM_REASONING_EFFORT: "", CUSTOM_SMALL_FAST_MODEL: "",
    OPENAI_API_KEY: "", ANTHROPIC_API_KEY: "", OPENAI_BASE_URL: "", OPENAI_MODEL: "", ANTHROPIC_BASE_URL: "", ANTHROPIC_MODEL: "", PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8",
  } });
  const wire: Fact[] = [], errors: string[] = [], desktopLogs: string[] = [], timings: Fact[] = [];
  app.process().stdout?.on("data", chunk => desktopLogs.push(String(chunk)));
  app.process().stderr?.on("data", chunk => desktopLogs.push(String(chunk)));
  const page = await app.firstWindow(); page.on("pageerror", error => errors.push(error.message));
  page.on("dialog", () => {});
  page.on("console", message => { if (["error", "warning"].includes(message.type())) desktopLogs.push(`[renderer ${message.type()}] ${message.text()}\n`); });
  const cdp = await page.context().newCDPSession(page); await cdp.send("Network.enable");
  cdp.on("Network.webSocketFrameReceived", ({ response }) => { if (response.opcode === 1) wire.push({ at: Date.now(), ...JSON.parse(response.payloadData) }); });
  const capture = async (name: string) => { await page.screenshot({ path: path.join(evidence, name + ".png"), animations: "disabled" }); await writeFile(path.join(evidence, name + ".txt"), await page.locator("body").innerText()); };
  const createConversation = async () => {
    const previous = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
    await page.evaluate(workspace => (window as any).__zustandStore.getState().createConversation({ bindWorkspace: true, workspaceRoot: workspace, appMode: "cowork" }), workspace);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).not.toBe(previous);
    await page.evaluate(() => (window as any).__zustandStore.getState().setPermissionMode("bypass"));
    return await page.evaluate(() => (window as any).__zustandStore.getState().conversationId as string);
  };
  const select = async (id: string) => { await page.evaluate(id => (window as any).__zustandStore.getState().requestConversationSwitch(id), id); await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().pendingConversationSwitchId)).toBeNull(); expect(await page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).toBe(id); };
  const send = async (route: string) => { const input = page.locator('.composer-input-region [role="textbox"]').first(); await input.fill(`${route}: Use this external image API ${providerUrl} with image model fixture-image. Generate the image using the imagegen skill.`); const start = Date.now(); await input.press("Enter"); await expect(input).toHaveText(""); timings.push({ name: route + "-composer-ack", elapsedMs: Date.now() - start }); };
  const showOriginalImageError = async (owner: string, name: string) => {
    await expect(page.locator(".assistant-cell-image-placeholder")).toHaveCount(0);
    const call = wire.find(event => event.type === "tool_call" && event.conversation_id === owner && event.name === "generate_image"
      && wire.some(result => result.type === "tool_result" && result.id === event.id && result.status === "failed"));
    expect(call).toBeTruthy();
    const original = wire.find(event => event.type === "tool_result" && event.conversation_id === owner && event.id === call!.id)!;
    const detail = original.summary.replace(/^Image generation failed:\s*/, "");
    await capture(name + "-folded");
    if (await page.locator('.chat-turn-process[data-collapsed="true"]').count()) await page.getByRole("button", { name: "展开处理步骤", exact: true }).click();
    const row = page.locator('.chat-turn-process .activity-cell[data-status="failed"]').filter({ hasText: "Generate image" });
    await expect(row).toHaveCount(1); await row.getByRole("button", { name: "展开活动详情", exact: true }).click();
    await expect(row).toContainText(detail); await expect(page.locator(".assistant-cell-image-error")).toHaveCount(0);
    await capture(name + "-original-call-details");
  };
  let originalFailure: unknown;
  try {
    await ready(page); expect(page.url()).toContain("/dist/index.html");
    provider.png = await page.evaluate(() => { const canvas = document.createElement("canvas"); canvas.width = 480; canvas.height = 300; const ctx = canvas.getContext("2d")!; ctx.fillStyle = "#6d5efc"; ctx.fillRect(0, 0, 480, 300); ctx.fillStyle = "#2cccb9"; ctx.fillRect(150, 75, 180, 150); return canvas.toDataURL("image/png").split(",")[1]; });
    await capture("00-current-build-home");
    await page.evaluate(() => { const store = (window as any).__zustandStore.getState(); store.setSettingsTab("connectors"); store.toggleSettings(); });
    await expect(page.locator('[data-settings-tab="connectors"]')).toBeVisible(); await capture("00-current-build-connectors");
    await page.evaluate(() => (window as any).__zustandStore.getState().toggleSettings());
    const first = await createConversation(); await send("IMAGE_E2E_SUCCESS");
    await expect.poll(() => provider.imageRequests.length, { timeout: 45_000 }).toBe(1);
    await expect(page.locator(".assistant-cell-image-placeholder").first()).toBeVisible(); await capture("01-running-image");
    const composer = page.locator('.composer-input-region [role="textbox"]').first(); await composer.fill("UNSENT_IMAGE_OWNER_DRAFT");
    const chooser = page.waitForEvent("filechooser");
    await page.getByRole("button", { name: "添加文件等内容", exact: true }).click();
    await page.getByRole("menuitem", { name: "文件和文件夹", exact: true }).click();
    await (await chooser).setFiles(attachment);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().attachments[0]?.status)).toBe("ready");
    const second = await createConversation(); await page.locator('.composer-input-region [role="textbox"]').first().fill("UNSENT_OTHER_DRAFT");
    await expect.poll(() => provider.requests.some(request => request.route === "IMAGE_E2E_SUCCESS" && request.selectedTool === "tool_wait"), { timeout: 30_000 }).toBe(true);
    provider.releaseImage!();
    await expect.poll(() => wire.some(event => event.type === "done" && event.conversation_id === first), { timeout: 45_000 }).toBe(true);
    await expect(page.locator(".assistant-cell-image-card")).toHaveCount(0); await capture("02-other-conversation-draft");
    const switchAt = Date.now(); await select(first);
    const image = page.locator(".assistant-cell-generated-image"); await expect(image).toHaveCount(1);
    await expect.poll(() => image.evaluate((node: HTMLImageElement) => node.complete && node.naturalWidth > 0)).toBe(true);
    timings.push({ name: "select-to-loaded-image", elapsedMs: Date.now() - switchAt });
    await expect(page.locator('.composer-input-region [role="textbox"]').first()).toHaveText("UNSENT_IMAGE_OWNER_DRAFT");
    expect(await page.evaluate(() => (window as any).__zustandStore.getState().attachments[0]?.name)).toBe("keep-draft.txt");
    await capture("03-generated-image-owner-draft");
    const openAt = Date.now(); await page.getByRole("button", { name: "查看图片大图", exact: true }).click(); await expect(page.getByRole("dialog", { name: "图片大图", exact: true })).toBeVisible();
    timings.push({ name: "open-image-lightbox", elapsedMs: Date.now() - openAt }); await capture("04-generated-image-lightbox"); await page.getByRole("button", { name: "关闭大图", exact: true }).click();
    expect(Buffer.from(await readFile(path.join(workspace, "outputs/generated.png")))).toEqual(Buffer.from(provider.png, "base64"));
    expect(provider.imageRequests[0].authorization).toBe("");
    await page.reload(); await ready(page); await select(first);
    await expect(page.locator(".assistant-cell-generated-image")).toHaveCount(1);
    await expect.poll(() => page.locator(".assistant-cell-generated-image").evaluate((node: HTMLImageElement) => node.complete && node.naturalWidth > 0)).toBe(true);
    await capture("05-reconnected-single-image"); await select(second); await expect(page.locator('.composer-input-region [role="textbox"]').first()).toHaveText("UNSENT_OTHER_DRAFT");
    await createConversation(); await send("IMAGE_E2E_UNSELECTED");
    await expect(page.getByText("IMAGE_E2E_UNSELECTED_DONE", { exact: true })).toBeVisible({ timeout: 45_000 });
    await expect(page.locator(".assistant-cell-image-card")).toHaveCount(0);
    await expect(page.locator(".assistant-cell-image-placeholder")).toHaveCount(0);
    await capture("06-unselected-image-completed");
    await createConversation(); await send("IMAGE_E2E_MULTIPLE");
    await expect.poll(() => provider.imageRequests.some(request => request.body.prompt === "IMAGE_E2E_MULTIPLE_SECOND"), { timeout: 45_000 }).toBe(true);
    await expect(page.locator(".assistant-cell-generated-image")).toHaveCount(1);
    await expect(page.locator('.assistant-cell-image-placeholder[data-status="running"]')).toHaveCount(1);
    expect(await page.locator(".assistant-cell-generated").evaluate(node => {
      const image = node.querySelector(".assistant-cell-image-card")!;
      const pending = node.querySelector(".assistant-cell-image-placeholder")!;
      return Boolean(image.compareDocumentPosition(pending) & Node.DOCUMENT_POSITION_FOLLOWING);
    })).toBe(true);
    await capture("07-one-image-and-next-running");
    provider.releaseImage!();
    await expect(page.getByText("IMAGE_E2E_MULTIPLE_DONE", { exact: true })).toBeVisible({ timeout: 45_000 });
    await expect(page.locator(".assistant-cell-generated-image")).toHaveCount(2);
    await expect(page.locator(".assistant-cell-image-placeholder")).toHaveCount(0); await capture("08-two-selected-images");
    for (const route of ["IMAGE_E2E_MIXED_SUCCESS_FIRST", "IMAGE_E2E_MIXED_FAILURE_FIRST"]) {
      const conversation = await createConversation(); await send(route);
      await expect(page.getByText(`${route}_DONE`, { exact: true })).toBeVisible({ timeout: 45_000 });
      await expect(page.locator(".assistant-cell-generated-image")).toHaveCount(1);
      await expect(page.locator('.assistant-cell-image-placeholder[data-status="failed"]')).toHaveCount(0);
      await expect(page.locator('.assistant-cell-image-placeholder[data-running="true"]')).toHaveCount(0);
      await expect.poll(() => page.evaluate(() => !(window as any).__zustandStore.getState().isStreaming)).toBe(true);
      await showOriginalImageError(conversation, route === "IMAGE_E2E_MIXED_SUCCESS_FIRST" ? "09-success-then-failure" : "10-failure-then-success");
      const artifact = wire.find(event => event.type === "artifact.preview" && event.conversation_id === conversation)!;
      expect(wire.some(event => event.type === "agent.progress" && event.conversation_id === conversation && event.status === "completed" && event.tool_call_id === artifact.tool_call_id)).toBe(true);
    }
    for (const route of ["IMAGE_E2E_FAILURE", "IMAGE_E2E_UNSUPPORTED", "IMAGE_E2E_MALFORMED"]) {
      const owner = await createConversation(); await send(route); await expect(page.getByText(`${route}_DONE`, { exact: true })).toBeVisible({ timeout: 45_000 });
      await expect.poll(() => page.evaluate(() => !(window as any).__zustandStore.getState().isStreaming)).toBe(true);
      await expect(page.getByRole("button", { name: "停止当前回复", exact: true })).toHaveCount(0);
      await expect(page.locator('[data-session-row][data-active="true"] .session-status-spinner')).toHaveCount(0);
      await expect(page.locator(".assistant-cell-image-card")).toHaveCount(0);
      await expect(page.locator(".assistant-cell-image-placeholder[data-status=failed]")).toHaveCount(0);
      await showOriginalImageError(owner, route === "IMAGE_E2E_FAILURE" ? "11-provider-failure" : route === "IMAGE_E2E_UNSUPPORTED" ? "12-api-unsupported" : "13-malformed-image");
      expect(await page.locator("body").innerText()).not.toContain("请检查图像模型与 Provider 配置");
    }
    expect(errors).toEqual([]);
  } catch (error) {
    originalFailure = error;
    throw error;
  } finally {
    await writeFile(path.join(evidence, "wire.json"), JSON.stringify(wire, null, 2));
    await writeFile(path.join(evidence, "result.json"), JSON.stringify({ profile, workspace, providerRequests: provider.requests, imageRequests: provider.imageRequests, errors, timings, failure: originalFailure instanceof Error ? originalFailure.message : originalFailure }, null, 2));
    await writeFile(path.join(evidence, "desktop.log"), desktopLogs.join(""));
    await writeFile(path.join(evidence, "connection.json"), JSON.stringify(await page.evaluate(() => {
      const runtime = (window as any).__MINICODE_RUNTIME__ || {}, state = (window as any).__zustandStore?.getState();
      return { url: window.location.href, connected: state?.isConnected, runtimeKeys: Object.keys(runtime),
        runtimeLocation: Object.fromEntries(Object.entries(runtime).filter(([key, value]) => /url|port|ready|revision|status/i.test(key) && !/token|auth|key/i.test(key) && ["string", "number", "boolean"].includes(typeof value))) };
    }).catch(() => ({ closed: true })), null, 2));
    const cleanup = await Promise.allSettled([provider.close(), app.close()]);
    const cleanupErrors = cleanup.flatMap(result => result.status === "rejected" ? [result.reason] : []);
    if (!originalFailure && cleanupErrors.length) throw new AggregateError(cleanupErrors, "Isolated desktop cleanup failed");
  }
});
