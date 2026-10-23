import assert from "node:assert/strict";
import { expect } from "@playwright/test";

export async function auditThreadControls({ page, app, wire, capture, checks }) {
  const owner = await page.evaluate(() => {
    const state = window.__zustandStore.getState();
    const conversation = state.conversations.find(item => item.id === state.conversationId);
    return { id: conversation.id, title: conversation.title, pinned: Boolean(conversation.pinned), draft: state.draft, viewWorkspace: state.workingDirectory,
      model: state.currentModel, models: state.availableModels,
      workspace: conversation.executionCwd || conversation.worktreePath || conversation.workspaceRoot || state.workingDirectory };
  });
  const composer = page.locator(".composer-input-region [data-composer-input]").first();
  assert(owner.id && owner.workspace && owner.draft);
  assert.equal(await composer.innerText(), owner.draft);
  const messages = (direction, type, since) => wire.slice(since).filter(row => row.direction === direction && row.message.type === type).map(row => row.message);
  const mutation = async (command, since) => {
    const receipts = () => messages("server", "command.result", since).filter(result => result.command === command
      && messages("client", command, since).some(request => request.client_command_id
        && (result.client_command_id === request.client_command_id || result.data?.request_id === request.client_command_id)));
    await expect.poll(receipts, { timeout: 30000 }).toHaveLength(1);
    const requests = messages("client", command, since);
    assert.equal(requests.length, 1);
    const request = requests[0];
    const receipt = receipts()[0];
    assert.equal(receipt.level, "info");
    assert.equal(receipt.data.conversation_id, owner.id);
    assert.equal(receipt.data.workspace_root, owner.workspace);
    assert.deepEqual([request.owner_conversation_id, request.workspace_root], [owner.id, owner.workspace]);
    const snapshots = messages("server", "scheduler.list", since).filter(snapshot => snapshot.request_id === request.client_command_id);
    assert.equal(snapshots.length, 1);
    assert.deepEqual([snapshots[0].conversation_id, snapshots[0].workspace_root], [owner.id, owner.workspace]);
    return { request, receipt, snapshot: snapshots[0] };
  };
  for (const pinned of [!owner.pinned, owner.pinned]) {
    const since = wire.length;
    await page.keyboard.press("Control+Alt+P");
    await page.waitForFunction(({ id, pinned }) => window.__zustandStore.getState().conversations.find(item => item.id === id)?.pinned === pinned, { id: owner.id, pinned });
    const requests = messages("client", "conversation.pin.set", since);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].conversation_id, owner.id);
    assert.equal(requests[0].pinned, pinned);
    const inventory = messages("server", "conversation.list", since).findLast(snapshot => snapshot.conversations.some(item => item.id === owner.id && item.pinned === pinned));
    assert(inventory && Number.isSafeInteger(inventory.inventory_revision));
    assert.equal(await composer.innerText(), owner.draft);
    if (pinned !== owner.pinned) await capture("thread-01-keyboard-pin", { owner: owner.id, pinned, inventoryRevision: inventory.inventory_revision });
  }
  checks.push({ scenario: "Real Ctrl+Alt+P pins and unpins the captured chat through its WS command and authoritative inventory, preserving the main draft", owner: owner.id });

  const actions = page.getByRole("button", { name: "聊天操作", exact: true });
  await actions.click();
  await page.getByRole("menuitem", { name: "添加定时任务…", exact: true }).click();
  const panel = page.locator(`[id="right-panel-automation:draft:${owner.id}"]`);
  await panel.waitFor();
  const geometry = async () => {
    const actual = await panel.locator(".thread-automation-panel").evaluate(element => {
      const scroll = element.querySelector(".thread-automation-scroll"), footer = element.querySelector(".thread-automation-footer");
      const paneStyle = getComputedStyle(element), scrollStyle = getComputedStyle(scroll), footerStyle = getComputedStyle(footer);
      const titleStyle = getComputedStyle(element.querySelector('[aria-label="任务名称"]'));
      return { display: paneStyle.display, direction: paneStyle.flexDirection, overflow: scrollStyle.overflowY, minHeight: scrollStyle.minHeight,
        titleWeight: titleStyle.fontWeight, titleLeading: Number.parseFloat(titleStyle.lineHeight) / Number.parseFloat(titleStyle.fontSize),
        separateFooter: footer.parentElement === element && !scroll.contains(footer), shrink: footerStyle.flexShrink,
        padding: [footerStyle.paddingTop, footerStyle.paddingRight, footerStyle.paddingBottom, footerStyle.paddingLeft],
        border: footerStyle.borderTopWidth, gap: footerStyle.gap, align: footerStyle.justifyContent,
        bottomGap: element.getBoundingClientRect().bottom - footer.getBoundingClientRect().bottom,
        paneGap: element.closest('[role="tabpanel"]').getBoundingClientRect().bottom - element.getBoundingClientRect().bottom };
    });
    const { titleWeight, titleLeading, ...layout } = actual;
    assert.equal(titleWeight, "400");
    assert(Math.abs(titleLeading - 1.25) < .01);
    // Source vt: independent flex scroll, shrink-0 footer with border-t/gap-2/p-panel.
    // app-shared-295ec67e1c5d.css: Electron p-panel = spacing(.25rem) * 3 = 12px.
    assert.deepEqual({ ...layout, bottomGap: 0, paneGap: 0 }, { display: "flex", direction: "column", overflow: "auto", minHeight: "0px",
      separateFooter: true, shrink: "0", padding: ["12px", "12px", "12px", "12px"], border: "1px", gap: "8px", align: "flex-end", bottomGap: 0, paneGap: 0 });
    assert(Math.abs(actual.bottomGap) <= 1 && Math.abs(actual.paneGap) <= 1);
    return actual;
  };
  const dialogGeometry = async (dialog, width) => {
    assert.equal(await dialog.getByRole("button", { name: "关闭", exact: true }).count(), 1);
    const actual = await dialog.evaluate(element => {
      const style = getComputedStyle(element), overlay = getComputedStyle(element.parentElement);
      const title = getComputedStyle(element.querySelector("h2")), subtitle = getComputedStyle(element.querySelector(".automation-dialog-heading p"));
      const heading = getComputedStyle(element.querySelector(".automation-dialog-heading")), actions = element.querySelector(".automation-dialog-actions");
      const footer = getComputedStyle(actions), close = element.querySelector(".automation-dialog-close"), closeStyle = getComputedStyle(close);
      const closeGlyph = getComputedStyle(close.querySelector(":scope > span.automation-source-icon"));
      return { overlay: overlay.backgroundColor, blur: overlay.backdropFilter, filter: overlay.filter,
        width: Number.parseFloat(style.width), padding: style.padding, border: style.borderTopWidth, radius: style.borderRadius,
        title: [title.fontSize, title.lineHeight, title.fontWeight], subtitle: [subtitle.fontSize, subtitle.lineHeight], headingGap: heading.gap,
        footer: [footer.gap, footer.paddingTop], buttons: [...actions.querySelectorAll("button")].map(button => {
          const style = getComputedStyle(button);
          return [Number.parseFloat(style.height), style.padding, style.fontSize, style.lineHeight, style.borderRadius];
        }), close: [Number.parseFloat(closeStyle.width), Number.parseFloat(closeStyle.height), closeStyle.top, closeStyle.right,
          Number.parseFloat(closeGlyph.width), Number.parseFloat(closeGlyph.height)],
        closeMask: closeGlyph.maskImage, expectedCloseMask: `url("${new URL("apps/thread-project-remove.svg", document.baseURI).href}")` };
    });
    // Source Swn/Ewn/Gwn and Electron cascade: automation-dialog-geometry-evidence.json.
    const { closeMask, expectedCloseMask, ...layout } = actual;
    assert.equal(closeMask, expectedCloseMask);
    assert.deepEqual(layout, { overlay: "rgba(0, 0, 0, 0.133)", blur: "none", filter: "none", width, padding: "20px", border: "0px", radius: "20px",
      title: ["18px", "28px", "600"], subtitle: ["14px", "21px"], headingGap: "4px", footer: ["12px", "12px"],
      buttons: [[32, "6px 16px", "14px", "18px", "10px"], [32, "6px 16px", "14px", "18px", "10px"]], close: [22, 22, "16px", "16px", 14, 14] });
    return actual;
  };
  await panel.locator(".thread-automation-footer").waitFor();
  assert.deepEqual((await panel.locator(".thread-automation-footer").getByRole("button").allTextContents()).map(text => text.trim()), ["取消", "创建定时任务"]);
  assert.equal(await panel.getByRole("button", { name: "取消", exact: true }).evaluate(element => getComputedStyle(element).borderTopColor), "rgba(0, 0, 0, 0)");
  assert.equal(await panel.getByRole("button", { name: "创建定时任务", exact: true }).evaluate(element => getComputedStyle(element).color),
    await panel.locator(".thread-automation-panel").evaluate(element => getComputedStyle(element).color));
  const draftAt = wire.length;
  const target = panel.getByRole("button", { name: "定时任务目标", exact: true });
  await target.click();
  const targets = page.getByRole("dialog", { name: "搜索聊天", exact: true });
  await targets.waitFor();
  await expect(targets.getByRole("button", { name: owner.title, exact: true })).toBeEnabled({ timeout: 30000 });
  assert.equal(await targets.getByRole("button", { name: owner.title, exact: true }).getAttribute("aria-pressed"), "true");
  await capture("thread-04-automation-targets", { owner: owner.id });
  await targets.getByRole("button", { name: "每次新建聊天", exact: true }).click();
  assert.equal(await target.getAttribute("title"), "每次新建聊天");
  const project = panel.getByRole("button", { name: /^定时任务项目，当前：/u });
  await project.click();
  const projects = page.getByRole("listbox", { name: "定时任务项目", exact: true });
  await projects.waitFor();
  assert.equal(await projects.getByRole("option", { name: "不在项目中工作", exact: true }).count(), 1);
  assert.equal(await projects.locator('[role="option"][aria-selected="true"]').count(), 1);
  const projectOptions = await projects.getByRole("option").allTextContents();
  await page.keyboard.press("Escape");
  await projects.waitFor({ state: "hidden" });
  const model = panel.getByRole("button", { name: /^定时任务模型，当前：/u });
  assert.equal(await model.getAttribute("aria-label"), "定时任务模型，当前：选择模型");
  await model.click();
  const models = page.getByRole("listbox", { name: "定时任务模型", exact: true });
  await models.waitFor();
  const modelValues = await models.getByRole("option").evaluateAll(elements => elements.map(element => element.getAttribute("data-value")));
  assert(modelValues.includes(owner.model));
  assert(modelValues.every(value => value && [...owner.models, owner.model].includes(value)));
  assert.equal(await models.getByRole("option", { name: "默认", exact: true }).count(), 0);
  assert.equal(await models.locator('[role="option"][aria-selected="true"]').count(), 0);
  await capture("thread-05-automation-standalone-model", { owner: owner.id, projectOptions, modelValues, boundary: "Unsaved standalone branch only; no task or model execution" });
  await page.keyboard.press("Escape");
  await models.waitFor({ state: "hidden" });
  await target.click();
  await targets.getByRole("textbox", { name: "搜索聊天", exact: true }).fill(owner.title);
  await targets.getByRole("button", { name: owner.title, exact: true }).click();
  assert.equal(await target.getAttribute("title"), owner.title);
  assert.equal(await project.count(), 0);
  assert.equal(await model.count(), 0);
  assert.equal(wire.slice(draftAt).filter(row => row.direction === "client" && ["scheduler.add", "scheduler.update", "scheduler.toggle", "scheduler.remove"].includes(row.message.type)).length, 0);
  const name = `UI heartbeat ${owner.id}`, prompt = "Preserve the captured chat and workspace for the scheduled follow-up.";
  const nextRun = Date.UTC(new Date().getUTCFullYear() + 1, 0, 1);
  assert(nextRun > Date.now() + 180000);
  const schedule = `DTSTART:${new Date(nextRun).toISOString().replace(/[-:]/g, "").replace(".000", "")}\nRRULE:FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1;BYHOUR=0;BYMINUTE=0;BYSECOND=0`;
  await panel.getByRole("textbox", { name: "任务名称", exact: true }).fill(name);
  await panel.getByRole("textbox", { name: "提示词", exact: true }).fill(prompt);
  await panel.getByRole("button", { name: /^重复计划，当前：/u }).click();
  const repeats = page.getByRole("listbox", { name: "重复计划", exact: true });
  await repeats.waitFor();
  assert.deepEqual((await repeats.getByRole("option").allTextContents()).map(text => text.trim()), ["间隔", "每天", "工作日", "每周", "自定义"]);
  await repeats.getByRole("option", { name: "自定义", exact: true }).click();
  const gear = panel.getByRole("button", { name: "编辑计划规则", exact: true });
  await panel.getByRole("heading", { name: "频率", exact: true }).hover();
  const gearGeometry = await gear.evaluate(element => {
    const style = getComputedStyle(element), header = element.closest(".automation-section-heading"), heading = header.querySelector("h3");
    const bounds = element.getBoundingClientRect(), headingBounds = heading.getBoundingClientRect();
    return { heading: heading.textContent, action: heading.nextElementSibling === element, text: element.textContent.trim(),
      width: bounds.width, height: bounds.height, padding: style.padding, radius: style.borderRadius, opacity: style.opacity,
      background: style.backgroundColor, border: style.borderTopColor, icon: element.querySelector(".automation-source-icon").getBoundingClientRect().width,
      afterHeading: bounds.left >= headingBounds.right, centerOffset: bounds.y + bounds.height / 2 - headingBounds.y - headingBounds.height / 2 };
  });
  assert.deepEqual({ ...gearGeometry, centerOffset: 0 }, { heading: "频率", action: true, text: "", width: 28, height: 28, padding: "4px", radius: "8px", opacity: "0.75",
    background: "rgba(0, 0, 0, 0)", border: "rgba(0, 0, 0, 0)", icon: 16, afterHeading: true, centerOffset: 0 });
  assert(Math.abs(gearGeometry.centerOffset) <= 1);
  await gear.click();
  const ruleDialog = page.getByRole("dialog", { name: "编辑计划规则", exact: true });
  await ruleDialog.getByText("使用 RRULE 定义自定义计划", { exact: true }).waitFor();
  await expect(ruleDialog.locator("label").filter({ hasText: /^RRULE$/ })).toBeVisible({ timeout: 30000 });
  assert.equal(await ruleDialog.getByRole("textbox", { name: "RRULE", exact: true }).evaluate(element => element.labels.length === 1 && element.labels[0].htmlFor === element.id), true);
  await ruleDialog.getByRole("textbox", { name: "RRULE", exact: true }).fill(schedule);
  const ruleDialogGeometry = await dialogGeometry(ruleDialog, 420);
  assert.deepEqual(await ruleDialog.getByRole("button", { name: "取消", exact: true }).evaluate(element => {
    const style = getComputedStyle(element); return [style.borderTopColor, style.backgroundColor];
  }), ["rgba(0, 0, 0, 0)", "rgba(0, 0, 0, 0)"]);
  await capture("thread-06-automation-custom-rrule", { schedule, nextRun: new Date(nextRun).toISOString(), gearGeometry, ruleDialogGeometry });
  await ruleDialog.getByRole("button", { name: "保存", exact: true }).click();
  await ruleDialog.waitFor({ state: "hidden" });
  await panel.getByRole("button", { name: /^通知，当前：/u }).click();
  const notifications = page.getByRole("listbox", { name: "通知", exact: true });
  await notifications.waitFor();
  assert.deepEqual((await notifications.getByRole("option").allTextContents()).map(text => text.trim()), ["重要更新", "仅失败运行"]);
  assert.equal(await notifications.getByRole("option", { name: "重要更新", exact: true }).getAttribute("aria-selected"), "true");
  await capture("thread-07-automation-notifications", { owner: owner.id });
  await notifications.getByRole("option", { name: "仅失败运行", exact: true }).click();
  const draftGeometry = await geometry();
  await capture("thread-02-automation-draft", { owner: owner.id, workspace: owner.workspace, draftGeometry });
  const addedAt = wire.length;
  await panel.getByRole("button", { name: "创建定时任务", exact: true }).click();
  const added = await mutation("scheduler.add", addedAt);
  const task = added.receipt.data.task;
  assert(task.id);
  assert.equal(task.conversation_id, owner.id);
  assert.equal(task.workspace_root, owner.workspace);
  assert.deepEqual([task.name, task.prompt, task.isolation], [name, prompt, "workspace"]);
  assert.equal(task.schedule, schedule);
  assert.equal(task.kind, "heartbeat");
  assert.equal(task.enabled, true);
  assert.equal(task.notification_policy, "failed_runs_only");
  assert.equal(Date.parse(task.next_run_at), nextRun);
  assert.deepEqual(added.snapshot.tasks.filter(item => item.conversation_id === owner.id), [task]);
  assert.deepEqual([added.request.owner_conversation_id, added.request.conversation_id, added.request.workspace_root], [owner.id, owner.id, owner.workspace]);
  await page.getByRole("button", { name: `关闭${name}标签页`, exact: true }).click();
  await panel.waitFor({ state: "hidden" });
  assert.equal(await composer.innerText(), owner.draft);
  await actions.click();
  await page.getByRole("menuitem", { name: "编辑定时任务…", exact: true }).click();
  await panel.waitFor();
  assert.equal(await panel.getByRole("textbox", { name: "任务名称", exact: true }).inputValue(), name);
  assert.equal(await panel.getByRole("textbox", { name: "提示词", exact: true }).inputValue(), prompt);
  assert.equal(await panel.getByRole("button", { name: "通知，当前：仅失败运行", exact: true }).count(), 1);
  await panel.getByRole("button", { name: "打开设置", exact: true }).waitFor();
  assert.deepEqual((await panel.locator(".thread-automation-footer").getByRole("button").allTextContents()).map(text => text.trim()), ["打开设置"]);
  const updatedAt = wire.length;
  await panel.getByRole("textbox", { name: "提示词", exact: true }).fill(`${prompt} Updated.`);
  const updated = await mutation("scheduler.update", updatedAt);
  assert.equal(updated.request.task_id, task.id);
  assert.deepEqual([updated.request.owner_conversation_id, updated.request.conversation_id, updated.request.workspace_root], [owner.id, owner.id, owner.workspace]);
  assert.equal(updated.receipt.data.task.id, task.id);
  assert.equal(updated.receipt.data.task.name, name);
  assert.deepEqual([updated.receipt.data.task.conversation_id, updated.receipt.data.task.workspace_root, updated.receipt.data.task.prompt], [owner.id, owner.workspace, `${prompt} Updated.`]);
  assert.equal(updated.receipt.data.task.schedule, schedule);
  assert.equal(updated.receipt.data.task.notification_policy, "failed_runs_only");
  assert.equal(Date.parse(updated.receipt.data.task.next_run_at), nextRun);
  assert.equal(messages("client", "scheduler.add", addedAt).length, 1);
  assert.deepEqual(updated.snapshot.tasks.filter(item => item.conversation_id === owner.id), [updated.receipt.data.task]);
  const savedGeometry = await geometry();
  await capture("thread-03-automation-updated", { owner: owner.id, task: updated.receipt.data.task, savedGeometry });
  const operations = panel.getByRole("button", { name: "定时任务操作", exact: true });
  const operationMenu = page.getByRole("menu", { name: "定时任务操作", exact: true });
  const toggles = [];
  for (const [label, enabled, nextLabel] of [["暂停", false, "恢复"], ["恢复", true, "暂停"]]) {
    await operations.click();
    await operationMenu.waitFor();
    assert.deepEqual((await operationMenu.getByRole("menuitem").allTextContents()).map(text => text.trim()), [label, "删除"]);
    assert.equal(await operationMenu.getByRole("separator").count(), 0);
    const since = wire.length;
    await operationMenu.getByRole("menuitem", { name: label, exact: true }).click();
    const toggled = await mutation("scheduler.toggle", since);
    assert.deepEqual([toggled.request.task_id, toggled.request.enabled, toggled.receipt.data.task_id, toggled.receipt.data.enabled], [task.id, enabled, task.id, enabled]);
    assert.deepEqual([toggled.receipt.data.task.id, toggled.receipt.data.task.conversation_id, toggled.receipt.data.task.workspace_root, toggled.receipt.data.task.enabled], [task.id, owner.id, owner.workspace, enabled]);
    assert.deepEqual(toggled.snapshot.tasks.filter(item => item.conversation_id === owner.id), [toggled.receipt.data.task]);
    assert.equal(toggled.receipt.data.task.schedule, schedule);
    if (enabled) assert.equal(Date.parse(toggled.receipt.data.task.next_run_at), nextRun);
    toggles.push(toggled.receipt.data);
    await operations.click();
    await operationMenu.getByRole("menuitem", { name: nextLabel, exact: true }).waitFor();
    if (!enabled) await capture("thread-08-automation-paused-menu", { owner: owner.id, task: toggled.receipt.data.task });
    await page.keyboard.press("Escape");
    await operationMenu.waitFor({ state: "hidden" });
  }
  const cancelledAt = wire.length;
  await operations.click();
  await operationMenu.getByRole("menuitem", { name: "删除", exact: true }).click();
  const deletion = page.getByRole("dialog", { name: `删除 ${name}？`, exact: true });
  await deletion.getByText("这将永久删除定时任务并停止后续运行。", { exact: true }).waitFor();
  const deleteDialogGeometry = await dialogGeometry(deletion, 520);
  await capture("thread-09-automation-delete-confirmation", { owner: owner.id, taskId: task.id, deleteDialogGeometry });
  await deletion.getByRole("button", { name: "取消", exact: true }).click();
  await deletion.waitFor({ state: "hidden" });
  assert.equal(messages("client", "scheduler.remove", cancelledAt).length, 0);
  assert.equal(await panel.isVisible(), true);
  assert.equal(await page.evaluate(id => window.__zustandStore.getState().scheduledTasks.some(task => task.id === id), task.id), true);
  assert.equal(await composer.innerText(), owner.draft);
  await operations.click();
  await operationMenu.getByRole("menuitem", { name: "删除", exact: true }).click();
  const pendingDeletion = await deletion.evaluateHandle(element => {
    const evidence = [];
    const observer = new MutationObserver(() => {
      const confirm = element.querySelector(".automation-danger-button");
      if (confirm.disabled) {
        evidence.push({ closeCount: element.querySelectorAll(".automation-dialog-close").length, confirmDisabled: confirm.disabled,
          cancelDisabled: element.querySelector(".automation-dialog-actions button:not(.automation-danger-button)").disabled });
        observer.disconnect();
      }
    });
    observer.observe(element, { attributes: true, childList: true, subtree: true });
    return evidence;
  });
  const removedAt = wire.length;
  await deletion.getByRole("button", { name: "删除定时任务", exact: true }).click();
  const removed = await mutation("scheduler.remove", removedAt);
  await expect.poll(() => pendingDeletion.jsonValue(), { timeout: 30000 }).toEqual([{ closeCount: 0, confirmDisabled: true, cancelDisabled: true }]);
  const pendingDeletionGeometry = await pendingDeletion.jsonValue();
  await pendingDeletion.dispose();
  assert.deepEqual([removed.request.task_id, removed.receipt.data.task_id], [task.id, task.id]);
  assert.equal(removed.snapshot.tasks.some(item => item.id === task.id), false);
  assert.equal(messages("client", "scheduler.remove", cancelledAt).length, 1);
  assert.equal(messages("client", "scheduler.add", addedAt).length, 1);
  await deletion.waitFor({ state: "hidden" });
  await panel.waitFor({ state: "hidden" });
  await actions.click();
  await page.getByRole("menuitem", { name: "添加定时任务…", exact: true }).waitFor();
  await page.keyboard.press("Escape");
  assert.equal(await composer.innerText(), owner.draft);
  assert.deepEqual(await page.evaluate(() => { const state = window.__zustandStore.getState(); return [state.conversationId, state.workingDirectory, state.draft]; }), [owner.id, owner.viewWorkspace, owner.draft]);
  checks.push({ scenario: "Source task editor preserves its owner and main draft across unsaved standalone controls, real future RRULE creation, same-ID autosave, pause/resume and cancelled/confirmed deletion", owner: owner.id, added: task, updated: updated.receipt.data.task, toggles, removed: removed.receipt.data, draftGeometry, savedGeometry, gearGeometry, ruleDialogGeometry, deleteDialogGeometry, pendingDeletionGeometry,
    boundary: "Standalone Project/Model controls were inspected before creation; no standalone run or alternate-project model execution is claimed" });
}
