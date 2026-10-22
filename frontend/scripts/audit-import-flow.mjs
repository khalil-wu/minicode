import assert from "node:assert/strict";
import { chmod, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

// CI-only: real detection, filesystem migration and error. Only delivery of the
// first completed HTTP response is held to inspect the renderer's pending state.
export async function auditImportFlow({ page, profile, sourceHome, capture, checks }) {
  assert.equal(process.env.CI, "true");
  await page.locator(".settings-center-tab").filter({ hasText: "常规" }).click();
  const importButton = page.getByRole("button", { name: "导入", exact: true });
  await importButton.click();
  const providers = page.getByRole("dialog", { name: "从其他 AI 应用导入", exact: true });
  await capture("import-01-actual-provider-menu");
  await providers.getByRole("button", { name: "继续", exact: true }).click();
  const selection = page.getByRole("dialog", { name: "选择要导入的内容", exact: true });
  await selection.getByRole("button", { name: "展开工具与配置详情", exact: true }).click();
  await capture("import-02-actual-selected-items");

  let releaseResponse;
  const responseGate = new Promise(resolve => { releaseResponse = resolve; });
  let migrated;
  let acknowledgeMigration;
  const migrationComplete = new Promise(resolve => { acknowledgeMigration = resolve; });
  const routeImport = async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    const response = await route.fetch();
    migrated = await response.json();
    acknowledgeMigration();
    await responseGate;
    await route.fulfill({ response });
  };
  await page.route("**/api/settings/import", routeImport);
  try {
    await selection.getByRole("button", { name: "导入", exact: true }).click();
    const pending = page.getByRole("dialog", { name: "正在导入外部智能体配置", exact: true });
    await pending.waitFor();
    await migrationComplete;
    assert(migrated.results.length > 20);
    assert(migrated.results.every(result => result.status === "success"));
    assert.equal(await page.getByRole("dialog", { name: "选择要导入的内容", exact: true }).count(), 0);
    assert.equal(await page.getByText("在后台继续", { exact: true }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "查看导入内容", exact: true }).count(), 0);
    await page.keyboard.press("Escape");
    await page.locator(".mc-project-dialog-backdrop").click({ position: { x: 4, y: 4 } });
    await pending.waitFor();
    await capture("import-03-real-request-awaiting-receipt");
    const scroll = pending.getByRole("button", { name: "滚动到底部", exact: true });
    await scroll.click();
    await page.waitForFunction(() => {
      const element = document.querySelector(".settings-import-files-content");
      return element.scrollHeight - element.clientHeight - element.scrollTop <= 8;
    });
    await capture("import-04-progress-scroll-bottom");
  } finally {
    releaseResponse();
  }
  const completed = page.getByRole("dialog", { name: "已导入外部智能体配置", exact: true });
  await completed.waitFor();
  assert.equal(await completed.locator('.settings-import-file-status[data-status="success"]').count(), migrated.results.length);
  for (const result of migrated.results.filter(result => result.item_type === "instructions" || result.item_type === "commands")) {
    assert(result.target_path.startsWith(profile));
    assert((await readFile(result.target_path, "utf8")).length > 0);
  }
  await completed.locator(".settings-import-files-content").evaluate(element => element.scrollTo({ top: 0 }));
  await capture("import-05-actual-files-imported");
  await completed.getByRole("button", { name: "确定", exact: true }).click();
  await page.unroute("**/api/settings/import", routeImport);
  checks.push("Actual import detection and file copies → pending response delivery → locked progress dialog → actual item results and success OK; no Background/View imported intermediary");

  // Create a real destination I/O failure after detection, without substituting
  // an API payload or claiming a successful write from a synthetic transcript.
  const commandSource = path.join(sourceHome, ".claude", "commands", "write-denied.md");
  await writeFile(commandSource, "---\ndescription: Audit an actual failed destination write\n---\nRead the actual file.\n");
  await page.locator(".settings-center-tab").filter({ hasText: "外观" }).click();
  await page.locator(".settings-center-tab").filter({ hasText: "常规" }).click();
  await page.getByRole("button", { name: "再次导入", exact: true }).click();
  await page.getByRole("dialog", { name: "从其他 AI 应用导入", exact: true }).getByRole("button", { name: "继续", exact: true }).click();
  const commandResult = migrated.results.find(result => result.item_type === "commands");
  const destination = path.dirname(commandResult.target_path);
  await chmod(destination, 0o555);
  try {
    const [response] = await Promise.all([
      page.waitForResponse(response => response.url().endsWith("/api/settings/import") && response.request().method() === "POST"),
      page.getByRole("dialog", { name: "选择要导入的内容", exact: true }).getByRole("button", { name: "导入", exact: true }).click(),
    ]);
    const failed = await response.json();
    assert(failed.results.some(result => result.status === "error" && result.title.includes("write-denied")));
    const error = page.getByRole("dialog", { name: "导入失败", exact: true });
    await error.waitFor();
    assert.equal(await error.getByRole("button", { name: "确定", exact: true }).count(), 0);
    assert.equal(await error.getByRole("link").count(), 0);
    await capture("import-06-real-destination-failure");
    await page.keyboard.press("Escape");
    await error.waitFor({ state: "hidden" });
    checks.push("Actual import destination write failure preserves the original item error, exposes no nonexistent file link and closes only after the real result");
  } finally {
    await chmod(destination, 0o755);
  }
}
