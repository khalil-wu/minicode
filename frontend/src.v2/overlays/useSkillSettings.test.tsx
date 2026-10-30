/* @vitest-environment jsdom */
import { act, cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSkillSettings } from "./useSkillSettings";
import { useAppStore } from "../stores";
import type { SettingsSkill } from "./SkillDetailsDialog";
import { captureExtensionSettingsOwner, publishExtensionSettingsChange } from "../lib/extension-settings-events";
import * as draftSelections from "../stores/conversation-drafts";
import { handleCommandCatalogEvent } from "../chat/commandCatalogEvents";
import type { ClientCommand } from "../protocol/events";
import { SkillsTab } from "./SkillsTab";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), toast: vi.fn(), send: vi.fn(), runtimeCatalog: vi.fn(), base: "http://test.local", token: "owner-token" }));
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { writable: true,
  value: vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) }));
vi.mock("../protocol/api", async original => ({ ...await original<typeof import("../protocol/api")>(),
  apiBase: () => mocks.base,
  authHeaders: (headers?: HeadersInit) => { const value = new Headers(headers); value.set("X-MiniCode-Token", mocks.token); return value; },
  fetchWithTimeout: mocks.fetch }));
vi.mock("./ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("../protocol/ws-outbox", async original => ({ ...await original<typeof import("../protocol/ws-outbox")>(), sendClientCommand: mocks.send }));

const user: SettingsSkill = { name: "user-skill", display_name: "User skill", description: "User instructions", path: "C:/skills/user/SKILL.md", source_level: "user", enabled: true, uninstall_name: "user-directory" };
const plugin: SettingsSkill = { name: "plugin:review", description: "Plugin instructions", path: "C:/plugins/review/SKILL.md", source_level: "plugin", enabled: true };
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
const deferred = () => { let resolve!: (value: Response) => void; const promise = new Promise<Response>(done => { resolve = done; }); return { promise, resolve }; };
type Operation = "user-toggle" | "plugin-toggle" | "uninstall";
const payload = (root: string, changed = false, operation: Operation = "user-toggle") => {
  const skills = operation === "uninstall" && changed ? [] : [{ ...user, enabled: operation === "user-toggle" ? !changed : true }];
  const pluginSkills = [{ ...plugin, enabled: operation === "plugin-toggle" ? !changed : true }];
  return { workspace_root: root, skills, plugin_skills: pluginSkills,
    available_skills: [...skills, ...pluginSkills].filter(skill => skill.enabled).concat({ name: root + "-local", description: "Current project", path: root + "/local/SKILL.md", source_level: "workspace", enabled: true }) };
};

beforeEach(() => {
  vi.clearAllMocks(); mocks.base = "http://test.local"; mocks.token = "owner-token";
  mocks.runtimeCatalog.mockImplementation((root: string) => payload(root).available_skills);
  mocks.send.mockImplementation((command: ClientCommand) => {
    if (command.type === "skills.list") handleCommandCatalogEvent({ type: "skills.list", conversation_id: command.conversation_id || "",
      workspace_root: command.workspace_root || "", skills: mocks.runtimeCatalog(command.workspace_root) });
    return true;
  });
  useAppStore.setState({ workingDirectory: "C:/A", conversationId: "owner-a", activeConversationDraftId: null,
    availableSkills: payload("C:/A").available_skills, draft: "Keep draft", selectedSkills: [user], marketplaceSkills: [{ name: "user-directory", title: "User", description: "Installed", triggers: [], installed: true }] });
});
afterEach(cleanup);

it.each([null, "draft-current"])("refreshes an open inventory page from skills/changed and defers hidden reads (draft=%s)", async activeConversationDraftId => {
  const invalidate = vi.spyOn(draftSelections, "invalidateConversationDraftSelection").mockResolvedValue(true);
  const executable = [{ name: "executor-only", description: "Conversation-owned", path: "C:/runtime/SKILL.md" }];
  useAppStore.setState({ activeConversationDraftId, availableSkills: executable });
  mocks.runtimeCatalog.mockReturnValue(executable);
  let name = "User skill";
  mocks.fetch.mockImplementation(async () => response({ ...payload("C:/A"), skills: [{ ...user, display_name: name }] }));
  try {
    const view = render(<SkillsTab active />);
    await screen.findByRole("button", { name: "查看技能详情 User skill" });
    name = "Changed externally";
    act(() => { handleCommandCatalogEvent({ type: "skills/changed" }); });
    await screen.findByRole("button", { name: "查看技能详情 Changed externally" });
    expect(mocks.fetch).toHaveBeenCalledTimes(2);
    expect(mocks.fetch).toHaveBeenLastCalledWith("http://test.local/api/skills/settings?workspace_root=C%3A%2FA",
      expect.objectContaining({ cache: "no-store", signal: expect.any(AbortSignal) }));
    expect(useAppStore.getState().availableSkills).toMatchObject(executable);
    if (activeConversationDraftId) {
      expect(invalidate).toHaveBeenCalledExactlyOnceWith(activeConversationDraftId);
      expect(mocks.send).not.toHaveBeenCalled();
    } else expect(mocks.send).toHaveBeenCalledWith({ type: "skills.list", conversation_id: "owner-a", workspace_root: "C:/A" }, { silent: true });
    view.rerender(<SkillsTab active={false} />);
    name = "Changed while hidden";
    await act(async () => { handleCommandCatalogEvent({ type: "skills/changed" }); });
    expect(mocks.fetch).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "查看技能详情 Changed while hidden" })).toBeNull();
    view.rerender(<SkillsTab active />);
    await screen.findByRole("button", { name: "查看技能详情 Changed while hidden" });
    expect(mocks.fetch).toHaveBeenCalledTimes(3);
    expect(useAppStore.getState().draft).toBe("Keep draft");
  } finally { invalidate.mockRestore(); }
});

