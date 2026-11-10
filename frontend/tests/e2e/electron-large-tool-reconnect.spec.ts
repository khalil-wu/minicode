/** Real command, code VM, backend transport and recovery; only model replies are controlled. */
import { _electron, expect, test, type ElectronApplication } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { createServer } from "node:http";
import { createServer as createPortServer } from "node:net";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "../../..");
const model = "large-output-loopback";
const first = "COMMAND_OUTPUT_BEGIN";
const last = "COMMAND_OUTPUT_END";
const final = "LARGE_OUTPUT_RECOVERY_COMPLETE";

test("large command output stays connected and survives one real reconnect without rerunning the command", async () => {
  test.setTimeout(150_000);
  const temporaryRoot = path.resolve(tmpdir());
  const owned = await mkdtemp(path.join(temporaryRoot, "minicode-large-output-"));
  const profile = path.join(owned, "profile"), workspace = path.join(owned, "workspace");
  await mkdir(path.join(profile, "data"), { recursive: true });
  await mkdir(path.join(profile, ".minicode"));
  await mkdir(workspace);
  const python = execFileSync("python", ["-c", "import sys; print(sys.executable)"], { encoding: "utf8" }).trim();
  await writeFile(path.join(workspace, "emit.py"), [
    "from pathlib import Path", "import sys", "sys.stdout.reconfigure(encoding='utf-8')",
    "counter=Path('invocations.txt')", "counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')",
    `print('${first}')`, "print('comment content 中文🙂 ' * 4500)", `print('${last}')`,
  ].join("\n"));
  const command = process.platform === "win32"
    ? `& '${python.replaceAll("'", "''")}' emit.py`
    : `'${python.replaceAll("'", "'\\''")}' emit.py`;
  let releaseFinal!: () => void;
  const finalGate = new Promise<void>(resolve => { releaseFinal = resolve; });
  let taskRequests = 0;
  const provider = createServer(async (request, response) => {
    if (request.method === "GET") {
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify({ data: [{ id: model, object: "model" }] }));
      return;
    }
    const chunks: Buffer[] = []; for await (const chunk of request) chunks.push(Buffer.from(chunk));
    const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    const task = body.tools?.length && JSON.stringify(body.messages).includes("LARGE_TOOL_RECONNECT_TEST");
    response.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
    const send = (delta: unknown, reason: string | null = null) => response.write(`data: ${JSON.stringify({
      id: "large-output", object: "chat.completion.chunk", model, choices: [{ index: 0, delta, finish_reason: reason }],
    })}\n\n`);
    send({ role: "assistant" });
    if (task && taskRequests++ === 0) {
      send({ tool_calls: [{ index: 0, id: "large-command-cell", type: "function", function: {
        name: "tool_exec", arguments: JSON.stringify({ code: `text(await tools.run_command(${JSON.stringify({
          command, login: false, yield_time_ms: 10000, max_chars: 120000,
        })}));`, yield_time_ms: 10000, max_output_tokens: 1000 }),
      } }] });
      send({}, "tool_calls");
    } else {
      if (task) await finalGate;
      send({ content: task ? final : "Large output test" });
      send({}, "stop");
    }
    response.end("data: [DONE]\n\n");
  });
  await new Promise<void>(resolve => provider.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${(provider.address() as { port: number }).port}/v1`;
  const portSocket = createPortServer();
  await new Promise<void>(resolve => portSocket.listen(0, "127.0.0.1", resolve));
  const backendPort = (portSocket.address() as { port: number }).port;
  await new Promise<void>(resolve => portSocket.close(() => resolve()));
  await writeFile(path.join(profile, "settings.json"), JSON.stringify({ agent: { code_mode_only: true }, memory: { enabled: false } }));
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({ localeOverride: "zh-CN", turnCompletionNotifications: "never" }));
  await writeFile(path.join(profile, "data/trusted_workspaces.json"), JSON.stringify({ roots: [workspace] }));
  await writeFile(path.join(profile, ".minicode/models.json"), JSON.stringify({ providers: { custom: {
    api: "openai-completions", base_url: url, api_key: "loopback-fixture", models: [{ id: model, name: model, context_window: 500000, max_tokens: 4096 }],
  } } }));
  const { ELECTRON_RUN_AS_NODE: _ignored, ...environment } = process.env;
  let app: ElectronApplication | undefined;
  try {
    app = await _electron.launch({ executablePath: createRequire(path.join(root, "desktop/package.json"))("electron"),
      args: [path.join(root, "desktop/main.js")], cwd: path.join(root, "desktop"), env: { ...environment,
        MINICODE_USER_DATA_DIR: profile, MINICODE_STATE_ROOT: profile, MINICODE_CONFIG_DIR: profile,
        MINICODE_BACKEND_PORT: String(backendPort), MINICODE_FRONTEND_URL: "",
        MINICODE_FRONTEND_DIST_FILE: path.join(root, "frontend/.tmp/desktop-release/index.html"),
        MINICODE_SYSTEM_CONFIG_FILE: path.join(profile, "absent.toml"), MINICODE_REQUIREMENTS_FILE: path.join(profile, "requirements.toml"),
        MINICODE_MANAGED_SETTINGS_DIR: path.join(profile, "managed"), LLM_PROVIDER: "custom", CUSTOM_BASE_URL: url,
        CUSTOM_MODEL: model, CUSTOM_API_KEY: "loopback-fixture", CUSTOM_WIRE_API: "chat", CUSTOM_REASONING_EFFORT: "",
        CUSTOM_SMALL_FAST_MODEL: "", OPENAI_API_KEY: "", ANTHROPIC_API_KEY: "", PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8",
      } });
    const page = await app.firstWindow();
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60000 }).toBe(true);
    await page.context().addInitScript(() => {
      const NativeSocket = window.WebSocket;
      (window as any).__reconnectTestSockets = [];
      window.WebSocket = class extends NativeSocket {
        constructor(url: string | URL, protocols?: string | string[]) {
          super(url, protocols); (window as any).__reconnectTestSockets.push(this);
        }
      };
    });
    await page.reload();
    const warnings: string[] = [], errors: string[] = [], incoming: any[] = [];
    page.on("console", message => { if (message.text().includes("[ws] Dropping")) warnings.push(message.text()); });
    page.on("pageerror", error => errors.push(error.message));
    const cdp = await page.context().newCDPSession(page); await cdp.send("Network.enable");
    cdp.on("Network.webSocketFrameReceived", ({ response }) => {
      if (response.opcode === 1) incoming.push(JSON.parse(response.payloadData));
    });
    await expect.poll(() => page.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 60000 }).toBe(true);
    await page.screenshot({ path: test.info().outputPath("current-build.png") });
    await page.evaluate(workspaceRoot => (window as any).__zustandStore.getState().createConversation({ bindWorkspace: true, workspaceRoot, appMode: "cowork" }), workspace);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().conversationId)).toBeTruthy();
    await page.evaluate(() => (window as any).__zustandStore.getState().setPermissionMode("bypass"));
    const composer = page.getByRole("textbox", { name: "消息输入", exact: true });
    await composer.fill("LARGE_TOOL_RECONNECT_TEST: execute the supplied command once and retain its output.");
    await composer.press("Enter");
    await expect.poll(() => incoming.some(event => event.type === "tool_result" && event.result_kind === "command" && event.summary?.includes(last)), { timeout: 45000 }).toBe(true);
    const result = incoming.find(event => event.type === "tool_result" && event.result_kind === "command" && event.summary?.includes(last));
    expect(result.summary.length).toBeGreaterThan(65536);
    expect(result.exit_code).toBe(0);
    expect(warnings).toEqual([]);
    const readRecord = () => page.evaluate(id => {
      const state = (window as any).__zustandStore.getState();
      return state.messages.flatMap((message: any) => message.blocks || [])
        .filter((block: any) => block.type === "tool_call").map((block: any) => block.record).find((tool: any) => tool.id === id);
    }, result.id);
    await expect.poll(async () => (await readRecord())?.summary).toContain(last);
    await composer.fill("KEEP_UNSENT_RECONNECT_DRAFT");
    const before = incoming.length;
    await page.evaluate(() => {
      const socket = (window as any).__reconnectTestSockets.findLast((item: WebSocket) => item.readyState === WebSocket.OPEN);
      socket.close(4002, "owned reconnect test");
    });
    await expect.poll(() => incoming.slice(before).some(event => event.type === "stream_resume" && event.tool_states?.some((tool: any) => tool.id === result.id)), { timeout: 30000 }).toBe(true);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().isConnected)).toBe(true);
    await expect.poll(async () => (await readRecord())?.summary).toContain(first);
    expect((await readRecord())?.status).toBe("success");
    expect(await composer.innerText()).toBe("KEEP_UNSENT_RECONNECT_DRAFT");
    expect(warnings).toEqual([]);
    releaseFinal();
    await expect(page.getByText(final, { exact: true })).toBeVisible({ timeout: 30000 });
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore.getState().isStreaming)).toBe(false);
    expect(await readFile(path.join(workspace, "invocations.txt"), "utf8")).toBe("1");
    await page.reload();
    await expect(page.getByText(final, { exact: true })).toBeVisible({ timeout: 30000 });
    await expect.poll(async () => (await readRecord())?.summary).toContain(first);
    expect((await readRecord())?.status).toBe("success");
    expect(await readFile(path.join(workspace, "invocations.txt"), "utf8")).toBe("1");
    const commandId = result.summary.match(/Background command (bg_[a-z0-9]+)/)?.[1];
    expect(commandId).toBeTruthy();
    const runtime = path.join(profile, "data/agent-runtime/background_tasks");
    const stored = (await readdir(runtime, { recursive: true })).filter(name => name.endsWith(`${commandId}.output`));
    expect(stored).toHaveLength(1);
    const completeOutput = await readFile(path.join(runtime, stored[0]), "utf8");
    expect(completeOutput).toContain(first); expect(completeOutput).toContain(last);
    expect(completeOutput.match(/comment content 中文🙂 /g)).toHaveLength(4500);
    expect(warnings).toEqual([]); expect(errors).toEqual([]);
    await page.screenshot({ path: test.info().outputPath("recovered-completed.png") });
  } finally {
    releaseFinal();
    await app?.close();
    provider.closeAllConnections();
    await new Promise<void>(resolve => provider.close(() => resolve()));
    const resolved = path.resolve(owned);
    if (path.dirname(resolved) !== temporaryRoot || !path.basename(resolved).startsWith("minicode-large-output-")) throw new Error("Unexpected test cleanup path");
    await rm(resolved, { recursive: true, force: true, maxRetries: 8, retryDelay: 150 });
  }
});
