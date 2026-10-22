import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

// Called only by the isolated production-desktop CI audit. Fixtures are real
// files; renderer setup uses existing public actions, never injected snapshots.
export async function auditProjectConfig({ page, app, workspace, profile, capture, checks, wire }) {
  assert.equal(process.env.CI, "true");
  const projectFile = path.join(workspace, ".minicode", "config.toml");
  const userFile = path.join(profile, "settings.json");
  const trustFile = path.join(profile, "data", "trusted_workspaces.json");
  const initialProject = await readFile(projectFile, "utf8");
  const initialUser = await readFile(userFile, "utf8");
  const comments = initialProject.split(/\r?\n/).filter(line => line.trimStart().startsWith("#"));
  assert(comments.length > 0, "The real project fixture must contain preserved comments");
  const scope = page.getByRole("button", { name: /^配置作用域，当前：/ });
  const verbosity = page.getByRole("button", { name: /^回复详细程度，当前：/ });
  const currentId = () => page.locator(".config-scope-select select").inputValue();
  const assertProjectFormatting = text => {
    for (const comment of comments) assert(text.includes(comment), `Project comment was lost: ${comment}`);
    assert(/^\s*network_access\s*=\s*true\b/m.test(text), "Unrelated project network access must survive verbosity edits");
  };
  const writeVerbosity = async (label, scopeId) => {
    await verbosity.click();
    const [response] = await Promise.all([
      page.waitForResponse(response => response.url().includes("/api/settings/agent-config")
        && response.request().method() === "PUT" && response.request().postDataJSON().scope_id === scopeId),
      page.getByRole("option", { name: label, exact: true }).click(),
    ]);
    assert(response.ok(), await response.text());
    return response.json();
  };
  const showConfig = async () => {
    await page.evaluate(() => {
      const state = window.__zustandStore.getState();
      state.setSettingsTab("config");
      if (!state.settingsOpen) state.toggleSettings();
    });
    await scope.waitFor();
    await page.waitForFunction(() => !document.querySelector(".config-scope-select .mc-select-trigger").disabled);
  };
  const hideSettings = () => page.evaluate(() => {
    const state = window.__zustandStore.getState();
    if (state.settingsOpen) state.toggleSettings();
  });
  const waitForFile = async file => {
    await page.waitForFunction(target => {
      const normalize = value => value.replaceAll("\\", "/");
      return window.__zustandStore.getState().editorTabs.some(tab => normalize(tab.path) === normalize(target) && !tab.loading);
    }, file);
    const tab = await page.evaluate(target => {
      const normalize = value => value.replaceAll("\\", "/");
      return window.__zustandStore.getState().editorTabs.find(tab => normalize(tab.path) === normalize(target));
    }, file);
    assert.equal(tab.error ?? null, null);
    return tab;
  };
  const snapshot = () => page.evaluate(() => {
    const state = window.__zustandStore.getState();
    return { workspace: state.workingDirectory, conversation: state.conversationId, draft: state.draft,
      attachments: state.attachments, hiddenDrafts: state.workspaceComposerDrafts,
      tabs: state.editorTabs.map(({ id, path, content, original, contentHash }) => ({ id, path, content, original, contentHash })) };
  });
  const assertRetained = (before, after) => {
    assert.equal(after.workspace, before.workspace);
    assert.equal(after.conversation, before.conversation);
    assert.equal(after.draft, before.draft);
    assert.deepEqual(after.attachments, before.attachments);
    assert.deepEqual(after.hiddenDrafts, before.hiddenDrafts);
    for (const tab of before.tabs) assert.deepEqual(after.tabs.find(item => item.id === tab.id), tab);
  };

  await scope.click();
  await page.getByRole("option", { name: path.basename(workspace), exact: true }).waitFor();
  const scopeMenu = page.getByRole("listbox", { name: "配置作用域", exact: true });
  assert.equal(await scopeMenu.getByText("项目", { exact: true }).count(), 1);
  assert.equal(await scopeMenu.getByText("全局", { exact: true }).count(), 1);
  await capture("settings-06-project-scope-menu");
  await page.getByRole("option", { name: path.basename(workspace), exact: true }).click();
  const projectId = await currentId();
  const saved = await writeVerbosity("高", projectId);
  assert.equal(path.resolve(saved.write.path), projectFile);
  assert.equal(saved.scopes.find(item => item.id === projectId).values.model_verbosity, "high");
  let projectText = await readFile(projectFile, "utf8");
  assert(/^\s*model_verbosity\s*=\s*["']high["']/m.test(projectText));
  assertProjectFormatting(projectText);
  assert.equal(await readFile(userFile, "utf8"), initialUser);
  await page.getByRole("button", { name: "回复详细程度，当前：高", exact: true }).waitFor();
  await capture("settings-06-project-saved", { scopeId: projectId, file: saved.write.path });
  const unset = await writeVerbosity("模型默认值", projectId);
  assert.equal(unset.write.fields.model_verbosity.requested_value, null);
  assert.equal(Object.hasOwn(unset.scopes.find(item => item.id === projectId).values, "model_verbosity"), false);
  projectText = await readFile(projectFile, "utf8");
  assert.equal(/^\s*model_verbosity\s*=/m.test(projectText), false);
  assertProjectFormatting(projectText);
  assert.equal(await readFile(userFile, "utf8"), initialUser);
  await page.getByRole("button", { name: "回复详细程度，当前：模型默认值", exact: true }).waitFor();
  await capture("settings-06-project-unset");
  checks.push("Project scope writes and unsets the actual TOML field, retains comments and unrelated network access, and leaves the real User file unchanged");

  const initialTrust = await readFile(trustFile, "utf8");
  await writeFile(trustFile, JSON.stringify({ ...JSON.parse(initialTrust), roots: [] }));
  await page.locator(".settings-center-tab").filter({ hasText: "常规" }).click();
  const [disabledRequest] = await Promise.all([
    page.waitForRequest(request => {
      const url = new URL(request.url());
      return request.method() === "GET" && url.pathname === "/api/settings/agent-config"
        && url.searchParams.get("workspace_root") === workspace;
    }),
    page.locator(".settings-center-tab").filter({ hasText: "配置" }).click(),
  ]);
  const disabledResponse = await disabledRequest.response();
  assert(disabledResponse);
  assert.equal(await disabledResponse.finished(), null);
  assert.equal(disabledResponse.status(), 200);
  const disabled = (await disabledResponse.json()).scopes.find(item => item.id === projectId);
  assert(disabled.disabled_reason);
  const warning = page.locator(".config-scope-warning");
  await warning.waitFor();
  assert.equal(await warning.innerText(), disabled.disabled_reason);
  for (const name of ["审批策略", "沙箱模式", "网页搜索", "回复详细程度", "推理摘要"]) {
    assert.equal(await page.getByRole("button", { name: new RegExp(`^${name}，当前：`) }).isDisabled(), true);
  }
  assert.equal(await page.getByRole("switch", { name: "允许网络访问" }).isDisabled(), true);
  assert.equal(await page.getByRole("button", { name: "打开配置文件", exact: true }).isDisabled(), false);
  await capture("settings-06-project-disabled", { disabledReason: disabled.disabled_reason });
  await writeFile(trustFile, initialTrust);
  await page.locator(".settings-center-tab").filter({ hasText: "常规" }).click();
  await page.locator(".settings-center-tab").filter({ hasText: "配置" }).click();
  await warning.waitFor({ state: "hidden" });
  checks.push("An actual revoked CI trust ledger produces the original project disabled reason and disabled controls while the registered file opener remains available; the ledger is restored");

  const originalConversation = await page.evaluate(() => window.__zustandStore.getState().conversationId);
  const child = path.join(workspace, "project-child"), childConfig = path.join(child, ".minicode", "config.toml");
  const dirtyFile = path.join(child, "keep-unsaved.txt");
  await mkdir(path.dirname(childConfig), { recursive: true });
  await writeFile(childConfig, "# Child project config for the actual ancestor-open flow\n[sandbox_workspace_write]\nnetwork_access = true\n");
  await writeFile(dirtyFile, "CI disk content must remain unchanged\n");
  await hideSettings();
  await page.evaluate(workspaceRoot => window.__zustandStore.getState().createConversation({ bindWorkspace: true, workspaceRoot, appMode: "cowork" }), child);
  await page.waitForFunction(({ root, previous }) => {
    const state = window.__zustandStore.getState();
    return state.conversationId !== previous && state.workingDirectory.replaceAll("\\", "/") === root.replaceAll("\\", "/");
  }, { root: child, previous: originalConversation });
  await page.locator(".composer-input-region [data-composer-input]:visible").first().fill("KEEP_CHILD_PROJECT_DRAFT");
  await page.evaluate(file => window.__zustandStore.getState().openEditorFile(file, undefined, { exact: true, preview: false }), dirtyFile);
  const dirtyTab = await waitForFile(path.relative(child, dirtyFile));
  await page.evaluate(({ file, content }) => window.__zustandStore.getState().updateTabContent(file, content), { file: dirtyTab.path, content: "CI unsaved editor buffer must survive registered opening\n" });
  await showConfig();
  assert.equal(await scope.innerText(), path.basename(child));
  await scope.click();
  await capture("settings-06-project-ancestor-menu");
  const ancestor = page.getByRole("option", { name: path.basename(workspace), exact: true });
  assert.equal(path.resolve(await ancestor.getAttribute("title")), projectFile);
  await ancestor.click();
  const beforeOpen = await snapshot();
  await page.getByRole("button", { name: "打开配置文件", exact: true }).click();
  await hideSettings();
  const opened = await waitForFile(projectFile);
  assert.equal(opened.content, projectText);
  assert(opened.workspaceOwner.session_id, "The registered config must retain its actual backend session");
  assert.deepEqual(opened.workspaceOwner, { session_id: opened.workspaceOwner.session_id, conversation_id: "" });
  assertRetained(beforeOpen, await snapshot());
  assert.equal(await readFile(dirtyFile, "utf8"), "CI disk content must remain unchanged\n");
  await capture("settings-06-project-ancestor-open", { ownerWorkspace: child, fileWorkspace: workspace, file: projectFile });

  const saveEditor = async contents => {
    await page.evaluate(({ file, content }) => window.__zustandStore.getState().updateTabContent(file, content), { file: opened.path, content: contents });
    const [response] = await Promise.all([
      page.waitForResponse(response => response.url().includes("/api/workspace/file/compare-write") && response.request().method() === "PUT"),
      page.evaluate(() => window.dispatchEvent(new Event("editor:save"))),
    ]);
    assert(response.ok(), await response.text());
    const request = new URL(response.url());
    assert.equal(request.searchParams.get("conversation_id"), "");
    assert.equal(request.searchParams.get("session_id"), opened.workspaceOwner.session_id);
    await page.waitForFunction(({ file, content }) => window.__zustandStore.getState().editorTabs.some(tab => tab.path === file && tab.original === content), { file: opened.path, content: contents });
    assert.equal(await readFile(projectFile, "utf8"), contents);
  };
  await saveEditor("[ # temporary invalid project TOML to verify editor repair\n");
  await capture("settings-06-project-editor-invalid");
  await saveEditor(projectText);
  await capture("settings-06-project-editor-repaired");
  assert.equal(await readFile(userFile, "utf8"), initialUser);
  assert.equal(await readFile(dirtyFile, "utf8"), "CI disk content must remain unchanged\n");
  checks.push({ scenario: "Registered ancestor config opens and repairs invalid TOML through its real session editor context, without switching workspace or changing the existing dirty buffer and chat draft", file: projectFile, workspace: child, processId: app.process().pid });
  const restoreAt = wire.length;
  await page.evaluate(id => window.__zustandStore.getState().requestConversationSwitch(id), originalConversation);
  await page.waitForFunction(({ id, root }) => {
    const state = window.__zustandStore.getState();
    return state.conversationId === id && state.pendingConversationSwitchId === null
      && state.workingDirectory.replaceAll("\\", "/") === root.replaceAll("\\", "/");
  }, { id: originalConversation, root: workspace });
  assert(wire.slice(restoreAt).some(row => row.direction === "server" && row.message.type === "conversation.switched"
    && row.message.conversation_id === originalConversation && row.message.client_command_type === "conversation.switch"));
  // Observe the real final reopen without retries: a prior run lost its scope
  // menu here. Keep the event order to distinguish focus/scroll from config load.
  await page.evaluate(() => {
    const events = [];
    const describe = node => node instanceof Element ? { tag: node.tagName, className: node.className,
      role: node.getAttribute("role"), label: node.getAttribute("aria-label") } : { name: node?.constructor.name };
    const record = (kind, details = {}) => events.push({ at: performance.now(), kind, ...details,
      active: describe(document.activeElement), scrollTop: document.querySelector(".settings-center-main").scrollTop,
      open: document.querySelector(".config-scope-select").dataset.open,
      triggerDisabled: document.querySelector(".config-scope-select .mc-select-trigger").disabled,
      fieldDisabled: document.querySelector('[aria-label^="回复详细程度，当前："]').disabled });
    const onEvent = event => record(event.type, { target: describe(event.target), related: describe(event.relatedTarget),
      path: event.composedPath().slice(0, 5).map(describe) });
    const kinds = ["pointerdown", "pointerup", "click", "focusin", "focusout", "scroll", "resize"];
    for (const kind of kinds) window.addEventListener(kind, onEvent, true);
    const observer = new MutationObserver(changes => {
      for (const change of changes) if (change.target.matches(".config-scope-select, .config-scope-select .mc-select-trigger")) {
        record("mutation", { target: describe(change.target), attribute: change.attributeName,
          before: change.oldValue, after: change.target.getAttribute(change.attributeName) });
      }
    });
    observer.observe(document.querySelector(".config-scope-select"), { attributes: true, subtree: true,
      attributeOldValue: true, attributeFilter: ["data-open", "disabled"] });
    record("before-reopen");
    window.__finishConfigReopenAudit = () => {
      record("finished");
      for (const kind of kinds) window.removeEventListener(kind, onEvent, true);
      observer.disconnect();
      delete window.__finishConfigReopenAudit;
      return events;
    };
  });
  try {
    await showConfig();
    await scope.click();
    await page.getByRole("option", { name: "用户", exact: true }).click();
  } finally {
    checks.push({ scenario: "Actual final Configuration reopen and scope selection event order",
      events: await page.evaluate(() => window.__finishConfigReopenAudit()) });
  }
}