it("refreshes the Marketplace inventory with its existing include-projects GET on a server notification", async () => {
  let changed = false;
  mocks.fetch.mockImplementation(async () => response(payload("C:/A", changed)));
  const view = renderHook(() => useSkillSettings(true, undefined, true));
  await waitFor(() => expect(view.result.current.skills).toHaveLength(1));
  changed = true;
  act(() => { handleCommandCatalogEvent({ type: "skills/changed" }); });
  await waitFor(() => expect(view.result.current.skills[0].enabled).toBe(false));
  expect(mocks.fetch).toHaveBeenCalledTimes(2);
  expect(mocks.fetch.mock.calls.every(([url, init]) => url === "http://test.local/api/skills/settings?workspace_root=C%3A%2FA&include_projects=true"
    && !init.method && init.cache === "no-store")).toBe(true);
});

it.each(["workspace", "base", "token"] as const)("keeps a notification-triggered old read out of the new %s owner", async field => {
  const old = deferred();
  let reads = 0;
  const current = () => ({ ...payload(useAppStore.getState().workingDirectory), skills: [{ ...user, display_name: `Current ${field}` }] });
  mocks.fetch.mockImplementation(async () => ++reads === 2 ? old.promise : response(reads === 1 ? payload("C:/A") : current()));
  const view = renderHook(() => useSkillSettings(true));
  await waitFor(() => expect(view.result.current.skills[0]?.display_name).toBe("User skill"));
  act(() => { handleCommandCatalogEvent({ type: "skills/changed" }); });
  const obsoleteSignal = mocks.fetch.mock.calls[1][1].signal as AbortSignal;
  if (field === "workspace") act(() => useAppStore.setState({ workingDirectory: "C:/B", conversationId: "owner-b" }));
  else if (field === "base") mocks.base = "http://new-backend.test";
  else mocks.token = "new-token";
  act(() => { handleCommandCatalogEvent({ type: "skills/changed" }); });
  await waitFor(() => expect(view.result.current.skills[0]?.display_name).toBe(`Current ${field}`));
  expect(obsoleteSignal.aborted).toBe(true);
  await act(async () => old.resolve(response({ ...payload("C:/A"), skills: [{ ...user, display_name: "Obsolete inventory" }] })));
  expect(view.result.current.skills[0].display_name).toBe(`Current ${field}`);
  expect(view.result.current.error).toBe("");
  expect(view.result.current.loading).toBe(false);
  const palette = useAppStore.getState().availableSkills;
  act(() => { handleCommandCatalogEvent({ type: "skills.list", conversation_id: "old-owner", workspace_root: "C:/A",
    skills: [{ name: "obsolete-executor", description: "Old scope" }] }); });
  expect(useAppStore.getState().availableSkills).toBe(palette);
  expect(mocks.fetch.mock.calls.at(-1)?.[0]).toContain(`${mocks.base}/api/skills/settings?workspace_root=${encodeURIComponent(useAppStore.getState().workingDirectory)}`);
  expect(new Headers(mocks.fetch.mock.calls.at(-1)?.[1].headers).get("X-MiniCode-Token")).toBe(mocks.token);
});

