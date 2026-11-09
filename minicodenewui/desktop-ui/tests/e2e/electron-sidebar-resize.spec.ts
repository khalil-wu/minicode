import { _electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import { createRequire } from "node:module";
import { createServer } from "node:http";
import { createServer as createPortServer } from "node:net";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "../../..");
const desktop = path.join(root, "desktop");
const electron = createRequire(path.join(desktop, "package.json"))("electron") as string;

async function finishLayout(page: Page) {
  await page.evaluate(async () => {
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    await Promise.allSettled(document.getAnimations().filter(animation =>
      animation.effect?.getComputedTiming().iterations !== Infinity).map(animation => animation.finished));
  });
}

test("native browser leaves the entire sidebar separator draggable and restores the panel", async ({}, testInfo) => {
  test.setTimeout(120_000);
  const profile = await mkdtemp(path.join(tmpdir(), "minicode-sidebar-resize-"));
  const workspace = path.join(profile, "workspace");
  await mkdir(workspace);
  await writeFile(path.join(profile, "desktop-preferences.json"), JSON.stringify({
    localeOverride: "zh-CN", defaultTaskDirectory: workspace, confirmBeforeClose: "never",
  }));
  const server = createServer((_request, response) => {
    response.setHeader("content-type", "text/html; charset=utf-8");
    response.end("<!doctype html><title>Sidebar resize fixture</title><h1>Native browser content</h1>");
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${(server.address() as { port: number }).port}/`;
  const portServer = createPortServer();
  await new Promise<void>(resolve => portServer.listen(0, "127.0.0.1", resolve));
  const port = (portServer.address() as { port: number }).port;
  await new Promise<void>(resolve => portServer.close(() => resolve()));
  const { ELECTRON_RUN_AS_NODE: _nodeMode, ...environment } = process.env;
  let app: ElectronApplication | undefined;
  try {
    app = await _electron.launch({ executablePath: electron, args: [path.join(desktop, "main.js")], cwd: desktop,
      env: { ...environment, MINICODE_PYTHON: process.env.MINICODE_PYTHON || path.join(desktop, "python-runtime/python.exe"),
        MINICODE_USER_DATA_DIR: profile, MINICODE_BACKEND_PORT: String(port), MINICODE_FRONTEND_URL: "",
        MINICODE_CONFIG_DIR: path.join(profile, "config"), MINICODE_SYSTEM_CONFIG_FILE: path.join(profile, "absent.toml"),
        MINICODE_REQUIREMENTS_FILE: path.join(profile, "absent-requirements.toml"), MINICODE_MANAGED_SETTINGS_DIR: path.join(profile, "managed"),
        LLM_PROVIDER: "custom", CUSTOM_API_KEY: "", CUSTOM_BASE_URL: "", CUSTOM_MODEL: "", OPENAI_API_KEY: "", ANTHROPIC_API_KEY: "",
      } });
    const page = await app.firstWindow();
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await app.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows()[0];
      window.webContents.setBackgroundThrottling(false);
      window.setContentSize(1920, 1080);
      window.hide();
    });
    await page.waitForFunction(() => (window as any).__zustandStore?.getState().isConnected);
    const composer = page.getByRole("textbox", { name: "消息输入", exact: true });
    await composer.fill("Unsent draft must survive every sidebar resize");
    await page.getByRole("button", { name: "新建标签页", exact: true }).click();
    const address = page.getByRole("textbox", { name: "搜索或输入网址", exact: true });
    await address.fill(url);
    await address.press("Enter");
    const nativeBounds = async () => app!.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows()[0];
      const child = window.contentView.children.find(view => "webContents" in view
        && (view as Electron.WebContentsView).webContents.getTitle() === "Sidebar resize fixture");
      return child ? { ...child.getBounds(), zoom: window.webContents.getZoomFactor() } : null;
    });
    await expect.poll(nativeBounds).not.toBeNull();
    const handle = page.getByRole("separator", { name: "调整右侧栏宽度", exact: true });
    for (const zoom of [1]) {
      await app.evaluate(({ BrowserWindow }, value) => BrowserWindow.getAllWindows()[0].webContents.setZoomFactor(value), zoom);
      await finishLayout(page);
      for (const [fraction, delta] of [[0.1, 64], [0.5, -64], [0.9, 64]]) {
        const box = (await handle.boundingBox())!;
        const native = (await nativeBounds())!;
        console.log(JSON.stringify({ zoom, fraction, box, native }));
        // CSS z-index cannot raise a renderer element above WebContentsView.
        expect((box.x + box.width) * native.zoom).toBeLessThanOrEqual(native.x + 1);
        const point = { x: box.x + box.width / 2, y: box.y + box.height * fraction };
        expect(await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.getAttribute("role"), point)).toBe("separator");
        const width = Number(await handle.getAttribute("aria-valuenow"));
        await page.mouse.move(point.x, point.y);
        await page.mouse.down();
        await page.mouse.move(point.x + delta, point.y, { steps: 5 });
        await page.mouse.up();
        // Electron rounds the CSS/native split to device pixels while the
        // pointer move is reported in CSS pixels. Codex's native split has
        // the same bounded rounding at the edge; keep the assertion on the
        // resulting width instead of requiring a fractional-pixel identity.
        await expect.poll(async () => Number(await handle.getAttribute("aria-valuenow")))
          .toBeGreaterThanOrEqual(width - delta - 2);
        await expect.poll(async () => Number(await handle.getAttribute("aria-valuenow")))
          .toBeLessThanOrEqual(width - delta + 2);
        await expect.poll(async () => (await nativeBounds())!.width).toBeGreaterThan(0);
        await expect.poll(() => page.evaluate(() => document.body.classList.contains("layout-dragging"))).toBe(false);
      }
    }
    await page.getByRole("button", { name: "扩大到主工作区", exact: true }).click();
    await expect(handle).toHaveCount(0);
    await page.getByRole("button", { name: "还原到侧栏", exact: true }).click();
    await expect(handle).toBeVisible();
    await finishLayout(page);
    const box = (await handle.boundingBox())!;
    const native = (await nativeBounds())!;
    expect((box.x + box.width) * native.zoom).toBeLessThanOrEqual(native.x + 1);
    await expect(composer).toHaveText("Unsent draft must survive every sidebar resize");
    await page.screenshot({ path: testInfo.outputPath("native-sidebar-restored.png") });
    expect(errors).toEqual([]);
  } finally {
    if (app) await app.close();
    await new Promise<void>(resolve => server.close(() => resolve()));
  }
});
