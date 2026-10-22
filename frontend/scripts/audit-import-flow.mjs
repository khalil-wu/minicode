import assert from "node:assert/strict";
import { chmod, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

// CI-only: real detection, filesystem migration and error. Only delivery of the
// first completed HTTP response is held to inspect the renderer's pending state.
export async function auditImportFlow({ page, profile, sourceHome, capture, checks, trace }) {
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
    const scrollGeometry = await pending.evaluate(element => {
      const content = element.querySelector(".settings-import-files-content");
      return { width: element.getBoundingClientRect().width, display: getComputedStyle(element).display,
        padding: getComputedStyle(element).padding, headingFont: getComputedStyle(element.querySelector("h2")).fontSize,
        overlay: getComputedStyle(element.parentElement).backgroundColor,
        contentHeight: content.clientHeight, scrollHeight: content.scrollHeight };
    });
    assert.equal(scrollGeometry.width, 520);
    assert.equal(scrollGeometry.display, "flex");
    assert.equal(scrollGeometry.padding, "0px");
    assert.equal(scrollGeometry.headingFont, "20px");
    assert.equal(scrollGeometry.overlay, "rgba(0, 0, 0, 0.133)");
    assert(scrollGeometry.scrollHeight > scrollGeometry.contentHeight);
    await capture("import-03-real-request-awaiting-receipt", { scrollGeometry });
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
  assert.equal(await completed.getByRole("button", { name: "确定", exact: true }).count(), 1);
  await capture("import-05-actual-files-imported");
  await page.unroute("**/api/settings/import", routeImport);
  checks.push("Actual import detection and file copies → pending response delivery → locked progress dialog → actual item results and success OK; no Background/View imported intermediary");

  const importedFile = migrated.results.find(result => result.item_type === "instructions" && result.workspace_root === "");
  assert(importedFile, "The actual home instruction file must be present in this import");
  const beforeOpen = await page.evaluate(() => {
    const state = window.__zustandStore.getState();
    return { workspace: state.workingDirectory, conversation: state.conversationId, draft: state.draft,
      attachments: state.attachments, hiddenDrafts: state.workspaceComposerDrafts };
  });
  await completed.getByRole("link", { name: importedFile.target_path, exact: true }).click();
  await completed.waitFor({ state: "hidden" });
  await page.locator(".settings-center").waitFor({ state: "hidden" });
  await page.waitForFunction(target => window.__zustandStore.getState().editorTabs.some(tab => tab.path === target && !tab.loading), importedFile.target_path);
  const opened = await page.evaluate(target => {
    const state = window.__zustandStore.getState();
    return { workspace: state.workingDirectory, conversation: state.conversationId, draft: state.draft,
      attachments: state.attachments, hiddenDrafts: state.workspaceComposerDrafts,
      tab: state.editorTabs.find(tab => tab.path === target) };
  }, importedFile.target_path);
  assert.equal(opened.tab.error ?? null, null);
  assert.equal(opened.tab.content, await readFile(importedFile.target_path, "utf8"));
  assert(opened.tab.workspaceOwner.session_id);
  assert.equal(opened.tab.workspaceOwner.conversation_id, "");
  for (const field of Object.keys(beforeOpen)) assert.deepEqual(opened[field], beforeOpen[field]);
  const editedImport = `${opened.tab.content}\nAn actual saved edit from the isolated import opener audit.\n`;
  await page.evaluate(({ file, content }) => window.__zustandStore.getState().updateTabContent(file, content), { file: opened.tab.path, content: editedImport });
  const [savedImport] = await Promise.all([
    page.waitForResponse(response => response.url().includes("/api/workspace/file/compare-write") && response.request().method() === "PUT"),
    page.evaluate(() => window.dispatchEvent(new Event("editor:save"))),
  ]);
  assert(savedImport.ok(), await savedImport.text());
  assert.equal(new URL(savedImport.url()).searchParams.get("conversation_id"), "");
  assert.equal(new URL(savedImport.url()).searchParams.get("session_id"), opened.tab.workspaceOwner.session_id);
  await page.waitForFunction(({ file, content }) => window.__zustandStore.getState().editorTabs.some(tab => tab.path === file && tab.original === content), { file: opened.tab.path, content: editedImport });
  assert.equal(await readFile(importedFile.target_path, "utf8"), editedImport);
  await capture("import-06-open-and-save-real-imported-file");
  checks.push("Actual successful home-import link resolves its ledger identity, honors the preferred file target and reads/saves the exact registered file in the original editor workspace with drafts retained");

  // Create a real destination I/O failure after detection, without substituting
  // an API payload or claiming a successful write from a synthetic transcript.
  const commandSource = path.join(sourceHome, ".claude", "commands", "write-denied.md");
  await writeFile(commandSource, "---\ndescription: Audit an actual failed destination write\n---\nRead the actual file.\n");
  await page.evaluate(() => {
    const state = window.__zustandStore.getState();
    state.setSettingsTab("appearance");
    state.toggleSettings();
  });
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
    assert.equal(failed.results.length, 1, "The new command is the only newly detected import item");
    assert(failed.results.some(result => result.status === "error" && result.title.includes("write-denied")));
    const error = page.getByRole("dialog", { name: "导入失败", exact: true });
    await error.waitFor();
    assert.equal(await error.getByRole("button", { name: "确定", exact: true }).count(), 0);
    assert.equal(await error.getByRole("link").count(), 0);
    assert.equal(await error.getByRole("button", { name: "滚动到底部", exact: true }).count(), 0);
    const failureScroll = await error.locator(".settings-import-files-content").evaluate(element => ({ height: element.clientHeight, scrollHeight: element.scrollHeight }));
    assert.equal(failureScroll.scrollHeight, failureScroll.height);
    const failedItem = error.locator(".settings-import-file-result").filter({ hasText: "write-denied.md" });
    assert((await failedItem.innerText()).includes("Permission denied"));
    const failureGeometry = await failedItem.evaluate(element => {
      const row = element.getBoundingClientRect();
      const viewport = element.closest(".settings-import-files-content").getBoundingClientRect();
      return { rowTop: row.top, rowBottom: row.bottom, viewportTop: viewport.top, viewportBottom: viewport.bottom };
    });
    assert(failureGeometry.rowTop >= failureGeometry.viewportTop && failureGeometry.rowBottom <= failureGeometry.viewportBottom);
    await capture("import-07-real-destination-failure", { failureScroll, failureGeometry });
    trace("import:failure:escape");
    await page.keyboard.press("Escape");
    await error.waitFor({ state: "hidden" });
    trace("import:failure:closed");
    checks.push("Actual import destination write failure preserves the original item error, exposes no nonexistent file link and closes only after the real result");
  } finally {
    await chmod(destination, 0o755);
  }
}