it.each((["old-before", "old-after", "return-c"] as const).flatMap(order =>
  (["user-toggle", "plugin-toggle", "uninstall"] as const).map(operation => ({ order, operation }))))(
  "rebuilds the current scope after a global Skill operation ($order/$operation)", async ({ order, operation }) => {
    useAppStore.setState({ workingDirectory: "C:/A", conversationId: "owner-a", availableSkills: payload("C:/A", false, operation).available_skills });
    mocks.runtimeCatalog.mockImplementation((root: string) => payload(root, true, operation).available_skills);
    const write = deferred(), oldB = deferred(), newB = deferred(), oldC = deferred(), newC = deferred();
    let bReads = 0, cReads = 0, writes = 0;
    mocks.fetch.mockImplementation((input: string, init?: RequestInit) => {
      if (init?.method === "PUT" || init?.method === "DELETE") { writes++; return write.promise; }
      const root = new URL(input).searchParams.get("workspace_root")!;
      if (root === "C:/B") return ++bReads === 1 ? oldB.promise : newB.promise;
      if (root === "C:/C") return ++cReads === 1 ? oldC.promise : newC.promise;
      return Promise.resolve(response(payload(root, false, operation)));
    });
    const view = renderHook(() => useSkillSettings(true));
    await waitFor(() => expect(view.result.current.skills).toHaveLength(1));
    let mutation!: Promise<unknown>;
    act(() => { mutation = operation === "uninstall" ? view.result.current.uninstall(user) : view.result.current.toggle(operation === "plugin-toggle" ? plugin : user); });
    if (order === "return-c") {
      act(() => useAppStore.setState({ workingDirectory: "C:/C", conversationId: "owner-c", availableSkills: payload("C:/C", false, operation).available_skills }));
      await waitFor(() => expect(cReads).toBe(1));
    }
    act(() => useAppStore.setState({ workingDirectory: "C:/B", conversationId: "owner-b", availableSkills: payload("C:/B", false, operation).available_skills }));
    await waitFor(() => expect(bReads).toBe(1));
    if (order === "old-before") await act(async () => oldB.resolve(response(payload("C:/B", false, operation))));
    await act(async () => write.resolve(response(payload("C:/A", true, operation))));
    await waitFor(() => expect(bReads).toBe(2));
    if (order === "return-c") {
      act(() => {
        useAppStore.setState({ workingDirectory: "C:/C", conversationId: "owner-c" });
        handleCommandCatalogEvent({ type: "skills.list", conversation_id: "owner-c", workspace_root: "C:/C", skills: payload("C:/C", true, operation).available_skills });
      });
      await waitFor(() => expect(cReads).toBe(2));
      await act(async () => newC.resolve(response(payload("C:/C", true, operation))));
      await act(async () => { oldC.resolve(response(payload("C:/C", false, operation))); oldB.resolve(response(payload("C:/B", false, operation))); newB.resolve(response(payload("C:/B", true, operation))); });
    } else {
      expect(view.result.current.pendingPath).toBe(operation === "plugin-toggle" ? plugin.path : user.path);
      await act(async () => { await view.result.current.toggle(user); });
      expect(writes).toBe(1);
      await act(async () => newB.resolve(response(payload("C:/B", true, operation))));
      if (order === "old-after") await act(async () => oldB.resolve(response(payload("C:/B", false, operation))));
    }
    await act(async () => { await mutation; });
    const finalRoot = order === "return-c" ? "C:/C" : "C:/B";
    await waitFor(() => expect(useAppStore.getState().availableSkills.some(skill => skill.name === finalRoot + "-local")).toBe(true));
    expect(useAppStore.getState().availableSkills.some(skill => skill.name === "C:/A-local")).toBe(false);
    expect(view.result.current.skills).toHaveLength(operation === "uninstall" ? 0 : 1);
    if (operation === "user-toggle") expect(view.result.current.skills[0].enabled).toBe(false);
    if (operation === "plugin-toggle") expect(view.result.current.pluginSkills[0].enabled).toBe(false);
    expect(writes).toBe(1);
    expect(mocks.send).toHaveBeenCalledWith({ type: "skills.list", conversation_id: "owner-b", workspace_root: "C:/B" }, { silent: true });
    expect(useAppStore.getState().draft).toBe("Keep draft");
    expect(useAppStore.getState().selectedSkills).toEqual([user]);
    view.unmount();
});

