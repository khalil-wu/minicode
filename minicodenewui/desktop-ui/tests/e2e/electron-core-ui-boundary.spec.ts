/** Controlled model HTTP and delayed delivery only. Desktop, approvals, tools, files, Git, WS and projections are production code. */
import { _electron as electron, expect, test, type Page } from "@playwright/test";
import { createServer } from "node:http";
import { createServer as portServer } from "node:net";
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

type Fact = Record<string, any>;
const root = path.resolve(import.meta.dirname, "../../..");
const evidence = path.resolve(process.env.MINICODE_CORE_UI_EVIDENCE_ROOT || path.join(root, "output/playwright/multi-agent-boundary-20261031/core-ui"));
const models = ["core-ui-model-a", "core-ui-model-b"];
const selectedFixtureModel = process.env.MINICODE_CORE_UI_SKIP_MODEL_SWITCH === "1" ? models[0] : models[1];
const text = (message: Fact) => typeof message.content === "string" ? message.content : (message.content || []).map((part: Fact) => part.text || "").join("\n");

class Provider {
  readonly requests: Fact[] = [];
  readonly counts = new Map<string, number>();
  readonly gates = new Map<string, () => void>();
  readonly server = createServer(async (request, response) => {
    if (request.method === "GET") { response.writeHead(200, { "content-type": "application/json" }); response.end(JSON.stringify({ data: models.map(id => ({ id })) })); return; }
    const bytes: Buffer[] = []; for await (const chunk of request) bytes.push(Buffer.from(chunk));
    const body = JSON.parse(Buffer.concat(bytes).toString("utf8"));
    const source = body.messages.filter((message: Fact) => message.role === "user").map(text).findLast((value: string) => /CORE_UI_[A-Z_]+/.test(value)) || "";
    const route = source.match(/CORE_UI_[A-Z_]+/)?.[0] || "auxiliary";
    const index = this.counts.get(route) || 0; this.counts.set(route, index + 1);
    const tools = body.tools?.map((tool: Fact) => tool.function.name) || [];
    this.requests.push({ at: Date.now(), route, index, body, tools });
    const latest = text(body.messages.findLast((message: Fact) => message.role === "tool") || {});
    const runningCell = latest.match(/Script running with cell ID (cell_[a-z0-9]+)/i)?.[1];
    const report = latest.includes('"cell_id"') ? JSON.parse(latest.slice(latest.indexOf("{"), latest.lastIndexOf("}") + 1))
      : runningCell ? { status: "running", cell_id: runningCell } : undefined;
    let content = route === "auxiliary" ? "Core UI audit" : `${route}_DONE`, calls: Fact[] = [];
    const call = (name: string, args: Fact) => { expect(tools).toContain(name); calls = [{ id: `${route}-${index}`, type: "function", function: { name, arguments: JSON.stringify(args) } }]; };
    if (route !== "auxiliary" && tools.length) {
      if (index === 0) {
        if (route.startsWith("CORE_UI_APPROVAL")) await new Promise<void>(resolve => this.gates.set(route + ":start", resolve));
        const code = route.startsWith("CORE_UI_APPROVAL")
          ? `text(await tools.run_command({command:${JSON.stringify(route === "CORE_UI_APPROVAL_DENY" ? "Set-Content -LiteralPath approval-denied-must-not-exist.txt -Value CORE_UI_DENIED_WRITTEN" : "Write-Output CORE_UI_APPROVAL_EXECUTED")}, sandbox_permissions:"require_escalated", justification:"Run the isolated UI acceptance marker", yield_time_ms:10000}));`
          : route === "CORE_UI_AGENT" ? 'text(await tools.task({description:"Verify exact model override",agent_type:"core-ui-override",prompt:"CORE_UI_AGENT_CHILD: Read first.txt and report the real bytes.",read_only:true}));'
          : route === "CORE_UI_AGENT_CHILD" ? 'text(await tools.read_file({file_path:"first.txt"}));'
          : route === "CORE_UI_INTERRUPT" ? 'text(await tools.read_file({file_path:"first.txt"})); await new Promise(resolve => setTimeout(resolve,60000)); text("CORE_UI_MUST_NOT_COMPLETE");'
          : route === "CORE_UI_DIFF" ? 'text(await tools.read_file({file_path:"first.txt"})); text(await tools.read_file({file_path:"second.txt"})); text(await tools.edit_file({file_path:"first.txt",old_string:"FIRST_BASELINE",new_string:"FIRST_EDITED"})); text(await tools.edit_file({file_path:"second.txt",old_string:"SECOND_BASELINE",new_string:"SECOND_EDITED"}));'
          : 'text(await tools.read_file({file_path:"first.txt"})); text(await tools.run_command({command:"Write-Output CORE_UI_REAL_STDOUT; Write-Error CORE_UI_REAL_STDERR -ErrorAction Continue; exit 7",yield_time_ms:10000}));';
        call("tool_exec", { yield_time_ms: route === "CORE_UI_INTERRUPT" ? 0 : 10000, code });
      } else if (report?.status === "running") call("tool_wait", { cell_id: report.cell_id, yield_time_ms: 1000 });
      else if (route === "CORE_UI_FAILED") {
        response.writeHead(400, { "content-type": "application/json" }); response.end(JSON.stringify({ error: { message: "CORE_UI_REAL_MODEL_REJECTION", type: "invalid_request_error" } })); return;
      } else if (["CORE_UI_AUTO", "CORE_UI_KEEP", "CORE_UI_BACKGROUND"].includes(route)) await new Promise<void>(resolve => this.gates.set(route + ":final", resolve));
    }
    response.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
    const delta = calls.length ? { tool_calls: calls.map((item, index) => ({ index, ...item })) } : { content };
    response.write(`data: ${JSON.stringify({ id: `core-${route}-${index}`, choices: [{ index: 0, delta, finish_reason: null }] })}\n\n`);
    response.write(`data: ${JSON.stringify({ id: `core-${route}-${index}`, choices: [{ index: 0, delta: {}, finish_reason: calls.length ? "tool_calls" : "stop" }] })}\n\n`);
    response.end("data: [DONE]\n\n");
  });
  async start() { await new Promise<void>(resolve => this.server.listen(0, "127.0.0.1", resolve)); return `http://127.0.0.1:${(this.server.address() as { port: number }).port}/v1`; }
  release(key: string) { this.gates.get(key)!(); this.gates.delete(key); }
  async close() { for (const release of this.gates.values()) release(); this.server.closeAllConnections(); await new Promise<void>(resolve => this.server.close(() => resolve())); }
}
async function freePort() { const server = portServer(); await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve)); const port = (server.address() as { port: number }).port; await new Promise<void>(resolve => server.close(() => resolve())); return port; }

