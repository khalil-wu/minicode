// Ad-hoc driver for the demo: node shot.mjs <name> [steps...]
// steps: "type:<text>", "enter", "wait:<ms>", "click:<selector>", "approve", "theme:dark|light", "size:WxH"
import { chromium } from "@playwright/test";

const [, , name = "shot", ...steps] = process.argv;
const browser = await chromium.launch();
let size = { width: 1440, height: 900 };
for (const step of steps) if (step.startsWith("size:")) { const [w, h] = step.slice(5).split("x").map(Number); size = { width: w, height: h }; }
const page = await browser.newPage({ viewport: size, deviceScaleFactor: 1 });
const logs = [];
page.on("console", async (m) => {
  if (!(["error", "warning"].includes(m.type()) || m.text().startsWith("[demo]"))) return;
  const args = await Promise.all(m.args().map((a) => a.jsonValue().catch(() => "?")));
  logs.push(`${m.type()}: ${m.text()} ${JSON.stringify(args.slice(1)).slice(0, 1500)}`);
});
page.on("pageerror", (e) => logs.push(`pageerror: ${e.stack}`));
await page.goto(process.env.DEMO_URL || "http://localhost:5190/", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(2500);
await page.addScriptTag({ path: "../../.tmp/demo-shots/dom.js" });
for (const step of steps) {
  if (step.startsWith("type:")) { await page.locator('[contenteditable="true"]').first().click(); await page.keyboard.type(step.slice(5)); }
  else if (step === "enter") await page.keyboard.press("Enter");
  else if (step.startsWith("wait:")) await page.waitForTimeout(Number(step.slice(5)));
  else if (step.startsWith("click:")) await page.locator(step.slice(6)).first().click();
  else if (step.startsWith("text:")) await page.getByText(step.slice(5), { exact: false }).first().click();
  else if (step.startsWith("shot:")) await page.screenshot({ path: `../../.tmp/demo-shots/${step.slice(5)}.png` });
  else if (step === "plain") await page.evaluate(() => document.querySelectorAll('style[data-vite-dev-id*="newui"]').forEach((n) => n.remove()));
  else if (step.startsWith("theme:")) await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, step.slice(6));
  else if (step.startsWith("hover:")) await page.locator(step.slice(6)).first().hover();
  else if (step.startsWith("eval:")) console.log(await page.evaluate(step.slice(5)));
}
await page.screenshot({ path: `../../.tmp/demo-shots/${name}.png` });
const unhandled = await page.evaluate(() => [...(window.__demoUnhandled ?? [])]);
console.log("UNHANDLED", JSON.stringify(unhandled));
console.log(logs.slice(0, 40).join("\n"));
await browser.close();