it.each([false, true])("does not let an old same-scope read replace committed inventory (old read fails=%s)", async failOld => {
  mocks.runtimeCatalog.mockImplementation((root: string) => payload(root, true).available_skills);
  const old = deferred(), fresh = deferred();
  let reads = 0, writes = 0;
  mocks.fetch.mockImplementation((_input: string, init?: RequestInit) => {
    if (init?.method === "PUT") { writes++; return Promise.resolve(response(payload("C:/A", true))); }
    reads++;
    return reads === 1 ? Promise.resolve(response(payload("C:/A"))) : reads === 2 ? old.promise : fresh.promise;
  });
  const view = renderHook(() => useSkillSettings(true));
  await waitFor(() => expect(view.result.current.skills).toHaveLength(1));
  act(() => { void view.result.current.reload(); });
  let write!: Promise<void>;
  act(() => { write = view.result.current.toggle(user); });
  await waitFor(() => expect(reads).toBe(3));
  expect(view.result.current.loading).toBe(true);
  await act(async () => { await view.result.current.toggle(user); });
  expect(writes).toBe(1);
  await act(async () => fresh.resolve(response(payload("C:/A", true))));
  await act(async () => { await write; old.resolve(failOld ? new Response("obsolete read failure", { status: 503 }) : response(payload("C:/A"))); });
  expect(view.result.current.skills[0].enabled).toBe(false);
  expect(view.result.current.error).toBe("");
  expect(view.result.current.loading).toBe(false);
  expect(view.result.current.pendingPath).toBe("");
  expect(useAppStore.getState().availableSkills.some(skill => skill.path === user.path)).toBe(false);
});

it("refreshes two mounted consumers when an older PUT reply arrives after the newer committed projection", async () => {
  let enabled = true, writes = 0, reads = 0;
  const firstReply = deferred();
  const current = () => ({ ...payload("C:/A"), skills: [{ ...user, enabled }], available_skills: enabled ? [user, plugin] : [plugin] });
  mocks.runtimeCatalog.mockImplementation(() => current().available_skills);
  mocks.fetch.mockImplementation((_input: string, init?: RequestInit) => {
    if (init?.method === "PUT") {
      writes++; enabled = JSON.parse(String(init.body)).enabled;
      return writes === 1 ? firstReply.promise : Promise.resolve(response(current()));
    }
    reads++; return Promise.resolve(response(current()));
  });
  const first = renderHook(() => useSkillSettings(true));
  const second = renderHook(() => useSkillSettings(true));
  await waitFor(() => expect(first.result.current.skills).toHaveLength(1));
  await waitFor(() => expect(second.result.current.skills).toHaveLength(1));
  let oldWrite!: Promise<void>;
  act(() => { oldWrite = first.result.current.toggle(first.result.current.skills[0]); });
  await act(async () => { await second.result.current.reload(); });
  expect(second.result.current.skills[0].enabled).toBe(false);
  await act(async () => { await second.result.current.toggle(second.result.current.skills[0]); });
  expect(first.result.current.skills[0].enabled).toBe(true);
  expect(second.result.current.skills[0].enabled).toBe(true);
  await act(async () => { firstReply.resolve(response(payload("C:/A", true))); await oldWrite; });
  expect(first.result.current.skills[0].enabled).toBe(true);
  expect(second.result.current.skills[0].enabled).toBe(true);
  expect(useAppStore.getState().availableSkills.some(skill => skill.path === user.path)).toBe(true);
  expect(writes).toBe(2); expect(reads).toBe(7);
  expect(mocks.send).toHaveBeenCalledTimes(2);
});