test("core desktop ownership, approvals, disclosure, failure recovery and independent file diffs", async () => {
  test.setTimeout(300_000); await mkdir(evidence, { recursive: true });
  const profile = await mkdtemp(path.join(tmpdir(), "minicode-core-ui-"));
  const workspace = path.join(profile, "workspace"), config = path.join(profile, "config");
  await Promise.all([mkdir(workspace), mkdir(config), mkdir(path.join(profile, "data"))]);
  await writeFile(path.join(workspace, "first.txt"), "FIRST_BASELINE\n"); await writeFile(path.join(workspace, "second.txt"), "SECOND_BASELINE\n");
  const attachment = path.join(workspace, "draft-attachment.txt"); await writeFile(attachment, "PRESERVED_ATTACHMENT\n");
  const git = (...args: string[]) => execFileSync("git", ["-C", workspace, ...args], { windowsHide: true, encoding: "utf8" });
  git("init", "-b", "main"); git("config", "user.name", "Isolated QA"); git("config", "user.email", "qa@example.test"); git("config", "core.autocrlf", "false"); git("add", "."); git("commit", "-m", "Isolated baseline");
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", confirmBeforeClose: "never", turnCompletionNotifications: "never", permissionNotifications: false, questionNotifications: false }));
  await writeFile(path.join(profile, "data/trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  const provider = new Provider(), providerUrl = await provider.start();
  const metadata = { [models[0]]: { reasoning_effort_levels: ["low", "medium", "high"], default_reasoning_effort: "medium", context_window: 96000 },
    [models[1]]: { reasoning_effort_levels: ["low", "high"], default_reasoning_effort: "low", context_window: 96000 } };
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: true }, memory: { enabled: false }, features: { memories: false },
    llm: { provider: "custom", custom: { base_url: providerUrl, api_key: "core-ui-key", model: models[0], wire_api: "chat", reasoning_effort: "medium", available_models: models, model_metadata: metadata } } }));
  const shim = path.join(profile, "inactive-entry.cjs"); await writeFile(shim, `const {app,BrowserWindow}=require('electron'); app.commandLine.appendSwitch('disable-renderer-backgrounding'); BrowserWindow.prototype.show=function(){this.showInactive()}; BrowserWindow.prototype.focus=function(){}; require(${JSON.stringify(path.join(root, "desktop/main.js"))}); globalThis.__coreQaReload=()=>require(${JSON.stringify(path.join(root, "desktop/window-manager.js"))}).reloadWindow(BrowserWindow.getAllWindows()[0]);`);
  const { ELECTRON_RUN_AS_NODE: _node, ...environment } = process.env;
  const app = await electron.launch({ executablePath: createRequire(path.join(root, "desktop/package.json"))("electron"), args: [shim], cwd: path.join(root, "desktop"), env: {
    ...environment, MINICODE_USER_DATA_DIR: profile, MINICODE_STATE_ROOT: profile, MINICODE_CONFIG_DIR: config, MINICODE_BACKEND_PORT: String(await freePort()), MINICODE_FRONTEND_URL: "", MINICODE_DISABLE_HARDWARE_ACCELERATION: "1",
    MINICODE_SYSTEM_CONFIG_FILE: path.join(profile, "no-system.toml"), MINICODE_REQUIREMENTS_FILE: path.join(profile, "no-requirements.toml"), MINICODE_MANAGED_SETTINGS_DIR: path.join(profile, "managed"),
    LLM_PROVIDER: "custom", CUSTOM_API_KEY: "core-ui-key", CUSTOM_BASE_URL: providerUrl, CUSTOM_MODEL: models[0], CUSTOM_WIRE_API: "chat", CUSTOM_REASONING_EFFORT: "", CUSTOM_SMALL_FAST_MODEL: "",
    OPENAI_API_KEY: "", OPENAI_BASE_URL: "", OPENAI_MODEL: "", ANTHROPIC_API_KEY: "", ANTHROPIC_BASE_URL: "", ANTHROPIC_MODEL: "", PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8",
  } });
  const page = await app.firstWindow(), wire: Fact[] = [], errors: string[] = [], logs: string[] = [], checks: string[] = [], timings: Fact[] = [];
  page.on("pageerror", error => errors.push(error.message)); app.process().stdout?.on("data", bytes => logs.push(String(bytes))); app.process().stderr?.on("data", bytes => logs.push(String(bytes)));
  // The native draft-commit barrier owns beforeunload. Playwright's automatic
  // dismissal races the real resumeUnload and can address an already-gone dialog.
  page.on("dialog", dialog => logs.push(`Native unload barrier dialog: ${dialog.type()}\n`));
  let heldOwner = "", releaseSwitch: (() => void) | undefined;
  await page.routeWebSocket("**", socket => {
    const server = socket.connectToServer();
    const delayed: (string | Buffer)[] = [];
    let paused = false;
    socket.onMessage(message => { wire.push({ at: Date.now(), outbound: true, ...JSON.parse(String(message)) }); server.send(message); });
    server.onMessage(message => {
      const event = JSON.parse(String(message)); wire.push({ at: Date.now(), ...event });
      if (paused) { delayed.push(message); return; }
      if (heldOwner && event.type === "conversation.switched" && (event.conversation_id || event.conversation?.id) === heldOwner) {
        paused = true; delayed.push(message);
        releaseSwitch = () => { heldOwner = ""; paused = false; for (const item of delayed.splice(0)) socket.send(item); };
        return;
      }
      socket.send(message);
    });
  });
  const ready = async () => expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
  const reload = async () => {
    await Promise.all([page.waitForEvent("load"), app.evaluate(() => (globalThis as any).__coreQaReload())]); await ready();
    expect(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length)).toBe(1);
  };
  const input = () => page.locator('.composer-input-region [role="textbox"]').first();
  const capture = async (name: string) => { await page.screenshot({ path: path.join(evidence, name + ".png"), animations: "disabled" }); await writeFile(path.join(evidence, name + ".txt"), await page.locator("body").innerText());
    await writeFile(path.join(evidence, "latest.json"), JSON.stringify({ phase: name, checks, timings, state: await state(), wire }, null, 2)); };
  const state = () => page.evaluate(() => { const s = (window as any).__zustandStore.getState(); return { owner: s.conversationId, pending: s.pendingConversationSwitchId, streaming: s.isStreaming, draft: s.draft,
    permission: s.permissionMode, model: s.currentModel, effort: s.effortLevel, messages: s.messages, attachments: s.attachments, runtime: s.conversationRuntimeStates }; });
  const create = async (mode = "bypass") => { const before = (await state()).owner; await page.evaluate(workspace => (window as any).__zustandStore.getState().createConversation({ bindWorkspace: true, workspaceRoot: workspace, appMode: "cowork" }), workspace);
    await expect.poll(async () => { const s = await state(); return Boolean(s.owner && s.owner !== before && !s.pending); }).toBe(true);
    await page.evaluate(mode => (window as any).__zustandStore.getState().setPermissionMode(mode), mode); await expect.poll(async () => (await state()).permission).toBe(mode); return (await state()).owner as string; };
  const select = async (owner: string) => { await page.evaluate(owner => (window as any).__zustandStore.getState().requestConversationSwitch(owner), owner); await expect.poll(async () => { const s = await state(); return s.owner === owner && !s.pending; }).toBe(true); };
  const idle = async (owner: string) => { await expect.poll(() => wire.some(event => !event.outbound && event.type === "done" && event.conversation_id === owner), { timeout: 45_000 }).toBe(true);
    await expect.poll(async () => { const s = await state(); return s.owner === owner && !s.streaming && !s.pending; }, { timeout: 45_000 }).toBe(true);
    await expect(page.getByRole("button", { name: "停止当前回复", exact: true })).toHaveCount(0); await expect(page.locator('[data-session-row][data-active="true"] .session-status-spinner')).toHaveCount(0); };
  const send = async (route: string) => { const at = Date.now(); await input().fill(route); await input().press("Enter"); await expect(input()).toHaveText(""); timings.push({ name: route + ":composer-ack", ms: Date.now() - at });
    await expect.poll(() => provider.requests.some(request => request.route === route), { timeout: 30_000 }).toBe(true); };
  const attach = async () => { const chooser = page.waitForEvent("filechooser"); await page.getByRole("button", { name: "添加文件等内容", exact: true }).click(); await page.getByRole("menuitem", { name: "文件和文件夹", exact: true }).click(); await (await chooser).setFiles(attachment); await expect.poll(async () => (await state()).attachments[0]?.status).toBe("ready"); };
  let failure: unknown;
  try {
    await ready(); await reload(); expect(page.url()).toContain("/dist/index.html");
    if (process.env.MINICODE_CORE_UI_TAIL_ONLY !== "1") {
    const approvalOwner = await create("confirm");
    await expect(page.locator(".composer-model-controls .composer-model-select")).toHaveCount(1);
    const menuAt = Date.now(); await page.locator(".composer-model-select").click();
    await expect(page.getByRole("slider", { name: "推理强度", exact: true })).toBeVisible(); timings.push({ name: "model-effort-menu", ms: Date.now() - menuAt });
    await capture("01-combined-model-effort-menu"); await page.getByRole("button", { name: "选择模型", exact: true }).click(); await capture("02-model-choices");
    await page.getByRole("option", { name: selectedFixtureModel, exact: true }).click(); await expect.poll(async () => (await state()).model).toBe(selectedFixtureModel);
    await expect.poll(async () => (await state()).effort).toBe(selectedFixtureModel === models[1] ? "low" : "medium");
    await expect(page.getByRole("listbox", { name: "选择模型", exact: true })).toHaveCount(0);
    await page.locator(".composer-model-select").press("Escape"); await expect(page.locator(".composer-model-select")).toHaveAttribute("aria-expanded", "false");
    await page.locator(".composer-model-select").click();
    await page.getByRole("slider", { name: "推理强度", exact: true }).press("End"); await page.getByRole("slider", { name: "推理强度", exact: true }).press("Escape");
    await expect.poll(async () => (await state()).effort).toBe("high"); checks.push("single model+effort control uses actual model/effort WS commands");
    await send("CORE_UI_APPROVAL"); await expect.poll(() => provider.gates.has("CORE_UI_APPROVAL:start")).toBe(true);
    await input().fill("PRESERVE_APPROVAL_DRAFT"); await attach(); provider.release("CORE_UI_APPROVAL:start");
    await expect(page.locator(".inline-approval-bar")).toBeVisible({ timeout: 30_000 }); await expect(page.locator(".composer-input-region")).toBeHidden();
    await expect(page.locator('.composer-container[data-approval-replacement="true"] .inline-approval-bar')).toHaveCount(1);
    await expect(page.locator('.message-list-content .inline-approval-bar')).toHaveCount(0); await capture("03-approval-replaces-composer");
    const second = await create(); await input().fill("PRESERVE_SECOND_DRAFT"); await expect(page.locator(".inline-approval-bar")).toHaveCount(0); await select(approvalOwner);
    await expect(page.locator(".inline-approval-bar")).toBeVisible(); const approveAt = Date.now(); await page.getByRole("button", { name: "允许使用工具", exact: true }).click();
    await expect(page.locator(".composer-input-region")).toBeVisible(); await expect(input()).toHaveText("PRESERVE_APPROVAL_DRAFT");
    timings.push({ name: "approval-to-composer", ms: Date.now() - approveAt }); expect((await state()).attachments[0]?.name).toBe("draft-attachment.txt");
    await expect(page.getByText("CORE_UI_APPROVAL_DONE", { exact: true })).toBeVisible({ timeout: 30_000 }); await idle(approvalOwner); await capture("04-approved-idle-draft-attachment");
    expect(wire.some(event => !event.outbound && event.type === "control_request" && (event.conversation_id === approvalOwner || event.request?.conversation_id === approvalOwner))).toBe(true);
    expect(provider.requests.find(request => request.route === "CORE_UI_APPROVAL")!.body).toMatchObject({ model: selectedFixtureModel, reasoning_effort: "high" }); checks.push("real escalation approval replaces composer and restores owned draft/attachment across conversation switches");
    await page.locator(".composer-model-select").click(); await page.getByRole("button", { name: "重置为默认", exact: true }).click();
    await expect.poll(async () => { const s = await state(); return [s.model, s.effort]; }).toEqual([models[0], "medium"]);
    await expect(input()).toHaveText("PRESERVE_APPROVAL_DRAFT"); expect((await state()).attachments[0]?.name).toBe("draft-attachment.txt");
    await capture("04a-project-model-default-restored"); checks.push("unsupported effort selects the new model default and reset restores the actual project pair without clearing draft or attachment");
    const denyOwner = await create("confirm"); await send("CORE_UI_APPROVAL_DENY"); await expect.poll(() => provider.gates.has("CORE_UI_APPROVAL_DENY:start")).toBe(true);
    await input().fill("PRESERVE_DENIED_DRAFT"); provider.release("CORE_UI_APPROVAL_DENY:start"); await expect(page.locator(".inline-approval-bar")).toBeVisible({ timeout: 30_000 });
    await page.getByRole("button", { name: "拒绝使用工具", exact: true }).click(); await idle(denyOwner); await expect(input()).toHaveText("PRESERVE_DENIED_DRAFT");
    await expect(readFile(path.join(workspace, "approval-denied-must-not-exist.txt"))).rejects.toMatchObject({ code: "ENOENT" }); await capture("04b-denied-no-execution"); checks.push("denied real approval restores draft and never executes the requested file write");
    const background = await create(); await send("CORE_UI_BACKGROUND"); await expect.poll(() => provider.gates.has("CORE_UI_BACKGROUND:final"), { timeout: 30_000 }).toBe(true);
    await select(second); await expect(input()).toHaveText("PRESERVE_SECOND_DRAFT"); await expect(page.locator(".session-status-spinner")).not.toHaveCount(0);
    provider.release("CORE_UI_BACKGROUND:final"); await expect.poll(() => wire.some(event => !event.outbound && event.type === "done" && event.conversation_id === background)).toBe(true);
    await expect(page.locator(".session-status-spinner")).toHaveCount(0); await expect(page.getByRole("button", { name: "停止当前回复", exact: true })).toHaveCount(0); await capture("05-background-finished-other-draft");
    heldOwner = background; await page.evaluate(owner => (window as any).__zustandStore.getState().requestConversationSwitch(owner), background);
    await expect(page.locator('.chat-pane[data-switching="true"] .chat-pane-switch-status')).toBeVisible(); await expect(page.locator(".session-status-spinner")).toHaveCount(0); await capture("06-real-switch-delivery-pending");
    const switchAt = Date.now(); releaseSwitch!(); await expect(page.locator('.chat-pane[data-switching="true"]')).toHaveCount(0); timings.push({ name: "released-switch-to-message", ms: Date.now() - switchAt }); await idle(background); checks.push("background task spinner tracks real runtime and switch loading stays centered in messages");
    const auto = await create(); await send("CORE_UI_AUTO"); await expect.poll(() => provider.gates.has("CORE_UI_AUTO:final"), { timeout: 30_000 }).toBe(true); await capture("07-auto-process-running"); provider.release("CORE_UI_AUTO:final");
    await expect(page.getByText("CORE_UI_AUTO_DONE", { exact: true })).toBeVisible(); await idle(auto); await expect(page.locator('.chat-turn-process')).toHaveAttribute("data-collapsed", "true"); await capture("08-completed-process-folded");
    const keep = await create(); await send("CORE_UI_KEEP"); await expect.poll(() => provider.gates.has("CORE_UI_KEEP:final"), { timeout: 30_000 }).toBe(true);
    await page.getByRole("button", { name: "收起处理步骤", exact: true }).click(); await page.getByRole("button", { name: "展开处理步骤", exact: true }).click(); provider.release("CORE_UI_KEEP:final");
    await expect(page.getByText("CORE_UI_KEEP_DONE", { exact: true })).toBeVisible(); await idle(keep); await expect(page.locator('.chat-turn-process')).toHaveAttribute("data-collapsed", "false"); await capture("09-explicit-reading-retained"); checks.push("complete final answer folds untouched process but preserves the user's explicit expansion");
    }
    const failed = await create(); await send("CORE_UI_FAILED"); await idle(failed); await expect(page.locator('.chat-turn-process')).toHaveAttribute("data-collapsed", "false");
    expect(JSON.stringify((await state()).messages)).toContain("CORE_UI_REAL_STDERR"); await capture("10-real-failure-evidence");
    const interrupted = await create(); await send("CORE_UI_INTERRUPT"); await expect.poll(() => {
      const read = wire.find(event => event.type === "tool_call" && event.name === "read_file" && event.conversation_id === interrupted);
      return read && wire.some(event => event.type === "tool_result" && event.id === read.id && event.conversation_id === interrupted);
    }, { timeout: 30_000 }).toBeTruthy();
    const stopAt = Date.now(); await page.getByRole("button", { name: "停止当前回复", exact: true }).click(); await idle(interrupted); timings.push({ name: "stop-to-idle", ms: Date.now() - stopAt });
    expect(wire.filter(event => event.type === "tool_result" && event.conversation_id === interrupted).map(event => event.summary).join("\n")).not.toContain("CORE_UI_MUST_NOT_COMPLETE");
    await capture("11-interrupted-evidence"); const reconnectAt = Date.now(); await reload(); await select(interrupted); await idle(interrupted); timings.push({ name: "native-reload-to-idle-evidence", ms: Date.now() - reconnectAt }); await expect(page.locator('.chat-turn-process')).toHaveAttribute("data-collapsed", "false");
    expect(JSON.stringify((await state()).messages)).toContain("FIRST_BASELINE"); await capture("12-interrupted-cold-reconnect"); await select(failed); await idle(failed); expect(JSON.stringify((await state()).messages)).toContain("CORE_UI_REAL_STDERR"); checks.push("failed and interrupted turns preserve real evidence through cold reconnect without false running");
    const diff = await create(); await send("CORE_UI_DIFF"); await expect(page.getByText("CORE_UI_DIFF_DONE", { exact: true })).toBeVisible({ timeout: 30_000 }); await idle(diff);
    expect(await readFile(path.join(workspace, "first.txt"), "utf8")).toBe("FIRST_EDITED\n"); expect(await readFile(path.join(workspace, "second.txt"), "utf8")).toBe("SECOND_EDITED\n");
    const diffAt = Date.now(); await page.getByRole("button", { name: "查看变更", exact: true }).click(); const sections = page.locator(".mc-diff-file-section"); await expect(sections).toHaveCount(2); timings.push({ name: "open-two-file-diff", ms: Date.now() - diffAt });
    const firstDiff = page.locator('.mc-diff-file-section[data-path="first.txt"]'), secondDiff = page.locator('.mc-diff-file-section[data-path="second.txt"]');
    const secondBefore = await secondDiff.getAttribute("data-open"); await firstDiff.locator('.mc-diff-file-section-trigger').click(); await expect(secondDiff).toHaveAttribute("data-open", secondBefore!);
    const firstBefore = await firstDiff.getAttribute("data-open"); await secondDiff.locator('.mc-diff-file-section-trigger').click(); await expect(firstDiff).toHaveAttribute("data-open", firstBefore!);
    await firstDiff.locator('.mc-diff-file-section-trigger').click(); await expect(firstDiff).toHaveAttribute("data-open", firstBefore === "true" ? "false" : "true"); await capture("13-independent-diff-files");
    checks.push("two real edited file diffs expand independently without changing the other file"); expect(errors).toEqual([]);
    const agentOwner = await create(); await page.evaluate(() => (window as any).__zustandStore.getState().toggleAgentEditor());
    await expect(page.getByRole("button", { name: "关闭 Agent 编辑器", exact: true })).toBeVisible();
    await page.getByLabel("名称", { exact: true }).fill("core-ui-override"); await page.getByLabel("说明", { exact: true }).fill("Isolated exact model override verification");
    await page.getByRole("button", { name: /^模型，当前：/ }).click(); await page.getByRole("option", { name: /core-ui-model-b/ }).click();
    await page.getByRole("button", { name: /^推理强度，当前：/ }).click(); await page.getByRole("option", { name: "high", exact: true }).click();
    await page.getByLabel("系统提示词", { exact: true }).fill("Read the requested file and report only actual results."); await capture("14-agent-exact-model-form");
    await page.getByRole("button", { name: "保存", exact: true }).click(); await expect(page.locator(".agent-editor-sidebar").getByText("core-ui-override", { exact: true })).toBeVisible();
    const agentSource = await readFile(path.join(workspace, ".minicode/agents/core-ui-override.md"), "utf8"); expect(agentSource).toContain("model: custom/core-ui-model-b"); expect(agentSource).toContain("effort: high");
    await page.getByRole("button", { name: "关闭 Agent 编辑器", exact: true }).click(); await send("CORE_UI_AGENT");
    await expect(page.getByText("CORE_UI_AGENT_DONE", { exact: true })).toBeVisible({ timeout: 45_000 }); await idle(agentOwner);
    expect(provider.requests.find(request => request.route === "CORE_UI_AGENT")!.body.model).toBe(models[0]);
    expect(provider.requests.find(request => request.route === "CORE_UI_AGENT_CHILD")!.body).toMatchObject({ model: models[1], reasoning_effort: "high" });
    await capture("15-real-child-model-override"); checks.push("Agent editor model selection persists and a real child request uses its exact model/effort while parent keeps its own model");
  } catch (error) { failure = error; await capture("99-failure").catch(() => {}); throw error; }
  finally {
    releaseSwitch?.();
    try {
      await writeFile(path.join(evidence, "wire.json"), JSON.stringify(wire, null, 2)); await writeFile(path.join(evidence, "requests.json"), JSON.stringify(provider.requests, null, 2));
      await writeFile(path.join(evidence, "result.json"), JSON.stringify({ profile, workspace, checks, timings, errors,
        tailOnly: process.env.MINICODE_CORE_UI_TAIL_ONLY === "1", modelSwitchSkipped: selectedFixtureModel !== models[1],
        failure: failure instanceof Error ? failure.message : failure }, null, 2)); await writeFile(path.join(evidence, "desktop.log"), logs.join(""));
    } finally {
      const cleanup = await Promise.allSettled([provider.close(), app.close()]); if (!failure && cleanup.some(item => item.status === "rejected")) throw new Error("Core desktop cleanup failed");
    }
  }
});
