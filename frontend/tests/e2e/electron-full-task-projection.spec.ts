import { _electron as electron, expect, test } from "@playwright/test";
import { createWriteStream } from "node:fs";
import { cp, mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import { createServer } from "node:net";
import { createRequire } from "node:module";
import { createHash, randomBytes } from "node:crypto";
import { promisify } from "node:util";
import path from "node:path";
import { pathToFileURL } from "node:url";

const root = path.resolve(import.meta.dirname, "../../..");
const out = process.env.MINICODE_FULL_TASK_OUT!;
const apiKey = process.env.MINICODE_REAL_E2E_API_KEY!;
const model = process.env.MINICODE_REAL_E2E_MODEL!;
const timeout = Number(process.env.MINICODE_FULL_TASK_TIMEOUT_MS ?? "900000");
const runFile = promisify(execFile);

const verifier = `import csv, io, json, random, unittest
from decimal import Decimal
from pathlib import Path
import ledger, reader
class Oracle(unittest.TestCase):
    def test_exact_first_global(self):
        rows=[{'id':'x','account':'a','amount':'0.10'},{'id':'x','account':'b','amount':'999'}, {'id':'y','account':'a','amount':'0.20'}]
        self.assertEqual(ledger.reconcile(rows), {'a':'0.30'})
    def test_zero_and_negative(self):
        self.assertEqual(ledger.reconcile([{'id':'a','account':'zero','amount':'4.10'}, {'id':'b','account':'zero','amount':'-4.10'}, {'id':'c','account':'debt','amount':'-2'}]), {'debt':'-2.00','zero':'0.00'})
    def test_empty(self): self.assertEqual(ledger.reconcile([]), {})
    def test_csv(self):
        text='\\ufeffid,account,amount\\n" x ","cash, reserve",0.10\\ny,"cash, reserve",-0.10\\n'
        self.assertEqual(reader.load_rows(io.StringIO(text)), [{'id':'x','account':'cash, reserve','amount':'0.10'}, {'id':'y','account':'cash, reserve','amount':'-0.10'}])
    def test_random_and_input_ownership(self):
        rng=random.Random(281024)
        for _ in range(25):
            rows=[{'id':str(rng.randrange(200)), 'account':'acct-'+str(rng.randrange(5)), 'amount':str(Decimal(rng.randrange(-999,1000))/100)} for _ in range(500)]
            before=json.dumps(rows, sort_keys=True); first={}; expected={}
            for row in rows: first.setdefault(row['id'],row)
            for row in first.values(): expected[row['account']]=expected.get(row['account'],Decimal(0))+Decimal(row['amount'])
            expected={key:format(value,'.2f') for key,value in sorted(expected.items())}
            self.assertEqual(ledger.reconcile(rows), expected); self.assertEqual(json.dumps(rows,sort_keys=True),before)
    def test_result_artifact(self):
        rows=reader.load_rows(io.StringIO(Path('data/sample.csv').read_text(encoding='utf-8')))
        self.assertEqual(json.loads(Path('result.json').read_text()), ledger.reconcile(rows))
if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Oracle))
    Path('oracle-result.json').write_text(json.dumps({'passed':result.wasSuccessful(),'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors)}))
    raise SystemExit(0 if result.wasSuccessful() else 1)
`;

async function freePort() {
  const server = createServer();
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as any).port;
  await new Promise<void>(resolve => server.close(() => resolve()));
  return port;
}