it("keeps a committed uninstall successful when its read fails and retries only the inventory", async () => {
  mocks.runtimeCatalog.mockImplementation((root: string) => payload(root, true, "uninstall").available_skills);
  let reads = 0, deletes = 0;
  mocks.fetch.mockImplementation((_input: string, init?: RequestInit) => {
    if (init?.method === "DELETE") { deletes++; return Promise.resolve(response({ removed: true })); }
    reads++; return Promise.resolve(reads === 2 ? new Response("Actual inventory read failure", { status: 503 }) : response(payload("C:/A", reads > 2, "uninstall")));
  });
  const view = renderHook(() => useSkillSettings(true));
  await waitFor(() => expect(view.result.current.skills).toHaveLength(1));
  let removed: boolean | undefined;
  await act(async () => { removed = await view.result.current.uninstall(user); });
  expect(removed).toBe(true); expect(deletes).toBe(1);
  expect(view.result.current.error).toContain("Actual inventory read failure");
  expect(view.result.current.pendingPath).toBe("");
  expect(mocks.toast).not.toHaveBeenCalled();
  await act(async () => { await view.result.current.reload(); });
  expect(view.result.current.skills).toHaveLength(0);
  expect(deletes).toBe(1); expect(reads).toBe(3);
  expect(useAppStore.getState().marketplaceSkills[0].installed).toBe(false);
});

it("shows the persisted but overridden setting without pretending its effective switch changed", async () => {
  mocks.fetch.mockImplementation(() => Promise.resolve(response(payload("C:/A"))));
  const view = renderHook(() => useSkillSettings(true));
  await waitFor(() => expect(view.result.current.skills).toHaveLength(1));
  await act(async () => { await view.result.current.toggle(user); });
  expect(view.result.current.skills[0].enabled).toBe(true);
  expect(mocks.toast).toHaveBeenCalledWith("技能设置已保存，但当前配置尚未生效。", "warning");
});

it("does not borrow settings inventory for an active draft's provider projection", async () => {
  const refresh = vi.spyOn(draftSelections, "invalidateConversationDraftSelection").mockResolvedValue(true);
  useAppStore.setState({ activeConversationDraftId: "draft-current", availableSkills: [],
    currentModel: "draft-model", currentProvider: "draft-provider" });
  let enabled = true;
  mocks.fetch.mockImplementation((_input: string, init?: RequestInit) => {
    if (init?.method === "PUT") enabled = false;
    return Promise.resolve(response(payload("C:/A", !enabled)));
  });
  try {
    const view = renderHook(() => useSkillSettings(true));
    await waitFor(() => expect(view.result.current.skills).toHaveLength(1));
    expect(useAppStore.getState().availableSkills).toEqual([]);
    await act(async () => { await view.result.current.toggle(user); });
    expect(view.result.current.skills[0].enabled).toBe(false);
    expect(useAppStore.getState().availableSkills).toEqual([]);
    expect(refresh).toHaveBeenCalledTimes(1); expect(refresh).toHaveBeenCalledWith("draft-current");
    expect(mocks.send).not.toHaveBeenCalled();
    expect(useAppStore.getState().currentModel).toBe("draft-model");
    expect(useAppStore.getState().currentProvider).toBe("draft-provider");
  } finally { refresh.mockRestore(); useAppStore.setState({ activeConversationDraftId: null }); }
});

