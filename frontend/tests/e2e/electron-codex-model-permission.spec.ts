/** Provider HTTP replies are controlled; Desktop/IPC/WS/config/ModelRuntime/repository are production code. */
import { _electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import { createRequire } from "node:module";
import { createServer } from "node:http";
import { createServer as portServer } from "node:net";
import { cp, mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

type Fact = Record<string, any>;
const root = path.resolve(import.meta.dirname, "../../..");
const audit = path.join(root, "output/playwright/codex-control-audit-20261013");
const evidenceRoot = path.resolve(process.env.MINICODE_MODEL_PERMISSION_E2E_EVIDENCE_ROOT || path.join(audit, "production-model-permission-run01"));
const modelA = "audit-model-a", modelB = "audit-model-b";
const marker = /MODEL_PERMISSION_QA:([a-z0-9_-]+)/i;
const messageText = (message: Fact): string => typeof message.content === "string" ? message.content : message.content.map((part: Fact) => part.text || "").join("\n");

class ControlledProvider {
  readonly requests: Fact[] = [];
  readonly responses: Fact[] = [];
  phase = "warm";
  constructor(readonly models: string[]) {}
  readonly server = createServer(async (request, response) => {
    if (request.method === "GET") {
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify({ data: this.models.map(id => ({ id, object: "model" })) }));
      return;
    }
    const chunks: Buffer[] = []; for await (const chunk of request) chunks.push(Buffer.from(chunk));
    const rawBody = Buffer.concat(chunks).toString("utf8"), body = JSON.parse(rawBody);
    const lastProbe = body.messages.filter((message: Fact) => message.role === "user").map(messageText).findLast((text: string) => marker.test(text));
    const caseKey = lastProbe?.match(marker)[1] || "auxiliary";
    const main = Boolean(body.tools?.length), content = main && caseKey !== "auxiliary" ? `MODEL_PERMISSION_COMPLETE:${caseKey}` : "Model controls audit";
    const index = this.requests.length, id = `model-control-${index}`;
    this.requests.push({ phase: this.phase, index, at: new Date().toISOString(), url: request.url, caseKey, main, rawBody, body });
    const frames = [
      { id, object: "chat.completion.chunk", choices: [{ index: 0, delta: { content }, finish_reason: null }] },
      { id, object: "chat.completion.chunk", choices: [{ index: 0, delta: {}, finish_reason: "stop" }] },
    ];
    const bytes = frames.map(frame => `data: ${JSON.stringify(frame)}\n\n`).join("") + "data: [DONE]\n\n";
    this.responses.push({ phase: this.phase, index, caseKey, main, frames, bytes });
    response.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" }); response.end(bytes);
  });
  async start() {
    await new Promise<void>(resolve => this.server.listen(0, "127.0.0.1", resolve));
    return `http://127.0.0.1:${(this.server.address() as { port: number }).port}/v1`;
  }
  async close() { await new Promise<void>((resolve, reject) => this.server.close(error => error ? reject(error) : resolve())); }
}

async function freePort() {
  const server = portServer(); await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const value = (server.address() as { port: number }).port; await new Promise<void>(resolve => server.close(() => resolve())); return value;
}

class DesktopRun {
  app!: ElectronApplication;
  page!: Page;
  phase = "warm";
  readonly wire: Fact[] = [];
  readonly desktopOutput: Fact[] = [];
  readonly pageErrors: Fact[] = [];
  readonly rendererWarnings: Fact[] = [];
  readonly checks: Fact[] = [];
  readonly native: Fact[] = [];
  readonly records: Fact[] = [];
  readonly screenshots: string[] = [];
  currentScenario = "launch";
  passed = false;
  closed = true;
  constructor(readonly profile: string, readonly workspace: string, readonly evidence: string, readonly provider: ControlledProvider) {}
  async launch() {
    const { ELECTRON_RUN_AS_NODE: _node, ...environment } = process.env;
    this.provider.phase = this.phase;
    this.app = await _electron.launch({ executablePath: createRequire(path.join(root, "desktop/package.json"))("electron"),
      args: [path.join(audit, "offscreen-desktop-entry.cjs")], cwd: path.join(root, "desktop"),
      env: { ...environment, MINICODE_USER_DATA_DIR: this.profile, MINICODE_BACKEND_PORT: String(await freePort()), MINICODE_FRONTEND_URL: "",
        MINICODE_DISABLE_HARDWARE_ACCELERATION: "1", MINICODE_SYSTEM_CONFIG_FILE: path.join(this.profile, "absent-system.toml"),
        MINICODE_REQUIREMENTS_FILE: path.join(this.profile, "absent-requirements.toml"), MINICODE_MANAGED_SETTINGS_DIR: path.join(this.profile, "managed"),
        LLM_PROVIDER: "custom", CUSTOM_API_KEY: "fixture-model-control", CUSTOM_BASE_URL: "", CUSTOM_MODEL: "", CUSTOM_WIRE_API: "chat", CUSTOM_REASONING_EFFORT: "", CUSTOM_SMALL_FAST_MODEL: "",
        OPENAI_API_KEY: "", OPENAI_BASE_URL: "", OPENAI_MODEL: "", ANTHROPIC_API_KEY: "", ANTHROPIC_BASE_URL: "", ANTHROPIC_MODEL: "" } });
    this.closed = false;
    const phase = this.phase;
    this.app.process().stdout?.on("data", bytes => this.desktopOutput.push({ phase, stream: "stdout", text: String(bytes) }));
    this.app.process().stderr?.on("data", bytes => this.desktopOutput.push({ phase, stream: "stderr", text: String(bytes) }));
    this.page = await this.app.firstWindow();
    this.page.on("pageerror", error => this.pageErrors.push({ phase, message: error.message }));
    this.page.on("console", event => { if (["warning", "error"].includes(event.type())) this.rendererWarnings.push({ phase, type: event.type(), text: event.text() }); });
    const cdp = await this.page.context().newCDPSession(this.page); await cdp.send("Network.enable");
    cdp.on("Network.webSocketFrameReceived", ({ response }) => { if (response.opcode === 1) this.wire.push({ phase, at: new Date().toISOString(), ...JSON.parse(response.payloadData) }); });
    cdp.on("Network.webSocketFrameSent", ({ response }) => { if (response.opcode === 1) this.wire.push({ phase, at: new Date().toISOString(), outbound: true, ...JSON.parse(response.payloadData) }); });
    await expect.poll(() => this.page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60_000 }).toBe(true);
    expect(this.page.url()).toContain("/dist/index.html");
    await this.checkHidden();
  }
  async state(): Promise<Fact> {
    return this.page.evaluate(() => {
      const state = (window as any).__zustandStore.getState();
      return { connected: state.isConnected, owner: state.conversationId, workspace: state.workingDirectory, model: state.currentModel,
        provider: state.currentProvider, effort: state.effortLevel, mode: state.modelSelectionMode, permission: state.permissionMode,
        defaults: state.defaultModelSelection, options: state.availableModelOptions, allowedPermissions: state.runtimeCapabilities?.permission?.available_modes,
        streaming: state.isStreaming, draft: state.draft };
    });
  }
  async semantic(mark: number, type: string, commandName: string) {
    await expect.poll(() => this.wire.slice(mark).some(frame => frame.outbound && frame.type === type)).toBe(true);
    const command = this.wire.slice(mark).find(frame => frame.outbound && frame.type === type)!;
    await expect.poll(() => this.wire.some(frame => !frame.outbound && frame.type === "command.result" && frame.command === commandName
      && frame.client_command_id === command.client_command_id && ["success", "info"].includes(frame.level))).toBe(true);
    return command;
  }
  async selection(model: string, effort: string, mode?: string) {
    await expect.poll(() => this.state().then(state => ({ model: state.model, effort: state.effort, ...(mode ? { mode: state.mode } : {}) })))
      .toEqual({ model, effort, ...(mode ? { mode } : {}) });
  }
  async firstConversation() {
    const mark = this.wire.length;
    // Fixture setup uses the existing production action: it issues conversation.create through the real outbox/WS/repository.
    // Model/effort/permission changes and later conversation navigation use visible UI controls only. No setState or fake events.
    await this.page.evaluate(workspace => (window as any).__zustandStore.getState().createConversation({ bindWorkspace: true, workspaceRoot: workspace, appMode: "cowork" }), this.workspace);
    await this.semantic(mark, "conversation.create", "conversation.create");
    await expect.poll(() => this.state().then(state => Boolean(state.owner) && path.resolve(state.workspace).toLowerCase() === path.resolve(this.workspace).toLowerCase())).toBe(true);
    return (await this.state()).owner as string;
  }
  async rename(title: string) {
    const mark = this.wire.length;
    await this.page.getByRole("button", { name: "聊天操作", exact: true }).click();
    await this.page.getByRole("menuitem", { name: "重命名", exact: true }).click();
    const dialog = this.page.getByRole("dialog", { name: "重命名聊天", exact: true });
    await dialog.getByRole("textbox").fill(title); await dialog.getByRole("button", { name: "保存", exact: true }).click();
    await this.semantic(mark, "conversation.rename", "conversation.rename");
    await expect(this.page.locator('[data-session-row="true"]').getByRole("button", { name: title, exact: true }).first()).toBeVisible();
  }
  async switchConversation(title: string, owner: string) {
    await this.page.locator('[data-session-row="true"]').getByRole("button", { name: title, exact: true }).first().click();
    await expect.poll(() => this.state().then(state => state.owner)).toBe(owner);
  }
  async power() {
    const slider = this.page.getByRole("slider", { name: "推理强度", exact: true });
    if (!await slider.isVisible()) await this.page.getByRole("button", { name: /^模型与推理强度：/ }).click();
    await expect(slider).toBeVisible(); return slider;
  }
  async models() {
    await this.power(); await this.page.getByRole("button", { name: "选择模型", exact: true }).click();
    await expect(this.page.getByRole("listbox", { name: "选择模型", exact: true })).toBeVisible();
  }
  async chooseModel(model: string, effort: string, screenshot?: string) {
    const mark = this.wire.length; await this.models(); if (screenshot) await this.capture(screenshot);
    await this.page.getByRole("option", { name: model, exact: true }).click();
    const command = await this.semantic(mark, "llm.model.set", "model.set");
    expect(command).toMatchObject({ model, selection_mode: "model" }); await this.selection(model, effort, "model");
  }
  async extremeEffort(key: "Home" | "End", effort: string) {
    const mark = this.wire.length, model = (await this.state()).model;
    await (await this.power()).press(key); await this.semantic(mark, "llm.config.set", "effort"); await this.selection(model, effort, "model");
  }
  async record(owner: string, name: string) {
    const manifest = JSON.parse(await readFile(path.join(this.profile, "data/conversations", `${owner}.manifest.json`), "utf8"));
    this.records.push({ phase: this.phase, name, owner, manifest }); return manifest.metadata as Fact;
  }
  async probe(key: string, model: string, effort: string) {
    const state = await this.state(), mark = this.wire.length, before = this.provider.requests.length;
    const input = this.page.locator(".composer-input-region textarea").first();
    await input.fill(`MODEL_PERMISSION_QA:${key} owner=${state.owner}`); await input.press("Enter");
    await expect.poll(() => this.provider.requests.slice(before).some(request => request.main && request.caseKey === key), { timeout: 30_000 }).toBe(true);
    const request = this.provider.requests.slice(before).find(request => request.main && request.caseKey === key)!;
    expect(request.body).toMatchObject({ model, reasoning_effort: effort });
    await expect.poll(() => this.wire.slice(mark).some(frame => frame.type === "item.completed" && frame.conversation_id === state.owner
      && frame.item?.type === "agent_message" && frame.item.text === `MODEL_PERMISSION_COMPLETE:${key}`), { timeout: 30_000 }).toBe(true);
    await expect(this.page.getByText(`MODEL_PERMISSION_COMPLETE:${key}`, { exact: true })).toBeVisible();
    await expect.poll(() => this.state().then(state => state.streaming)).toBe(false);
    await expect(this.page.getByText(/已安排压缩上下文/)).toHaveCount(0);
    return request;
  }
  async permissions() { await this.page.getByRole("button", { name: "更改权限", exact: true }).click(); }
  async fullAccess(accept: boolean, screenshot?: string) {
    const before = await this.state(), mark = this.wire.length;
    await this.permissions(); await this.page.getByRole("option", { name: "完全访问", exact: true }).click();
    const dialog = this.page.getByRole("dialog", { name: "要开启完全访问吗？", exact: true });
    await expect(dialog.getByText("文件和文件夹", { exact: true })).toBeVisible();
    await expect(dialog.getByText("终端命令", { exact: true })).toBeVisible();
    await expect(dialog.getByText("互联网和已连接的应用", { exact: true })).toBeVisible();
    if (screenshot) await this.capture(screenshot);
    await dialog.getByRole("button", { name: accept ? "确认" : "取消", exact: true }).click();
    if (accept) { await this.semantic(mark, "conversation.permission_mode.set", "conversation.permission_mode.set"); await expect.poll(() => this.state().then(state => state.permission)).toBe("bypass"); }
    else { expect(this.wire.slice(mark).filter(frame => frame.outbound && frame.type === "conversation.permission_mode.set")).toEqual([]); expect((await this.state()).permission).toBe(before.permission); }
    await expect(this.page.locator(".composer-input-region textarea").first()).toHaveValue(before.draft);
  }
  async capture(name: string) {
    await this.page.evaluate(async () => {
      await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
      await Promise.allSettled(document.getAnimations().filter(animation => animation.playState === "running"
        && animation.effect?.getComputedTiming().iterations !== Infinity).map(animation => animation.finished));
    });
    const styles = await this.page.locator('.composer-model-select,.composer-model-option,.composer-menu-choice,.composer-permission-btn,.mc-dialog-panel').evaluateAll(elements => elements.map(element => {
      const css = getComputedStyle(element); return { className: element.className, label: element.getAttribute("aria-label"), text: element.textContent,
        font: css.fontFamily, fontSize: css.fontSize, color: css.color, background: css.backgroundColor, padding: css.padding, gap: css.gap,
        radius: css.borderRadius, disabled: element instanceof HTMLButtonElement ? element.disabled : null, rect: element.getBoundingClientRect().toJSON() };
    }));
    const png = await this.app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].capturePage()).toPNG().toString("base64"));
    await writeFile(path.join(this.evidence, name), Buffer.from(png, "base64"));
    await writeFile(path.join(this.evidence, name.replace(/\.png$/, ".json")), JSON.stringify({ phase: this.phase, selection: await this.state(), styles }, null, 2));
    this.screenshots.push(name);
  }
  async checkHidden() {
    const native = await this.app.evaluate(({ BrowserWindow }) => ({ visible: BrowserWindow.getAllWindows().map(window => window.isVisible()), qa: (globalThis as any).__offscreenQa }));
    expect(native.visible).toEqual([false]); this.native.push({ phase: this.phase, ...native });
  }
  async close() { if (!this.closed) { await this.checkHidden(); await this.app.close(); this.closed = true; } }
  async scenario(name: string, action: () => Promise<void>) { this.currentScenario = name; await action(); this.checks.push({ name, status: "passed", phase: this.phase }); }
  async save(scenarios: string[]) {
    await this.close(); await this.provider.close();
    const rawProfile = path.join(this.evidence, `raw-profile-${path.basename(this.profile)}`); await cp(this.profile, rawProfile, { recursive: true });
    const checks = scenarios.map(name => this.checks.find(check => check.name === name) || { name, status: name === this.currentScenario ? "failed" : "skipped" });
    await Promise.all([
      writeFile(path.join(this.evidence, "wire.json"), JSON.stringify(this.wire, null, 2)),
      writeFile(path.join(this.evidence, "provider-requests.json"), JSON.stringify(this.provider.requests, null, 2)),
      writeFile(path.join(this.evidence, "provider-responses.json"), JSON.stringify(this.provider.responses, null, 2)),
      writeFile(path.join(this.evidence, "repository-records.json"), JSON.stringify(this.records, null, 2)),
      writeFile(path.join(this.evidence, "desktop-output.json"), JSON.stringify(this.desktopOutput, null, 2)),
      writeFile(path.join(this.evidence, "renderer-warnings.json"), JSON.stringify(this.rendererWarnings, null, 2)),
      writeFile(path.join(this.evidence, "verdict.json"), JSON.stringify({ passed: this.passed, controlledProviderHttpOnly: true, profile: this.profile, workspace: this.workspace,
        rawProfile, checks, failedScenario: this.passed ? null : this.currentScenario, pageErrors: this.pageErrors, native: this.native, screenshots: this.screenshots }, null, 2)),
    ]);
  }
}

