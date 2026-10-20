"use strict";
const sprite = document.getElementById("pet");
const activity = document.getElementById("activity");
const error = document.getElementById("error");
const media = window.matchMedia("(prefers-reduced-motion: reduce)");
let state;
let spriteKey = "";
let timer;
let frame = 0;
let currentAnimation = "";
let drag;
let hover = false;
let pointerInteractive = false;
let composingTask = null;
let spriteFailed = false;
const pendingTasks = new Set();
const actionErrors = new Map();
const followupDrafts = new Map();
const followupErrors = new Map();
const showError = reason => { spriteFailed = false; error.textContent = String(reason); error.hidden = false; };
const showSpriteError = reason => { showError(reason); spriteFailed = true; sprite.hidden = true; };
const act = (action, payload) => { void window.miniPet.act(action, payload).catch(showError); };
function pointerInteraction(enabled) {
  if (pointerInteractive === enabled) return;
  pointerInteractive = enabled; act("pointer-interaction", { enabled });
}
async function submitTaskAction(task, action) {
  pendingTasks.add(task.id); for (const item of task.actions) actionErrors.delete(item.id); renderActivity();
  try { await window.miniPet.act("task-action", { taskId: task.id, actionId: action.id }); }
  catch (reason) { actionErrors.set(action.id, String(reason)); }
  finally { pendingTasks.delete(task.id); renderActivity(); }
}
function renderActivity() {
  // Replacing a composing textarea aborts the platform IME. Apply the latest
  // task state after compositionend while retaining the user's candidate text.
  if (composingTask !== null) return;
  const focused = document.activeElement;
  const editing = focused?.matches(".notice-followup textarea") ? { taskId: focused.dataset.taskId, start: focused.selectionStart, end: focused.selectionEnd } : null;
  const chinese = /^zh/i.test(state.locale);
  activity.hidden = state.activity.tasks.length === 0;
  activity.style.maxHeight = `${state.layout.noticeHeight}px`;
  const labels = chinese ? { running: "运行中", waiting: "需要输入", failed: "受阻", review: "已就绪" } : { running: "Running", waiting: "Needs input", failed: "Blocked", review: "Ready" };
  activity.replaceChildren(...state.activity.tasks.map(task => {
    const card = document.createElement("div"); card.className = "notice"; card.setAttribute("role", "group"); card.setAttribute("aria-label", task.title);
    const open = document.createElement("button"); open.type = "button"; open.className = "notice-open"; open.setAttribute("aria-label", `${chinese ? "打开通知" : "Open notification"}: ${task.title}`);
    const copy = document.createElement("span"); copy.className = "notice-copy";
    const title = document.createElement("span"); title.className = "notice-title"; title.textContent = task.title;
    const status = document.createElement("span"); status.className = "notice-status"; status.textContent = labels[task.status]; copy.append(title, status); open.append(copy);
    if (!task.actions?.length && task.status !== "running") { const review = document.createElement("span"); review.className = "notice-review"; review.textContent = chinese ? "审阅" : "Review"; open.append(review); }
    open.addEventListener("click", () => act("open-task", { id: task.id }));
    const dismiss = document.createElement("button"); dismiss.type = "button"; dismiss.className = "notice-dismiss"; dismiss.textContent = "×";
    dismiss.setAttribute("aria-label", `${chinese ? "关闭通知" : "Dismiss notification"}: ${task.title}`);
    dismiss.addEventListener("click", () => act("dismiss-task", { taskId: task.id, turnKey: task.turnKey }));
    const heading = document.createElement("div"); heading.className = "notice-heading"; heading.append(open, dismiss); card.append(heading);
    card.addEventListener("contextmenu", event => { event.preventDefault(); event.stopPropagation(); act("task-context-menu", { taskId: task.id }); });
    if (task.detail) { const detail = document.createElement("p"); detail.className = "notice-detail"; detail.textContent = task.detail; card.append(detail); }
    if (task.actions?.length) {
      const actions = document.createElement("div"); actions.className = "notice-actions";
      for (const action of task.actions) {
        const button = document.createElement("button"); button.type = "button"; button.textContent = action.label; button.disabled = pendingTasks.has(task.id);
        button.addEventListener("click", () => { void submitTaskAction(task, action); }); actions.append(button);
      }
      card.append(actions);
      const failure = task.actions.map(action => actionErrors.get(action.id)).find(Boolean);
      if (failure) { const message = document.createElement("p"); message.className = "notice-error"; message.setAttribute("role", "alert"); message.textContent = failure; card.append(message); }
    }
    if (task.followUpAllowed) {
      const form = document.createElement("form"); form.className = "notice-followup";
      const input = document.createElement("textarea"); input.rows = 1; input.dataset.taskId = task.id;
      input.placeholder = chinese ? "跟进…" : "Follow up…"; input.setAttribute("aria-label", input.placeholder); input.value = followupDrafts.get(task.id) || "";
      input.addEventListener("input", () => followupDrafts.set(task.id, input.value));
      input.addEventListener("compositionstart", () => { composingTask = task.id; });
      input.addEventListener("compositionend", () => { followupDrafts.set(task.id, input.value); composingTask = null; renderActivity(); });
      input.addEventListener("keydown", event => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing && composingTask !== task.id) { event.preventDefault(); form.requestSubmit(); }
      });
      input.addEventListener("pointerdown", () => { void window.miniPet.act("focus-followup", { taskId: task.id }).then(() => input.focus()).catch(showError); });
      const send = document.createElement("button"); send.type = "submit"; send.textContent = chinese ? "发送" : "Send"; send.disabled = pendingTasks.has(task.id);
      form.append(input, send);
      form.addEventListener("submit", async event => {
        event.preventDefault();
        const prompt = followupDrafts.get(task.id) || "";
        if (!prompt.trim() || pendingTasks.has(task.id)) return;
        pendingTasks.add(task.id); followupErrors.delete(task.id); renderActivity();
        try {
          await window.miniPet.act("task-followup", { taskId: task.id, prompt });
          if (followupDrafts.get(task.id) === prompt) followupDrafts.delete(task.id);
        } catch (reason) { followupErrors.set(task.id, String(reason)); }
        finally { pendingTasks.delete(task.id); renderActivity(); }
      });
      card.append(form);
      if (followupErrors.has(task.id)) { const message = document.createElement("p"); message.className = "notice-error"; message.setAttribute("role", "alert"); message.textContent = followupErrors.get(task.id); card.append(message); }
    }
    return card;
  }));
  if (editing) {
    const input = [...activity.querySelectorAll("textarea")].find(input => input.dataset.taskId === editing.taskId);
    if (input) { input.focus({ preventScroll: true }); input.setSelectionRange(editing.start, editing.end); }
  }
  requestAnimationFrame(() => act("element-size-changed", { height: activity.scrollHeight }));
}
function draw(row, column) {
  const selected = state.pets.find(pet => pet.id === state.selectedPetId);
  const rows = selected.spriteVersionNumber === 2 ? 11 : 9;
  sprite.style.backgroundSize = `800% ${rows * 100}%`;
  sprite.style.backgroundPosition = `${column / 7 * 100}% ${row / (rows - 1) * 100}%`;
}
function animate() {
  clearTimeout(timer);
  if (!state || !state.petVisible || !state.miniOpen) return;
  const animation = drag ? drag.direction : hover ? "jumping" : state.activity.waiting ? "waiting" : state.activity.running ? "running" : state.activity.failed ? "failed" : state.activity.review ? "review" : "idle";
  const reduced = state.reducedMotion || media.matches;
  if (currentAnimation !== animation) { currentAnimation = animation; frame = 0; }
  const idle = state.animations.idleDurations.map((duration, column) => ({ row: 0, column, duration: duration * state.animations.idleHoldMultiplier }));
  const row = state.animations.states[animation];
  const frames = row ? Array.from({ length: row[1] }, (_, column) => ({ row: row[0], column, duration: row[column === row[1] - 1 ? 3 : 2] })) : idle;
  const loop = ["idle", "running", "waiting", "review", "running-left", "running-right"].includes(animation);
  const sequence = loop ? frames : [...frames, ...frames, ...frames, ...idle];
  if (reduced) { frame = 0; draw(sequence[0].row, sequence[0].column); return; }
  const current = sequence[frame];
  draw(current.row, current.column);
  timer = setTimeout(() => { frame = frame + 1 < sequence.length ? frame + 1 : loop ? 0 : frames.length * 3; animate(); }, current.duration);
}
async function apply(next) {
  state = next;
  if (!state.miniOpen) { drag = null; hover = false; sprite.dataset.dragging = "false"; pointerInteraction(false); }
  const actionIds = new Set(state.activity.tasks.flatMap(task => (task.actions ?? []).map(action => action.id)));
  for (const id of actionErrors.keys()) if (!actionIds.has(id)) actionErrors.delete(id);
  document.documentElement.lang = next.locale;
  sprite.style.width = `${state.size}px`;
  sprite.style.height = `${state.size * 208 / 192}px`;
  renderActivity();
  const selected = state.pets.find(pet => pet.id === state.selectedPetId);
  if (!selected) { showSpriteError(`Selected pet is unavailable: ${state.selectedPetId}`); return; }
  sprite.setAttribute("aria-label", selected.displayName);
  sprite.hidden = !state.petVisible;
  const key = `${selected.id}:${selected.updatedAt}`;
  if (key !== spriteKey) {
    spriteKey = key;
    let image;
    try { image = await window.miniPet.getSprite(selected.id); }
    catch (reason) { if (key === spriteKey) showSpriteError(reason); return; }
    if (key !== spriteKey) return;
    sprite.style.backgroundImage = `url("${image}")`;
    if (spriteFailed) { error.hidden = true; spriteFailed = false; }
  }
  animate();
}
sprite.addEventListener("pointerenter", () => { hover = true; animate(); });
sprite.addEventListener("pointerleave", () => { hover = false; animate(); });
sprite.addEventListener("pointerdown", event => {
  if (event.button !== 0) return;
  drag = { id: event.pointerId, x: event.screenX, y: event.screenY, startX: event.screenX, startY: event.screenY, moved: false, direction: "running-right" }; sprite.dataset.dragging = "true"; sprite.setPointerCapture(event.pointerId);
  act("drag-start", { x: event.screenX, y: event.screenY }); animate();
});
sprite.addEventListener("pointermove", event => {
  if (drag?.id !== event.pointerId) return;
  if (!drag.moved && Math.abs(event.screenX - drag.startX) < 4 && Math.abs(event.screenY - drag.startY) < 4) return;
  drag.moved = true;
  if (event.screenX !== drag.x) drag.direction = event.screenX < drag.x ? "running-left" : "running-right";
  drag.x = event.screenX;
  drag.y = event.screenY;
  act("drag-move", { x: event.screenX, y: event.screenY }); animate();
});
function endDrag(event) {
  if (drag?.id !== event.pointerId) return;
  const moved = drag.moved;
  act("drag-end", { x: moved ? event.type === "pointerup" ? event.screenX : drag.x : drag.startX,
    y: moved ? event.type === "pointerup" ? event.screenY : drag.y : drag.startY }); drag = null; sprite.dataset.dragging = "false";
  if (sprite.hasPointerCapture(event.pointerId)) sprite.releasePointerCapture(event.pointerId);
  animate();
  if (!moved && event.type === "pointerup") act("open-main");
}
sprite.addEventListener("pointerup", endDrag);
sprite.addEventListener("pointercancel", endDrag);
sprite.addEventListener("lostpointercapture", endDrag);
sprite.addEventListener("contextmenu", event => { event.preventDefault(); act("context-menu"); });
document.addEventListener("mousemove", event => pointerInteraction(Boolean(drag || event.target.closest("#pet, .notice, #error:not([hidden])"))));
document.addEventListener("mouseleave", () => { if (!drag) pointerInteraction(false); });
media.addEventListener("change", animate);
window.miniPet.onState(next => { void apply(next).catch(showError); });
window.miniPet.onError(showError);
void window.miniPet.getState().then(apply).catch(showError);
