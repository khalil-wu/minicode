/** The configured model is real; only the user-described image service is a loopback fixture. */
import { _electron as electron, expect, test } from "@playwright/test";
import { createServer } from "node:http";
import { createServer as portServer } from "node:net";
import { createRequire } from "node:module";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "../../..");
const enabled = process.env.MINICODE_REAL_IMAGE_AUTONOMY === "1";
const configuration = JSON.parse(process.env.MINICODE_REAL_IMAGE_CONFIG || "{}");
const evidence = path.resolve(process.env.MINICODE_REAL_IMAGE_EVIDENCE_ROOT || path.join(root, "output/playwright/multi-agent-boundary-20261031/real-image-autonomy"));
async function freePort() { const server = portServer(); await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve)); const port = (server.address() as { port: number }).port; await new Promise<void>(resolve => server.close(() => resolve())); return port; }

test("configured real model chooses the documented external image API and projects its result", async () => {
  test.skip(!enabled, "Opt-in: requires the current configured model, without changing user configuration.");
  test.setTimeout(240_000); await mkdir(evidence, { recursive: true });
  const profile = await mkdtemp(path.join(tmpdir(), "minicode-real-image-")), workspace = path.join(profile, "workspace");
  await Promise.all([mkdir(workspace), mkdir(path.join(profile, "config")), mkdir(path.join(profile, "data"))]);
  const provider = configuration.provider, section = { ...configuration, headers: Object.fromEntries(configuration.default_headers || []) };
  delete section.provider; delete section.api_key; delete section.default_headers;
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: true }, memory: { enabled: false }, features: { memories: false }, llm: { provider, [provider]: section } }));
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", confirmBeforeClose: "never", turnCompletionNotifications: "never", permissionNotifications: false, questionNotifications: false, defaultPermissionMode: "bypass" }));
  await writeFile(path.join(profile, "data/trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  let png = ""; const requests: Record<string, any>[] = [];
  const imageApi = createServer(async (request, response) => {
    if (request.method === "GET") { response.writeHead(200, { "content-type": "application/json" }); response.end(JSON.stringify({ data: [{ id: "fixture-image", capabilities: ["image_generation"] }] })); return; }
    const parts: Buffer[] = []; for await (const part of request) parts.push(Buffer.from(part));
    const body = JSON.parse(Buffer.concat(parts).toString("utf8")); requests.push({ at: Date.now(), method: request.method, path: request.url, authorizationPresent: Boolean(request.headers.authorization), body });
    const valid = request.url === "/v1/images/generations" && body.model === "fixture-image";
    response.writeHead(valid ? 200 : 400, { "content-type": "application/json" });
    response.end(JSON.stringify(valid ? { data: [{ b64_json: png }] } : { error: { message: "Use POST /v1/images/generations and the documented fixture-image model." } }));
  });
  await new Promise<void>(resolve => imageApi.listen(0, "127.0.0.1", resolve)); const imageUrl = `http://127.0.0.1:${(imageApi.address() as { port: number }).port}/v1`;
  const shim = path.join(profile, "inactive-entry.cjs"); await writeFile(shim, `const {app,BrowserWindow}=require('electron'); app.commandLine.appendSwitch('disable-renderer-backgrounding'); BrowserWindow.prototype.show=function(){this.showInactive()}; BrowserWindow.prototype.focus=function(){}; require(${JSON.stringify(path.join(root, "desktop/main.js"))});`);
  const { ELECTRON_RUN_AS_NODE: _node, ...environment } = process.env;
  const app = await electron.launch({ executablePath: createRequire(path.join(root, "desktop/package.json"))("electron"), args: [shim], cwd: path.join(root, "desktop"), env: {
    ...environment, MINICODE_USER_DATA_DIR: profile, MINICODE_STATE_ROOT: profile, MINICODE_CONFIG_DIR: path.join(profile, "config"), MINICODE_BACKEND_PORT: String(await freePort()), MINICODE_FRONTEND_URL: "", MINICODE_DISABLE_HARDWARE_ACCELERATION: "1",
    MINICODE_SYSTEM_CONFIG_FILE: path.join(profile, "absent-system.toml"), MINICODE_REQUIREMENTS_FILE: path.join(profile, "absent-requirements.toml"), MINICODE_MANAGED_SETTINGS_DIR: path.join(profile, "managed"),
    LLM_PROVIDER: provider, OPENAI_API_KEY: provider === "openai" ? process.env.MINICODE_REAL_IMAGE_KEY || "" : "", OPENAI_BASE_URL: provider === "openai" ? configuration.base_url : "", OPENAI_MODEL: provider === "openai" ? configuration.model : "",
    CUSTOM_API_KEY: provider === "custom" ? process.env.MINICODE_REAL_IMAGE_KEY || "" : "", CUSTOM_BASE_URL: provider === "custom" ? configuration.base_url : "", CUSTOM_MODEL: provider === "custom" ? configuration.model : "", CUSTOM_WIRE_API: provider === "custom" ? configuration.wire_api : "",
    CUSTOM_SMALL_FAST_MODEL: "", ANTHROPIC_API_KEY: "", ANTHROPIC_BASE_URL: "", ANTHROPIC_MODEL: "", PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8",
  } });
  const page = await app.firstWindow(), wire: Record<string, any>[] = [], errors: string[] = [], logs: string[] = [];
  page.on("pageerror", error => errors.push(error.message)); page.on("dialog", () => {});
  app.process().stdout?.on("data", bytes => logs.push(String(bytes))); app.process().stderr?.on("data", bytes => logs.push(String(bytes)));
  const cdp = await page.context().newCDPSession(page); await cdp.send("Network.enable");
  cdp.on("Network.webSocketFrameReceived", ({ response }) => { if (response.opcode === 1) wire.push({ at: Date.now(), ...JSON.parse(response.payloadData) }); });
  let failure: unknown, owner = "", passed = false; const started = Date.now();
  try {
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    expect(page.url()).toContain("/dist/index.html");
    png = await page.evaluate(() => { const canvas = document.createElement("canvas"); canvas.width = 480; canvas.height = 300; const context = canvas.getContext("2d")!; context.fillStyle = "#6d5efc"; context.fillRect(0, 0, 480, 300); context.fillStyle = "#2cccb9"; context.fillRect(150, 75, 180, 150); return canvas.toDataURL("image/png").split(",")[1]; });
    await page.evaluate(workspace => (window as any).__zustandStore.getState().createConversation({ bindWorkspace: true, workspaceRoot: workspace, appMode: "cowork" }), workspace);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).toBeTruthy(); owner = await page.evaluate(() => (window as any).__zustandStore.getState().conversationId);
    await page.evaluate(() => (window as any).__zustandStore.getState().setPermissionMode("bypass"));
    const input = page.locator('.composer-input-region [role="textbox"]').first();
    await input.fill(`请使用我提供的外部图片服务生成一张蓝紫色背景、绿色方块的图片，并直接在当前聊天展示真实返回的图片。接口文档：Base URL ${imageUrl}，POST /images/generations，JSON 参数 model="fixture-image"、prompt（描述）、n=1、response_format="b64_json"。成功返回 {"data":[{"b64_json":"PNG 的 base64 字节"}]}。此本地服务不需要 API Key 或 Authorization，也不用任何额外图片 Provider 设置。请根据这些接口信息及已有能力选择合适方式；不要自己绘制或拿别的图片替代 API 返回结果。`);
    await input.press("Enter");
    await expect.poll(() => requests.length > 0 || wire.some(event => event.type === "done" && event.conversation_id === owner), { timeout: 150_000 }).toBe(true);
    expect(requests.length, "The real configured model did not invoke the user-described image service; inspect the final response and wire.").toBeGreaterThan(0);
    await expect.poll(() => wire.some(event => event.type === "done" && event.conversation_id === owner), { timeout: 90_000 }).toBe(true);
    expect(wire.find(event => event.type === "done" && event.conversation_id === owner)?.status).toBe("completed");
    await expect(page.locator(".assistant-cell-generated-image")).toHaveCount(1); await expect.poll(() => page.locator(".assistant-cell-generated-image").evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth === 480)).toBe(true);
    await expect.poll(() => page.evaluate(() => !(window as any).__zustandStore.getState().isStreaming)).toBe(true);
    await expect(page.getByRole("button", { name: "停止当前回复", exact: true })).toHaveCount(0); await expect(page.locator(".session-status-spinner")).toHaveCount(0);
    expect(wire.some(event => event.type === "artifact.preview" && event.source === "image_generation" && event.conversation_id === owner)).toBe(true);
    expect(requests.every(request => !request.authorizationPresent)).toBe(true); expect(errors).toEqual([]); passed = true;
    await page.screenshot({ path: path.join(evidence, "01-real-model-selected-fixture-image.png"), animations: "disabled" });
  } catch (error) { failure = error; await page.screenshot({ path: path.join(evidence, "99-unverified.png"), animations: "disabled" }).catch(() => {}); throw error; }
  finally {
    try {
      await writeFile(path.join(evidence, "result.json"), JSON.stringify({ passed, provider, model: configuration.model, wireApi: configuration.wire_api, profile, workspace, owner, elapsedMs: Date.now() - started,
        modelInvokedImageApi: requests.length > 0, generatedImagePublished: wire.some(event => event.type === "artifact.preview" && event.source === "image_generation" && event.conversation_id === owner),
        terminalStatus: wire.find(event => event.type === "done" && event.conversation_id === owner)?.status,
        imageApiRequests: requests, errors, failure: failure instanceof Error ? failure.message : failure }, null, 2));
      await writeFile(path.join(evidence, "wire.json"), JSON.stringify(wire, null, 2)); await writeFile(path.join(evidence, "page.txt"), await page.locator("body").innerText()); await writeFile(path.join(evidence, "desktop.log"), logs.join(""));
    } finally { imageApi.closeAllConnections(); await Promise.allSettled([app.close(), new Promise<void>(resolve => imageApi.close(() => resolve()))]); }
  }
});