test.describe("manual real-provider full-task projection", () => {
  test.skip(!out || !apiKey || !model || !process.env.MINICODE_REAL_E2E_BASE_URL || !process.env.MINICODE_REAL_E2E_WIRE_API,
    "Set MINICODE_FULL_TASK_OUT and the configured MINICODE_REAL_E2E_API_KEY, BASE_URL, MODEL, and WIRE_API inputs to run this manual real-provider test.");

test("real task projects delegation, file work, navigation and renderer recovery", async () => {
  test.setTimeout(timeout + 90_000);
  const state = path.join(out, "state"), configDir = path.join(out, "config"), workspace = path.join(out, "workspace");
  await mkdir(path.join(state, "data"), { recursive: true });
  await mkdir(configDir, { recursive: true });
  await mkdir(path.join(workspace, "data"), { recursive: true });
  const frontendDist = path.join(out, "frontend-dist");
  await cp(path.join(root,"frontend","dist"),frontendDist,{recursive:true});
  await writeFile(path.join(out,"frontend-artifact.json"),JSON.stringify({reportedBaseRevision:process.env.MINICODE_FULL_TASK_BASE_REVISION,source:path.join(root,"frontend","dist"),frozenCopy:frontendDist,indexModifiedAt:(await stat(path.join(root,"frontend","dist","index.html"))).mtime.toISOString(),indexSha256:createHash("sha256").update(await readFile(path.join(frontendDist,"index.html"))).digest("hex"),hmr:false},null,2));
  const facts = [0, 1].map(index => `CHILD_${index + 1}_${randomBytes(9).toString("hex")}`);
  const finalMarker = `FULL_TASK_OK_${randomBytes(6).toString("hex")}`;
  const draft = `ISOLATED_SIDE_DRAFT_${randomBytes(8).toString("hex")}`;
  const initialLedger = "def reconcile(rows):\n    total = {}\n    for row in rows:\n        total[row['account']] = total.get(row['account'], 0.0) + float(row['amount'])\n    return {key: str(value) for key, value in total.items()}\n";
  const initialReader = "def load_rows(stream):\n    lines = stream.read().strip().splitlines()\n    return [dict(zip(lines[0].split(','), line.split(','))) for line in lines[1:]]\n";
  await Promise.all([
    writeFile(path.join(workspace, "ledger.py"), initialLedger),
    writeFile(path.join(workspace, "reader.py"), initialReader),
    writeFile(path.join(workspace, "verify.py"), verifier),
    writeFile(path.join(workspace, "data", "sample.csv"), 'id,account,amount\nx,"cash, reserve",0.10\nx,wrong,700\ny,"cash, reserve",0.20\nz,debt,-2\n'),
    writeFile(path.join(workspace, "data", "child-one.md"), `${facts[0]}\nThe first occurrence of an event id wins globally across all accounts. Include zero balances and use Decimal.\n`),
    writeFile(path.join(workspace, "data", "child-two.md"), `${facts[1]}\nRead CSV with BOM support and quoted fields. Strip surrounding whitespace in individual values, preserving embedded commas.\n`),
    writeFile(path.join(workspace, "REQUIREMENTS.md"), "Repair ledger.py and reader.py using only the Python standard library. Reconcile amounts exactly with Decimal, use first global event id, format every amount with exactly two decimals, include zero totals, sort account keys, and never mutate input rows. Parse CSV with BOM/quoted-comma support and trim fields. Generate result.json from data/sample.csv. verify.py is the independent oracle and must remain unchanged.\n"),
    writeFile(path.join(state, "data", "active_workspace.json"), JSON.stringify({ root: workspace })),
    writeFile(path.join(state, "data", "trusted_workspaces.json"), JSON.stringify({ version: 1, roots: [workspace] })),
    writeFile(path.join(state, "desktop-preferences.json"), JSON.stringify({ defaultPermissionMode: "auto", defaultTaskDirectory: path.join(out, "projectless"), confirmBeforeClose: "never", turnCompletionNotifications: "never", permissionNotifications: false, questionNotifications: false, localeOverride: "zh-CN" })),
    writeFile(path.join(state, "settings.json"), JSON.stringify({ agent: { code_mode_only: false, max_iterations: 50, max_turn_seconds: Math.floor(timeout / 1000) }, tools: { update_plan: { enabled: true } }, features: { memories: false, multi_agent: true } })),
  ]);
  const prompt = [
    "Complete the isolated coding task in this workspace, with real tools and real delegated work.",
    "First read REQUIREMENTS.md and the existing code. Use exactly two real child agents through the provided delegation schema (task or spawn_agent as offered), independently and concurrently.",
    "Child one must read data/child-one.md and review ledger.py. Child two must read data/child-two.md and review reader.py. Each child must send its exact CHILD marker plus a concrete finding to the parent using the actual internal send_message tool BEFORE returning its review. Children are read-only. Parent: do not read either child note directly; integrate their real messages.",
    "Then repair both modules, produce result.json by actually running the corrected implementation on data/sample.csv, and run python verify.py. You may iterate on genuine failures. Do not edit verify.py or either child note. Do not add sleeps, timers, dummy work, or fabricated statuses. Work only inside this workspace. Do not use Git.",
    `After successful verification, answer with both exact CHILD markers, the actual files changed and test result, and final marker ${finalMarker}.`,
  ].join("\n");
  await writeFile(path.join(out, "prompt.md"), prompt);
  const shim = path.join(out, "background-entry.cjs");
  await writeFile(shim, `const {app,BrowserWindow}=require('electron');\napp.commandLine.appendSwitch('disable-renderer-backgrounding');\nBrowserWindow.prototype.show=function(){this.showInactive()};\nBrowserWindow.prototype.focus=function(){};\nrequire(${JSON.stringify(path.join(root, "desktop", "main.js"))});\n`);
  const backendPort = await freePort();
  const { ELECTRON_RUN_AS_NODE: _, ...env } = process.env;
  const app = await electron.launch({ executablePath: createRequire(path.join(root, "desktop", "package.json"))("electron") as string,
    args: [shim], cwd: path.join(root, "desktop"), env: { ...env,
      MINICODE_FRONTEND_URL: pathToFileURL(path.join(frontendDist,"index.html")).href,
      MINICODE_USER_DATA_DIR: state, MINICODE_CONFIG_DIR: configDir, MINICODE_BACKEND_PORT: String(backendPort),
      CUSTOM_API_KEY: apiKey, CUSTOM_BASE_URL: process.env.MINICODE_REAL_E2E_BASE_URL!, CUSTOM_MODEL: model,
      CUSTOM_WIRE_API: process.env.MINICODE_REAL_E2E_WIRE_API!, CUSTOM_REASONING_EFFORT: process.env.MINICODE_REAL_E2E_REASONING_EFFORT ?? "",
      LLM_PROVIDER: "custom", OPENAI_API_KEY: "", ANTHROPIC_API_KEY: "", CUSTOM_SMALL_FAST_MODEL: "",
      CUSTOM_RESPONSES_REASONING_SUMMARY: "off", CUSTOM_MAX_TOKENS: "8192", PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8",
    } });
  const frames: any[] = [], milestones: any[] = [], pageErrors: string[] = [];
  const wire = createWriteStream(path.join(out, "wire.jsonl"));
  let page: Awaited<ReturnType<typeof app.firstWindow>> | undefined;
  let mainId = "", sideId = "", passed = false;
  const started = Date.now();
  const record = (name: string, data: any = {}) => { milestones.push({ name, elapsedMs: Date.now() - started, ...data }); console.log(name); };
  const snapshot = async (name: string) => {
    await page!.screenshot({path:path.join(out, `${name}.png`),animations:"disabled"});
    const data = await page!.evaluate(() => {
      const s = (window as any).__zustandStore.getState();
      return { conversationId: s.conversationId, pendingConversationSwitchId: s.pendingConversationSwitchId,
        isStreaming: s.isStreaming, conversationStreaming: s.conversationStreaming, messages: s.messages,
        subagents: s.subagents, draft: s.draft, body: document.body.innerText,
        appMode:s.appMode,workbenchLayout:s.workbenchLayout,panelSlots:s.panelSlots,
        inputs:[...document.querySelectorAll('textarea,[contenteditable],input,[role="textbox"]')].map(el=>({tag:el.tagName,role:el.getAttribute('role'),label:el.getAttribute('aria-label'),className:el.className})),
        process: [...document.querySelectorAll('.chat-turn-process')].map(el => ({ collapsed: el.getAttribute('data-collapsed'), text: el.textContent })),
        collaboration: [...document.querySelectorAll('.collaboration-cell')].map(el => ({ status: el.getAttribute('data-status'), text: el.textContent })) };
    });
    await writeFile(path.join(out, `${name}.json`), JSON.stringify(data, null, 2).replaceAll(apiKey, "[redacted]"));
    return data;
  };
  try {
    page = await app.firstWindow();
    page.on("pageerror", error => pageErrors.push(error.message));
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Network.enable");
    for (const [event, direction] of [["Network.webSocketFrameReceived", "in"], ["Network.webSocketFrameSent", "out"]] as const) {
      cdp.on(event, (item: any) => {
        if (item.response.opcode !== 1) return;
        const payload = JSON.parse(item.response.payloadData);
        const row = { direction, at: Date.now(), payload };
        frames.push(row); wire.write(JSON.stringify(row).replaceAll(apiKey, "[redacted]") + "\n");
      });
    }
    await expect.poll(() => page!.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)), { timeout: 90_000 }).toBe(true);
    expect(await app.evaluate(({ app }) => app.getPath("userData"))).toBe(state);
    const create = async () => {
      await page!.evaluate(async workspace => { const s=(window as any).__zustandStore.getState(); s.setWorkingDirectory(workspace); await s.createConversation({workspaceRoot:workspace,bindWorkspace:true,appMode:"cowork"}); }, workspace);
      return await page!.evaluate(() => (window as any).__zustandStore.getState().conversationId as string);
    };
    sideId = await create();
    const composer = page.getByRole("textbox", {name:"消息输入",exact:true});
    await composer.fill(draft);
    mainId = await create();
    await expect(composer).toBeVisible();
    const layout = await page.evaluate(() => {
      const selectors = ['.mc-navigation-rail','.mc-sidebar-left','.mc-header','.mc-titlebar-menus','.chat-pane','.chat-pane-main','.chat-pane-composer-region','.mc-sidebar-right','.mc-sidebar-right-header','header','textarea','[role="menu"]'];
      return { devicePixelRatio, innerWidth, innerHeight, bodyFont:getComputedStyle(document.body).fontFamily,
        elements:selectors.flatMap(selector=>[...document.querySelectorAll<HTMLElement>(selector)].map(el=>{
          const s=getComputedStyle(el), r=el.getBoundingClientRect();
          return {selector,className:el.className,tag:el.tagName,rect:r.toJSON(),visible:Boolean(r.width&&r.height),fontFamily:s.fontFamily,fontSize:s.fontSize,lineHeight:s.lineHeight,letterSpacing:s.letterSpacing};
        })) };
    });
    const nativeLayout = await app.evaluate(({BrowserWindow,app})=>{const w=BrowserWindow.getAllWindows()[0];return {zoomFactor:w.webContents.getZoomFactor(),bounds:w.getBounds(),focused:w.isFocused(),userData:app.getPath("userData"),sessionData:app.getPath("sessionData")};});
    await writeFile(path.join(out,"layout-baseline.json"),JSON.stringify({...layout,native:nativeLayout},null,2));
    await snapshot("00-initial");
    record("ready", { mainId, sideId, model });
    await composer.fill(prompt); await composer.press("Enter");
    await expect.poll(() => page!.evaluate(() => (window as any).__zustandStore.getState().isStreaming), { timeout: 60_000 }).toBe(true);
    await expect.poll(() => frames.some(row => row.direction === "in" && row.payload.type === "tool_call" && row.payload.conversation_id === mainId), { timeout: 150_000 }).toBe(true);
    record("real-tool-work-started");
    await snapshot("01-running");
    await page.evaluate(id => (window as any).__zustandStore.getState().requestConversationSwitch(id), sideId);
    await expect.poll(() => page!.evaluate(id => {const s=(window as any).__zustandStore.getState();return s.conversationId===id&&!s.pendingConversationSwitchId;},sideId)).toBe(true);
    const side = await snapshot("02-side-while-main-runs");
    expect(side.draft).toBe(draft); expect(side.isStreaming).toBe(false); expect(side.conversationStreaming[mainId]).toBe(true);
    record("switched-to-side-with-main-running");
    await page.evaluate(id => (window as any).__zustandStore.getState().requestConversationSwitch(id), mainId);
    await expect.poll(() => page!.evaluate(id => {const s=(window as any).__zustandStore.getState();return s.conversationId===id&&!s.pendingConversationSwitchId;},mainId)).toBe(true);
    await expect.poll(() => frames.some(row => row.direction === "in" && row.payload.type === "subagent.start" && row.payload.conversation_id === mainId), { timeout: 180_000 }).toBe(true);
    await snapshot("03-child-running-before-reload");
    expect(await page.evaluate(() => (window as any).__zustandStore.getState().isStreaming)).toBe(true);
    const beforeReload = frames.length;
    await page.reload();
    await expect.poll(() => page!.evaluate(id => { const s=(window as any).__zustandStore?.getState(); return Boolean(s?.isConnected && s.conversationId === id && !s.pendingConversationSwitchId); }, mainId), { timeout: 90_000 }).toBe(true);
    await snapshot("04-restored"); record("renderer-reloaded-and-owner-restored", { beforeReload });
    const deadline = started + timeout;
    while (Date.now() < deadline) {
      const pending = await page.evaluate(() => {
        const buttons = [...document.querySelectorAll<HTMLButtonElement>("button")];
        const button = buttons.find(el => ["允许文件更改", "允许使用工具"].includes(el.getAttribute("aria-label") ?? "") && !el.disabled && el.getClientRects().length);
        if (button) { button.click(); return "approved"; }
        const s=(window as any).__zustandStore.getState();
        const last=[...s.messages].reverse().find((message:any)=>message.role==='assistant');
        return !s.isStreaming && last?.terminalStatus && !last.isStreaming ? "terminal" : "running";
      });
      if (pending === "terminal") break;
      await page.waitForTimeout(300);
    }
    const final = await snapshot("05-final-collapsed");
    expect(final.isStreaming).toBe(false);
    expect(final.messages.filter((message:any)=>message.role==="assistant").at(-1).terminalStatus).toBe("completed");
    const finalText = final.messages.filter((m:any)=>m.role==="assistant").map((m:any)=>[m.content ?? "", ...(m.blocks ?? []).filter((b:any)=>b.type==="text").map((b:any)=>b.content ?? "")].join("\n")).join("\n");
    expect(finalText).toContain(finalMarker); for (const fact of facts) expect(finalText).toContain(fact);
    expect(final.process.length).toBeGreaterThan(0); expect(final.process.every((p:any)=>p.collapsed === "true")).toBe(true);
    expect(final.subagents).toHaveLength(2);
    expect(final.subagents.every((agent:any)=>agent.status==="done" || agent.status==="completed")).toBe(true);
    const sourceCalls = frames.filter(row=>row.direction==="in" && row.payload.type==="tool_call").map(row=>row.payload);
    const sourceResults = frames.filter(row=>row.direction==="in" && row.payload.type==="tool_result").map(row=>row.payload);
    expect(sourceCalls.some(call=>["task","spawn_agent"].includes(call.name))).toBe(true);
    expect(sourceCalls.some(call=>["run_command","tool_exec"].includes(call.name))).toBe(true);
    const ledger = await readFile(path.join(workspace,"ledger.py"),"utf8"), reader = await readFile(path.join(workspace,"reader.py"),"utf8");
    expect(ledger).not.toBe(initialLedger); expect(reader).not.toBe(initialReader); expect(await readFile(path.join(workspace,"verify.py"),"utf8")).toBe(verifier);
    expect(await readFile(path.join(workspace,"data","child-one.md"),"utf8")).toBe(`${facts[0]}\nThe first occurrence of an event id wins globally across all accounts. Include zero balances and use Decimal.\n`);
    expect(await readFile(path.join(workspace,"data","child-two.md"),"utf8")).toBe(`${facts[1]}\nRead CSV with BOM support and quoted fields. Strip surrounding whitespace in individual values, preserving embedded commas.\n`);
    const verification = await runFile("python", ["verify.py"], {cwd:workspace, windowsHide:true});
    await writeFile(path.join(out,"oracle.log"), verification.stdout + verification.stderr);
    expect(JSON.parse(await readFile(path.join(workspace,"oracle-result.json"),"utf8"))).toMatchObject({passed:true,tests:6,failures:0,errors:0});
    record("real-task-final-and-independent-oracle-passed", { rawToolCallFrames:sourceCalls.length, rawToolResultFrames:sourceResults.length,
      canonicalToolCallCount:final.messages.flatMap((message:any)=>message.blocks??[]).filter((block:any)=>block.type==="tool_call").length });
    await page.getByRole("button", {name:"展开处理步骤",exact:true}).first().evaluate((el:HTMLButtonElement)=>el.click());
    await snapshot("06-expanded-process");
    const childTexts: string[] = [];
    for (let index=0; index<final.subagents.length; index++) {
      const source = final.subagents[index];
      const open = page.getByRole("button", {name:`打开子智能体：${source.displayName}`,exact:true}).first();
      await expect(open).toBeVisible();
      await open.evaluate((el:HTMLButtonElement)=>el.click());
      const child = page.locator('[aria-label="子智能体工作记录"]'); await expect(child).toBeVisible({timeout:30_000});
      await expect(child.locator(".chat-turn").first()).toBeVisible();
      expect(await child.locator('textarea,[role="textbox"]').count()).toBe(0);
      childTexts.push(await child.innerText());
      const childExpand = child.getByRole("button", {name:"展开处理步骤",exact:true});
      if (await childExpand.count()) await childExpand.first().evaluate((el:HTMLButtonElement)=>el.click());
      const sentMessage = child.locator('.collaboration-cell[data-action="sent_message"]').first();
      await expect(sentMessage.locator('.collaboration-parent-message')).toHaveText("已向父代理发送消息");
      await expect(sentMessage).toBeVisible();
      await snapshot(`07-child-record-${index+1}`);
      await page.getByRole("button", {name:"返回子智能体列表",exact:true}).evaluate((el:HTMLButtonElement)=>el.click());
    }
    for (const fact of facts) expect(childTexts.some(text=>text.includes(fact))).toBe(true);
    await page.evaluate(id => (window as any).__zustandStore.getState().requestConversationSwitch(id), sideId);
    await expect.poll(() => page!.evaluate(id => {const s=(window as any).__zustandStore.getState();return s.conversationId===id&&!s.pendingConversationSwitchId;},sideId)).toBe(true);
    await expect(composer).toHaveText(draft);
    record("side-draft-retained-after-completion");
    expect(pageErrors).toEqual([]);
    passed = true;
  } finally {
    if (page && !page.isClosed()) {
      await snapshot("last-observed");
    }
    await writeFile(path.join(out,"result.json"), JSON.stringify({passed, model, mainId, sideId, facts, finalMarker, milestones, pageErrors, elapsedSeconds:(Date.now()-started)/1000, isolation:{state,configDir,workspace}, eventCount:frames.length},null,2));
    await new Promise<void>(resolve=>wire.end(resolve));
    await app.close();
  }
});
});