async function prepare(name: string, fixture: Fact): Promise<DesktopRun> {
  const profile = await mkdtemp(path.join(tmpdir(), `minicode-${name}-qa-`));
  const workspaceBase = path.join(path.parse(process.env.SystemRoot!).root, "Temp"); await mkdir(workspaceBase, { recursive: true });
  const workspace = await mkdtemp(path.join(workspaceBase, `minicode-${name}-work-`)), evidence = path.join(evidenceRoot, name);
  await mkdir(path.join(profile, "data"), { recursive: true }); await mkdir(evidence, { recursive: true });
  const provider = new ControlledProvider(fixture.llm.custom.available_models), url = await provider.start();
  fixture.llm.custom.base_url = url; fixture.llm.custom.wire_api = "chat"; fixture.llm.custom.api_key = "fixture-model-control";
  fixture.agent = { code_mode_only: true }; fixture.memory = { enabled: false };
  await writeFile(path.join(profile, "settings.json"), JSON.stringify(fixture, null, 2));
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", questionNotifications: false, turnCompletionNotifications: "never" }));
  await writeFile(path.join(profile, "data/active_workspace.json"), JSON.stringify({ root: workspace }));
  await writeFile(path.join(profile, "data/trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] }));
  await writeFile(path.join(workspace, "model-control-fact.txt"), "Owned model-control fixture workspace\n");
  await writeFile(path.join(evidence, "input-settings.json"), JSON.stringify(fixture, null, 2));
  return new DesktopRun(profile, workspace, evidence, provider);
}

test("Model defaults, supported effort and Full access through the actual offscreen desktop", async () => {
  test.setTimeout(240_000);
  const scenarios = ["unsupported effort uses new model default", "supported effort is preserved", "reset restores project model/effort and selection mode",
    "Full access cancel/accept/reselect preserves draft", "conversation selection and draft ownership", "cold repository restore of both model pairs and modes"];
  const run = await prepare("model-defaults", JSON.parse(await readFile(path.join(audit, "model-control-provider-config.json"), "utf8")));
  let ownerA = "", ownerB = "";
  try {
    await run.launch(); ownerA = await run.firstConversation(); await run.selection(modelA, "medium");
    const canonicalWorkspace = (await run.state()).workspace;
    await expect.poll(() => run.state().then(state => state.defaults)).toMatchObject({ provider: "custom", model: modelA, reasoningEffort: "medium", conversationId: ownerA, workingDirectory: canonicalWorkspace });
    await run.capture("01-initial-model-a-medium.png");
    await run.scenario(scenarios[0], async () => {
      await run.chooseModel(modelB, "low", "02-model-list-project-default.png"); await run.capture("03-model-b-low-slider.png");
      const record = await run.record(ownerA, "unsupported-to-b-low"); expect(record.model_selection).toMatchObject({ model: modelB, reasoning_effort: "low", selection_mode: "model" });
      await run.probe("unsupported_warm", modelB, "low");
    });
    await run.scenario(scenarios[1], async () => {
      await run.chooseModel(modelA, "low"); await run.extremeEffort("End", "high"); await run.capture("04-model-a-high-slider.png");
      await run.chooseModel(modelB, "high", "05-supported-model-switch-list.png"); await run.capture("06-model-b-high-preserved.png");
      expect((await run.record(ownerA, "supported-b-high")).model_selection).toMatchObject({ model: modelB, reasoning_effort: "high", selection_mode: "model" });
      await run.probe("supported_warm", modelB, "high");
    });
    await run.scenario(scenarios[2], async () => {
      const mark = run.wire.length; await run.power(); await expect(run.page.getByRole("button", { name: "重置为默认", exact: true })).toBeVisible();
      await run.capture("07-reset-button-before-project-default.png"); await run.page.getByRole("button", { name: "重置为默认", exact: true }).click();
      expect(await run.semantic(mark, "llm.model.set", "model.set")).toMatchObject({ selection_mode: "default" }); await run.selection(modelA, "medium", "default");
      await run.capture("08-project-default-a-medium-slider.png"); await run.models();
      await expect(run.page.getByRole("option", { name: /默认/ })).toHaveAttribute("aria-selected", "true");
      await expect(run.page.getByText("推荐模型集", { exact: true })).toHaveCount(0); await run.capture("09-default-option-selected.png");
      await run.page.getByRole("listbox", { name: "选择模型", exact: true }).press("Escape");
      await run.probe("default_warm", modelA, "medium");
      expect((await run.record(ownerA, "project-default-persisted")).model_selection).toMatchObject({ model: modelA, reasoning_effort: "medium", selection_mode: "default" });
      expect(JSON.parse(await readFile(path.join(run.profile, "settings.json"), "utf8")).llm.custom).toMatchObject({ model: modelA, reasoning_effort: "medium" });
    });
    await run.scenario(scenarios[3], async () => {
      await run.page.locator(".composer-input-region textarea").first().fill("UNSENT_PERMISSION_DRAFT"); await run.permissions();
      await expect(run.page.getByRole("option", { name: "请求批准", exact: true })).toBeVisible();
      await run.capture("10-default-permission-menu.png"); await run.page.getByRole("option", { name: "请求批准", exact: true }).press("Escape");
      await run.fullAccess(false, "11-full-access-cancel-dialog.png"); await run.fullAccess(true, "12-full-access-accept-dialog.png");
      await run.permissions(); await run.capture("13-current-full-access-menu.png"); await run.page.getByRole("option", { name: "完全访问", exact: true }).press("Escape");
      await run.fullAccess(false, "14-current-full-access-reconfirm-dialog.png");
      const expectedDefault = (await run.state()).allowedPermissions.find((mode: string) => mode !== "bypass"), mark = run.wire.length;
      await run.permissions(); await run.page.getByRole("option", { name: "请求批准", exact: true }).click();
      await run.semantic(mark, "conversation.permission_mode.set", "conversation.permission_mode.set"); await expect.poll(() => run.state().then(state => state.permission)).toBe(expectedDefault);
      await expect(run.page.locator(".composer-input-region textarea").first()).toHaveValue("UNSENT_PERMISSION_DRAFT"); await run.capture("15-default-permission-draft-restored.png");
    });
    await run.scenario(scenarios[4], async () => {
      await run.rename("MODEL_QA_OWNER_A"); await run.page.locator(".composer-input-region textarea").first().fill("UNSENT_MODEL_OWNER_A");
      const mark = run.wire.length; await run.page.getByRole("button", { name: /^在 .* 中新建任务$/ }).first().click();
      await run.semantic(mark, "conversation.create", "conversation.create"); await expect.poll(() => run.state().then(state => state.owner)).not.toBe(ownerA); ownerB = (await run.state()).owner;
      await run.selection(modelA, "medium"); await run.chooseModel(modelB, "low"); await run.extremeEffort("End", "high"); await run.probe("owner_b_warm", modelB, "high");
      await run.rename("MODEL_QA_OWNER_B"); await run.page.locator(".composer-input-region textarea").first().fill("UNSENT_MODEL_OWNER_B"); await run.power(); await run.capture("16-second-owner-b-high.png");
      await run.switchConversation("MODEL_QA_OWNER_A", ownerA); await run.selection(modelA, "medium", "default");
      await expect(run.page.locator(".composer-input-region textarea").first()).toHaveValue("UNSENT_MODEL_OWNER_A"); await run.capture("17-first-owner-default-draft.png");
      await run.switchConversation("MODEL_QA_OWNER_B", ownerB); await run.selection(modelB, "high", "model");
      await expect(run.page.locator(".composer-input-region textarea").first()).toHaveValue("UNSENT_MODEL_OWNER_B");
      await run.record(ownerA, "warm-owner-a-final"); await run.record(ownerB, "warm-owner-b-final");
    });
    await run.scenario(scenarios[5], async () => {
      await run.close(); run.phase = "cold"; await run.launch();
      await run.switchConversation("MODEL_QA_OWNER_A", ownerA); await run.selection(modelA, "medium", "default");
      await expect.poll(() => run.state().then(state => state.defaults.conversationId)).toBe(ownerA);
      await run.power(); await expect(run.page.getByRole("button", { name: "重置为默认", exact: true })).toHaveCount(0); await run.capture("18-cold-owner-a-project-default.png");
      expect((await run.record(ownerA, "cold-owner-a-restored")).model_selection).toMatchObject({ model: modelA, reasoning_effort: "medium", selection_mode: "default" });
      await run.probe("owner_a_cold", modelA, "medium");
      await run.switchConversation("MODEL_QA_OWNER_B", ownerB); await run.selection(modelB, "high", "model");
      await expect.poll(() => run.state().then(state => state.defaults.conversationId)).toBe(ownerB); await run.power(); await run.capture("19-cold-owner-b-explicit-high.png");
      expect((await run.record(ownerB, "cold-owner-b-restored")).model_selection).toMatchObject({ model: modelB, reasoning_effort: "high", selection_mode: "model" });
      await run.probe("owner_b_cold", modelB, "high");
    });
    expect(run.pageErrors).toEqual([]); run.passed = true;
  } finally { await run.save(scenarios); }
});

test("Ultra and Full access have separate Continue, Full access and close choices in an isolated profile", async () => {
  test.setTimeout(150_000);
  const scenarios = ["closing Ultra confirmation does not change effort or permission", "Continue applies a real restricted mode before Ultra", "Use Full access keeps bypass and applies Ultra"];
  const fixture = JSON.parse(await readFile(path.join(audit, "model-control-provider-config.json"), "utf8")), model = "audit-ultra-model";
  fixture.llm.custom.model = model; fixture.llm.custom.reasoning_effort = "high"; fixture.llm.custom.available_models = [model];
  fixture.llm.custom.model_metadata = { [model]: { reasoning_effort_levels: ["high", "ultra"], default_reasoning_effort: "high", context_window: 65536, max_output_tokens: 4096 } };
  const run = await prepare("ultra-permissions", fixture);
  try {
    await run.launch(); const owner = await run.firstConversation(); await run.selection(model, "high");
    await run.page.locator(".composer-input-region textarea").first().fill("UNSENT_ULTRA_PERMISSION_DRAFT"); await run.fullAccess(true);
    const dialog = () => run.page.getByRole("dialog", { name: "要将 Ultra 与完全访问搭配使用吗？", exact: true });
    await run.scenario(scenarios[0], async () => {
      const mark = run.wire.length; await (await run.power()).press("End"); await expect(dialog()).toBeVisible(); await run.capture("01-ultra-full-access-close-dialog.png");
      await dialog().press("Escape"); await expect(dialog()).toHaveCount(0); await run.selection(model, "high");
      expect((await run.state()).permission).toBe("bypass");
      expect(run.wire.slice(mark).filter(frame => frame.outbound && ["llm.config.set", "conversation.permission_mode.set"].includes(frame.type))).toEqual([]);
      await expect(run.page.locator(".composer-input-region textarea").first()).toHaveValue("UNSENT_ULTRA_PERMISSION_DRAFT");
    });
    await run.scenario(scenarios[1], async () => {
      const mark = run.wire.length, expectedDefault = (await run.state()).allowedPermissions.find((mode: string) => mode !== "bypass");
      await (await run.power()).press("End"); await expect(dialog().getByRole("button", { name: "继续", exact: true })).toBeEnabled();
      await run.capture("02-ultra-continue-restricted-dialog.png"); await dialog().getByRole("button", { name: "继续", exact: true }).click();
      await run.semantic(mark, "conversation.permission_mode.set", "conversation.permission_mode.set"); await run.semantic(mark, "llm.config.set", "effort"); await run.selection(model, "ultra", "model");
      expect((await run.state()).permission).toBe(expectedDefault);
      expect(run.wire.slice(mark).filter(frame => frame.outbound && ["conversation.permission_mode.set", "llm.config.set"].includes(frame.type)).map(frame => frame.type))
        .toEqual(["conversation.permission_mode.set", "llm.config.set"]);
      await expect(run.page.locator(".composer-input-region textarea").first()).toHaveValue("UNSENT_ULTRA_PERMISSION_DRAFT"); await run.capture("03-ultra-with-restricted-permission.png");
      expect((await run.record(owner, "restricted-ultra")).model_selection).toMatchObject({ model, reasoning_effort: "ultra", selection_mode: "model" });
    });
    await run.scenario(scenarios[2], async () => {
      await run.extremeEffort("Home", "high"); await run.fullAccess(true);
      const mark = run.wire.length; await (await run.power()).press("End"); await expect(dialog()).toBeVisible(); await run.capture("04-ultra-use-full-access-dialog.png");
      await dialog().getByRole("button", { name: "使用完全访问", exact: true }).click(); await run.semantic(mark, "llm.config.set", "effort"); await run.selection(model, "ultra", "model");
      expect((await run.state()).permission).toBe("bypass"); expect(run.wire.slice(mark).filter(frame => frame.outbound && frame.type === "conversation.permission_mode.set")).toEqual([]);
      await expect(run.page.locator(".composer-input-region textarea").first()).toHaveValue("UNSENT_ULTRA_PERMISSION_DRAFT"); await run.capture("05-ultra-with-full-access-draft.png");
      await run.probe("ultra_full_access", model, "ultra"); await run.record(owner, "full-access-ultra");
    });
    expect(run.pageErrors).toEqual([]); run.passed = true;
  } finally { await run.save(scenarios); }
});