it("keeps the session catalog authoritative when settings and wrong-owner replies contain different skills", async () => {
  const runtime = [{ name: "executor-role-skill", description: "Session-owned", path: "C:/runtime/role/SKILL.md" }];
  const bootstrap = [{ name: "bootstrap-only", description: "Settings snapshot", path: "C:/bootstrap/SKILL.md" }];
  useAppStore.setState({ availableSkills: runtime });
  let enabled = true;
  mocks.fetch.mockImplementation((_input: string, init?: RequestInit) => {
    if (init?.method === "PUT") enabled = false;
    return Promise.resolve(response({ ...payload("C:/A", !enabled), available_skills: bootstrap }));
  });
  mocks.send.mockImplementation((command: ClientCommand) => {
    if (command.type === "skills.list") {
      handleCommandCatalogEvent({ type: "skills.list", conversation_id: "wrong-owner", workspace_root: "C:/A", skills: bootstrap });
      handleCommandCatalogEvent({ type: "skills.list", conversation_id: "owner-a", workspace_root: "C:/wrong", skills: bootstrap });
    }
    return true;
  });
  const view = renderHook(() => useSkillSettings(true));
  await waitFor(() => expect(view.result.current.skills).toHaveLength(1));
  expect(useAppStore.getState().availableSkills).toEqual(runtime);
  await act(async () => { await view.result.current.toggle(user); });
  expect(view.result.current.skills[0].enabled).toBe(false);
  expect(mocks.send).toHaveBeenCalledTimes(1);
  expect(mocks.send).toHaveBeenCalledWith({ type: "skills.list", conversation_id: "owner-a", workspace_root: "C:/A" }, { silent: true });
  expect(useAppStore.getState().availableSkills).toEqual(runtime);
  act(() => { handleCommandCatalogEvent({ type: "skills.list", conversation_id: "owner-a", workspace_root: "C:/A", skills: [{ ...runtime[0], description: "Refreshed by session" }] }); });
  expect(useAppStore.getState().availableSkills).toMatchObject([{ name: "executor-role-skill", description: "Refreshed by session" }]);
});

it.each(["base", "token"] as const)("ignores another backend's commit signal and late write receipt (%s changed)", async field => {
  const oldOwner = captureExtensionSettingsOwner(), reply = deferred();
  let writes = 0, reads = 0;
  mocks.fetch.mockImplementation((input: string, init?: RequestInit) => {
    if (init?.method === "PUT") { writes++; return reply.promise; }
    reads++; return Promise.resolve(response(payload(new URL(input).searchParams.get("workspace_root")!)));
  });
  const view = renderHook(() => useSkillSettings(true));
  await waitFor(() => expect(view.result.current.skills).toHaveLength(1));
  let mutation!: Promise<void>;
  act(() => { mutation = view.result.current.toggle(user); });
  if (field === "base") mocks.base = "http://new-backend.test"; else mocks.token = "new-token";
  act(() => { publishExtensionSettingsChange("skills", oldOwner, {}); });
  await act(async () => { reply.resolve(response(payload("C:/A", true))); await mutation; });
  expect(reads).toBe(1); expect(writes).toBe(1);
  expect(view.result.current.skills[0].enabled).toBe(true);
  expect(useAppStore.getState().availableSkills.some(skill => skill.path === user.path)).toBe(true);
});

it("notifies the surviving consumer after the submitting component unmounts", async () => {
  const reply = deferred(); let enabled = true;
  mocks.runtimeCatalog.mockImplementation((root: string) => payload(root, !enabled).available_skills);
  mocks.fetch.mockImplementation((_input: string, init?: RequestInit) => init?.method === "PUT" ? reply.promise : Promise.resolve(response(payload("C:/A", !enabled))));
  const first = renderHook(() => useSkillSettings(true)), second = renderHook(() => useSkillSettings(true));
  await waitFor(() => expect(first.result.current.skills).toHaveLength(1));
  await waitFor(() => expect(second.result.current.skills).toHaveLength(1));
  let mutation!: Promise<void>;
  act(() => { mutation = first.result.current.toggle(user); });
  first.unmount(); enabled = false;
  await act(async () => { reply.resolve(response(payload("C:/A", true))); await mutation; });
  await waitFor(() => expect(second.result.current.skills[0].enabled).toBe(false));
});
