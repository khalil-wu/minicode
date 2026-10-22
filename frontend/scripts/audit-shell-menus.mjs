import assert from "node:assert/strict";

export async function auditShellMenus({ page, capture, checks }) {
  const profile = page.getByRole("button", { name: "打开个人资料菜单", exact: true });
  await profile.click();
  await capture("shell-01-profile-menu");
  const help = page.getByRole("menuitem", { name: "帮助", exact: true });
  await help.focus();
  await page.keyboard.press("ArrowRight");
  const helpMenu = page.getByRole("menu", { name: "帮助", exact: true });
  await helpMenu.waitFor();
  assert.deepEqual(await helpMenu.getByRole("menuitem").allTextContents(), ["新增内容", "键盘快捷键", "使用指南"]);
  assert.equal(await helpMenu.getByRole("separator").count(), 1);
  assert.equal(await helpMenu.getByRole("menuitem").first().evaluate(element => document.activeElement === element), true);
  const helpBottom = await help.evaluate(element => element.getBoundingClientRect().bottom);
  const submenuBottom = await helpMenu.evaluate(element => element.getBoundingClientRect().bottom);
  assert(Math.abs(submenuBottom - helpBottom - 4) <= 1);
  await capture("shell-02-help-submenu");
  await page.keyboard.press("End");
  const guide = helpMenu.getByRole("menuitem", { name: "使用指南", exact: true });
  for (const key of ["ArrowDown", "Tab", "Shift+Tab"]) {
    await page.keyboard.press(key);
    assert.equal(await guide.evaluate(element => document.activeElement === element), true);
  }
  await capture("shell-03-help-last-item-focus");
  await page.keyboard.press("ArrowLeft");
  await helpMenu.waitFor({ state: "hidden" });
  assert.equal(await help.evaluate(element => document.activeElement === element), true);
  await page.keyboard.press("Escape");
  await page.getByRole("menu").waitFor({ state: "hidden" });
  assert.equal(await profile.evaluate(element => document.activeElement === element), true);

  const actions = page.getByRole("button", { name: "聊天操作", exact: true });
  await actions.click();
  await capture("shell-04-chat-actions-menu");
  const copy = page.getByRole("menuitem", { name: "复制", exact: true });
  await copy.focus();
  await page.keyboard.press("ArrowRight");
  const copyMenu = page.getByRole("menu", { name: "复制", exact: true });
  await copyMenu.waitFor();
  assert.deepEqual(await copyMenu.getByRole("menuitem").allTextContents(), ["复制深度链接", "复制为 Markdown", "复制工作目录"]);
  await page.keyboard.press("Home");
  await page.keyboard.press("ArrowUp");
  assert.equal(await copyMenu.getByRole("menuitem").first().evaluate(element => document.activeElement === element), true);
  await capture("shell-05-chat-copy-submenu");
  await page.keyboard.press("ArrowLeft");
  await copyMenu.waitFor({ state: "hidden" });
  assert.equal(await copy.evaluate(element => document.activeElement === element), true);
  await page.keyboard.press("Escape");
  await page.getByRole("menu").waitFor({ state: "hidden" });
  assert.equal(await actions.evaluate(element => document.activeElement === element), true);
  checks.push("Actual profile/Help and chat Copy menus retain Source separators, parent-bottom alignment, non-looping focus, trapped Tab and original trigger focus on dismissal");
}
