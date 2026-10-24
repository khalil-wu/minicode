import { _electron as electron, expect, test } from "@playwright/test";
import { cp, mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { createServer } from "node:net";
import path from "node:path";
import { pathToFileURL } from "node:url";

const root = path.resolve(import.meta.dirname, "../../..");
const out = process.env.MINICODE_FULL_TASK_OUT!;

test.describe("manual retained real-task recovery", () => {
  test.skip(!out, "Set MINICODE_FULL_TASK_OUT to a retained completed full-task profile to run this manual recovery test.");

test("restores the retained completed real task in an inactive QA window without another provider turn", async () => {
  test.setTimeout(180_000);
  const original = JSON.parse(await readFile(path.join(out, "result.json"), "utf8"));
  const sideBefore = JSON.parse(await readFile(path.join(out, "02-side-while-main-runs.json"), "utf8"));
  const recovery = path.join(out, process.env.MINICODE_FULL_TASK_RECOVERY_DIR ?? "recovery");
  await mkdir(recovery);
  await cp(path.join(root, "frontend", "dist"), path.join(recovery, "frontend-dist"), { recursive: true });
  const shim = path.join(recovery, "inactive-entry.cjs");
  await writeFile(shim, `const {app,BrowserWindow}=require('electron');\napp.commandLine.appendSwitch('disable-renderer-backgrounding');\nBrowserWindow.prototype.show=function(){this.showInactive()};\nBrowserWindow.prototype.focus=function(){};\nrequire(${JSON.stringify(path.join(root,"desktop","main.js"))});\n`);
  const portServer = createServer();
  await new Promise<void>(resolve => portServer.listen(0, "127.0.0.1", resolve));
  const backendPort = (portServer.address() as any).port;
  await new Promise<void>(resolve => portServer.close(() => resolve()));
  const { ELECTRON_RUN_AS_NODE: _, ...env } = process.env;
  const app = await electron.launch({ executablePath: createRequire(path.join(root,"desktop","package.json"))("electron") as string,
    args:[shim],cwd:path.join(root,"desktop"),env:{...env,MINICODE_FRONTEND_URL:pathToFileURL(path.join(recovery,"frontend-dist","index.html")).href,
      MINICODE_USER_DATA_DIR:path.join(out,"state"),MINICODE_CONFIG_DIR:path.join(out,"config"),MINICODE_BACKEND_PORT:String(backendPort),
      CUSTOM_API_KEY:"",OPENAI_API_KEY:"",ANTHROPIC_API_KEY:"",PYTHONUTF8:"1",PYTHONIOENCODING:"utf-8"} });
  const receipt: any = {originalSpecPassed:original.passed,kind:"retained_completed_profile_visible_recovery",providerTurnsStarted:0,gates:[],pageErrors:[],outgoingCommandTypes:[]};
  const started = Date.now();
  const check = (name:string,passed:boolean,detail:any={}) => {receipt.gates.push({name,passed,...detail});console.log(JSON.stringify({name,passed,...detail}));};
  try {
    const page = await app.firstWindow();
    await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].webContents.setBackgroundThrottling(false));
    page.on("pageerror",error=>receipt.pageErrors.push(error.message));
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Network.enable");
    cdp.on("Network.webSocketFrameSent", (frame:any) => {
      if(frame.response.opcode===1) receipt.outgoingCommandTypes.push(JSON.parse(frame.response.payloadData).type);
    });
    const snapshot = async (name:string) => {
      const data = await page.evaluate(() => {
        const s=(window as any).__zustandStore.getState();
        return {conversationId:s.conversationId,pendingConversationSwitchId:s.pendingConversationSwitchId,isStreaming:s.isStreaming,draft:s.draft,messages:s.messages,
          subagents:s.subagents,body:document.body.innerText,process:[...document.querySelectorAll('.chat-turn-process')].map(el=>({collapsed:el.getAttribute('data-collapsed'),text:el.textContent})),
          collaboration:[...document.querySelectorAll('.collaboration-cell')].map(el=>({action:el.getAttribute('data-action'),status:el.getAttribute('data-status'),text:el.textContent}))};
      });
      await writeFile(path.join(recovery,name+".json"),JSON.stringify(data,null,2));
      await page.screenshot({path:path.join(recovery,name+".png")});
      return data;
    };
    await expect.poll(()=>page.evaluate(()=>Boolean((window as any).__zustandStore?.getState().isConnected)),{timeout:90_000}).toBe(true);
    const native=await app.evaluate(({app,BrowserWindow})=>{const w=BrowserWindow.getAllWindows()[0];return {userData:app.getPath("userData"),sessionData:app.getPath("sessionData"),visible:w.isVisible(),focused:w.isFocused()};});
    check("isolated_window_visible_inactive",native.userData===path.join(out,"state")&&native.visible&&!native.focused,{native});
    await page.evaluate(id=>(window as any).__zustandStore.getState().requestConversationSwitch(id),original.mainId);
    await expect.poll(()=>page.evaluate(id=>{const s=(window as any).__zustandStore.getState();return s.conversationId===id&&!s.pendingConversationSwitchId;},original.mainId)).toBe(true);
    const final=await snapshot("00-restored-final");
    const assistant=final.messages.filter((message:any)=>message.role==="assistant").at(-1);
    const text=[assistant.content,...assistant.blocks.filter((block:any)=>block.type==="text").map((block:any)=>block.content)].join("\n");
    check("final_answer_restored",!final.isStreaming&&assistant.terminalStatus==="completed"&&original.facts.every((fact:string)=>text.includes(fact))&&text.includes(original.finalMarker));
    check("two_children_completed",final.subagents.length===2&&final.subagents.every((child:any)=>child.status==="done"||child.status==="completed"),{children:final.subagents.map((child:any)=>({id:child.id,status:child.status,name:child.displayName}))});
    const mainExpand=page.getByRole("button",{name:"展开处理步骤",exact:true});
    if(await mainExpand.count()) await mainExpand.first().evaluate((el:HTMLButtonElement)=>el.click());
    for(let index=0;index<final.subagents.length;index++) {
      const source=final.subagents[index];
      const open=page.getByRole("button",{name:`打开子智能体：${source.displayName}`,exact:true}).first();
      await expect(open).toBeVisible();
      await open.evaluate((el:HTMLButtonElement)=>el.click());
      const child=page.locator('[aria-label="子智能体工作记录"]');
      await expect(child).toBeVisible();
      const expand=child.getByRole("button",{name:"展开处理步骤",exact:true});
      if(await expand.count()) await expand.first().evaluate((el:HTMLButtonElement)=>el.click());
      const sent=child.locator('.collaboration-parent-message');
      await expect(sent).toHaveText("已向父代理发送消息");
      await expect(sent).toBeVisible();
      await expect.poll(()=>child.evaluate(element=>{let opacity=1;for(let node:Element|null=element;node;node=node.parentElement)opacity*=Number(getComputedStyle(node).opacity);return opacity;})).toBe(1);
      const groups=child.locator('button.agent-loop-timeline-group-title[data-group-kind="work"][aria-expanded="false"]');
      for(const group of await groups.all()) await group.evaluate((el:HTMLButtonElement)=>el.click());
      await expect.poll(()=>child.evaluate(element=>element.getAnimations({subtree:true}).filter(animation=>animation.playState==="running"&&animation.effect?.getComputedTiming().iterations!==Infinity).length)).toBe(0);
      const childText=await child.innerText();
      check(`child_${index+1}_real_record_and_parent_message`,original.facts.some((fact:string)=>childText.includes(fact))&&await child.locator('textarea,[role="textbox"]').count()===0&&await child.locator('.collaboration-parent-message').count()===1,{id:source.id,agentName:source.displayName,selectedThroughVisibleIdentity:true});
      await snapshot(`01-child-${index+1}-record`);
      await page.getByRole("button",{name:"返回子智能体列表",exact:true}).evaluate((el:HTMLButtonElement)=>el.click());
    }
    await page.evaluate(id=>(window as any).__zustandStore.getState().requestConversationSwitch(id),original.sideId);
    await expect.poll(()=>page.evaluate(id=>{const s=(window as any).__zustandStore.getState();return s.conversationId===id&&!s.pendingConversationSwitchId;},original.sideId)).toBe(true);
    await expect(page.getByRole("textbox",{name:"消息输入",exact:true})).toHaveText(sideBefore.draft);
    const sideAfter=await snapshot("02-side-draft");
    const composerText=await page.getByRole("textbox",{name:"消息输入",exact:true}).innerText();
    check("side_draft_retained",sideAfter.draft===sideBefore.draft&&composerText===sideBefore.draft,{expected:sideBefore.draft,stateDraft:sideAfter.draft,composerText});
    check("no_new_model_task",!receipt.outgoingCommandTypes.some((type:string)=>["user_message","send_message","followup_task","spawn_agent","agent_resume"].includes(type)),{observedTypes:[...new Set(receipt.outgoingCommandTypes)]});
    check("no_renderer_errors",receipt.pageErrors.length===0);
    receipt.passed=receipt.gates.every((gate:any)=>gate.passed);
    expect(receipt.passed).toBe(true);
  } finally {
    receipt.elapsedSeconds=(Date.now()-started)/1000;
    await writeFile(path.join(recovery,"result.json"),JSON.stringify(receipt,null,2));
    await app.close();
    await writeFile(path.join(recovery,"closed.json"),JSON.stringify({closed:true,at:new Date().toISOString(),elapsedSeconds:(Date.now()-started)/1000}));
  }
});
});
