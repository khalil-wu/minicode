import assert from "node:assert/strict";
import { expect } from "@playwright/test";

export async function auditThreadControls({ page, app, wire, capture, checks }) {
  const owner = await page.evaluate(() => {
    const state = window.__zustandStore.getState();
    const conversation = state.conversations.find(item => item.id === state.conversationId);
    return { id: conversation.id, pinned: Boolean(conversation.pinned), draft: state.draft, viewWorkspace: state.workingDirectory,
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
    return { request, receipt };
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
  const name = `UI heartbeat ${owner.id}`;
  const prompt = "Preserve the captured chat and workspace for the scheduled follow-up.";
  await panel.getByRole("textbox", { name: "任务名称", exact: true }).fill(name);
  await panel.getByPlaceholder("要运行的提示词", { exact: true }).fill(prompt);
  await panel.getByRole("button", { name: "运行频率，当前：每小时", exact: true }).click();
  await page.getByRole("listbox", { name: "运行频率", exact: true }).getByRole("option", { name: "自定义 Cron", exact: true }).click();
  await panel.getByPlaceholder("Cron 表达式（例如 0 9 * * 1-5）", { exact: true }).fill("0 0 1 1 *");
  await capture("thread-02-automation-draft", { owner: owner.id, workspace: owner.workspace });
  const addedAt = wire.length;
  await panel.getByRole("button", { name: "创建定时任务", exact: true }).click();
  const added = await mutation("scheduler.add", addedAt);
  const task = added.receipt.data.task;
  assert(task.id);
  assert.equal(task.conversation_id, owner.id);
  assert.equal(task.workspace_root, owner.workspace);
  assert.deepEqual([task.name, task.prompt, task.isolation], [name, prompt, "workspace"]);
  assert.equal(task.schedule, "0 0 1 1 *");
  assert.equal(task.kind, "heartbeat");
  assert(Date.parse(task.next_run_at) > Date.now() + 180000);
  assert.deepEqual([added.request.owner_conversation_id, added.request.conversation_id, added.request.workspace_root], [owner.id, owner.id, owner.workspace]);
  await page.getByRole("button", { name: `关闭${name}标签页`, exact: true }).click();
  await panel.waitFor({ state: "hidden" });
  assert.equal(await composer.innerText(), owner.draft);
  await actions.click();
  await page.getByRole("menuitem", { name: "编辑定时任务…", exact: true }).click();
  await panel.waitFor();
  assert.equal(await panel.getByRole("textbox", { name: "任务名称", exact: true }).inputValue(), name);
  const updatedAt = wire.length;
  await panel.getByPlaceholder("要运行的提示词", { exact: true }).fill(`${prompt} Updated.`);
  const updated = await mutation("scheduler.update", updatedAt);
  assert.equal(updated.request.task_id, task.id);
  assert.deepEqual([updated.request.owner_conversation_id, updated.request.conversation_id, updated.request.workspace_root], [owner.id, owner.id, owner.workspace]);
  assert.equal(updated.receipt.data.task.id, task.id);
  assert.equal(updated.receipt.data.task.name, name);
  assert.deepEqual([updated.receipt.data.task.conversation_id, updated.receipt.data.task.workspace_root, updated.receipt.data.task.prompt], [owner.id, owner.workspace, `${prompt} Updated.`]);
  assert.equal(updated.receipt.data.task.schedule, "0 0 1 1 *");
  assert(Date.parse(updated.receipt.data.task.next_run_at) > Date.now() + 180000);
  assert.equal(messages("client", "scheduler.add", addedAt).length, 1);
  assert.equal(messages("server", "scheduler.list", updatedAt).at(-1).tasks.filter(item => item.conversation_id === owner.id).length, 1);
  await capture("thread-03-automation-updated", { owner: owner.id, task: updated.receipt.data.task });
  await page.getByRole("button", { name: `关闭${name}标签页`, exact: true }).click();
  await panel.waitFor({ state: "hidden" });
  assert.equal(await composer.innerText(), owner.draft);
  assert.deepEqual(await page.evaluate(() => { const state = window.__zustandStore.getState(); return [state.conversationId, state.workingDirectory, state.draft]; }), [owner.id, owner.viewWorkspace, owner.draft]);
  checks.push({ scenario: "Header creates a real owned heartbeat, reopens the right automation tab and updates the ACK task ID without duplicate add; closing preserves the main draft", owner: owner.id, added: added.receipt.data.task, updated: updated.receipt.data.task });
}
