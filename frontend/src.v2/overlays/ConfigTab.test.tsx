// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ConfigTab, type AgentConfigFields, type AgentConfigSnapshot } from "./ConfigTab";
import { useAppStore } from "../stores";
import { LanguageProvider } from "../i18n";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), open: vi.fn(), desktop: false,
  preferences: null as { enabledReasoningEfforts: string[] } | null, preferenceError: "", updatePreferences: vi.fn() }));
vi.mock("../protocol/api", async load => ({ ...await load<typeof import("../protocol/api")>(),
  apiBase: () => "http://config.test", authHeaders: () => ({}), fetchWithTimeout: mocks.fetch }));
vi.mock("../desktop/runtime", async load => ({ ...await load<typeof import("../desktop/runtime")>(), isDesktop: () => mocks.desktop, openManagedSettingsTarget: mocks.open }));
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: mocks.preferences, error: mocks.preferenceError, update: mocks.updatePreferences }) }));

const path = "C:/Profile/config.toml";
const defaults: AgentConfigFields = { approval_policy: "on-request", sandbox_mode: "workspace-write", network_access: false,
  web_search: "cached", model_verbosity: "medium", model_reasoning_summary: "auto" };
const snapshot = (effective: Partial<AgentConfigFields> = {}, values: Partial<AgentConfigFields> = {}): AgentConfigSnapshot => ({
  path, default_scope_id: "user", ...defaults, ...effective, effective: { ...defaults, ...effective }, origins: {}, requirements: {}, warnings: [],
  scopes: [{ id: "user", kind: "user", path, version: "user-v1", values, readonly: false,
    workspace_root: null, disabled_reason: null,
    source: { type: "user", file: path, profile: "review", display: `user (${path}, profile=review)` } }],
});
const projectScope = (workspace: string, values: Partial<AgentConfigFields> = {}, disabledReason: string | null = null): AgentConfigSnapshot["scopes"][number] => ({
  id: `project:${workspace}`, kind: "project", path: `${workspace}/.minicode/config.toml`, workspace_root: workspace,
  version: `project-v1:${workspace}`, disabled_reason: disabledReason, values, readonly: false,
  source: { type: "project", display: `project (${workspace}/.minicode/config.toml)` },
});
const projectSnapshot = (workspace: string, values: Partial<AgentConfigFields> = {}, effective: Partial<AgentConfigFields> = {}): AgentConfigSnapshot => {
  const config = snapshot(effective);
  config.scopes.unshift(projectScope(workspace, values));
  config.default_scope_id = config.scopes[0].id;
  return config;
};
const button = (label: string) => screen.getByRole("button", { name: new RegExp(`^${label}，当前：`) });
const choose = (label: string, option: string) => {
  fireEvent.click(button(label));
  fireEvent.click(screen.getByRole("option", { name: new RegExp(`^${option}`) }));
};
const ready = async () => { await screen.findByRole("button", { name: "配置作用域，当前：用户" }); await waitFor(() => expect(button("推理摘要")).toHaveProperty("disabled", false)); };

beforeEach(() => {
  vi.clearAllMocks();
  mocks.desktop = false; mocks.preferences = null; mocks.preferenceError = "";
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  mocks.open.mockResolvedValue(true);
  useAppStore.setState({ workingDirectory: "C:/A", draft: "Unsent chat", attachments: [], availableModelOptions: [] });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("uses raw User/profile values, retains legacy labels and offers only Source approval choices", async () => {
  const config = snapshot({}, { approval_policy: "untrusted", sandbox_mode: "read-only" });
  config.origins.approval_policy = { source: { type: "project", display: "project (C:/A/.codex/config.toml)" }, version: "project-v1" };
  mocks.fetch.mockImplementation(async () => Response.json(config));
  render(<ConfigTab active />); await ready();
  expect(button("审批策略").textContent).toBe("不受信任");
  expect(button("沙箱模式").textContent).toBe("只读");
  expect(screen.queryByRole("switch", { name: "允许网络访问" })).toBeNull();
  fireEvent.click(button("审批策略"));
  expect(screen.getAllByRole("option").map(option => option.querySelector(".mc-select-option-label")?.textContent)).toEqual(["按需", "从不"]);
  expect(screen.getByRole("listbox").style.width).toBe("360px");
  fireEvent.keyDown(screen.getByRole("option", { name: /^按需/ }), { key: "Escape" });
  fireEvent.click(button("配置作用域"));
  expect(screen.getAllByRole("option").map(option => option.textContent)).toEqual(["用户"]);
  expect(screen.getByRole("listbox").style.width).toBe("240px");
  expect(mocks.fetch).toHaveBeenCalledTimes(1);
});

it("shows only actual Admin layers, makes them readonly and locks only managed User fields", async () => {
  const config = snapshot({ model_verbosity: "high" }, { model_verbosity: "low" });
  const source = { type: "mdm", display: "MDM (test-domain:agent-policy)" };
  config.scopes.push({ id: "managed", kind: "managed", path: null, version: "managed-v1", readonly: true,
    workspace_root: null, disabled_reason: null,
    source, values: { web_search: "disabled", sandbox_mode: "workspace-write" } });
  config.origins.model_verbosity = { source, version: "managed-v1" };
  config.requirements.allowed_approval_policies = ["never"];
  config.requirements.allowed_sandbox_modes = [];
  mocks.fetch.mockImplementation(async () => Response.json(config));
  render(<ConfigTab active />); await ready();
  expect(button("回复详细程度")).toHaveProperty("disabled", true);
  expect(button("回复详细程度").textContent).toBe("低");
  expect(button("沙箱模式")).toHaveProperty("disabled", true);
  expect(button("网页搜索")).toHaveProperty("disabled", false);
  expect(screen.getByText("此值由管理员策略管理。")).toBeTruthy();
  choose("审批策略", "从不");
  await waitFor(() => expect(mocks.fetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));
  choose("配置作用域", "管理员");
  for (const name of ["审批策略", "沙箱模式", "网页搜索", "回复详细程度", "推理摘要"]) expect(button(name)).toHaveProperty("disabled", true);
  expect(button("网页搜索").textContent).toBe("已禁用");
  expect(screen.getByRole("button", { name: "打开配置文件" })).toHaveProperty("disabled", true);
  expect(mocks.open).not.toHaveBeenCalled();
});

it("writes Model default as null with the real scope version and waits for the actual result", async () => {
  let finish!: (response: Response) => void;
  mocks.fetch.mockImplementation(async (_url, init) => init?.method === "PUT"
    ? new Promise<Response>(resolve => { finish = resolve; }) : Response.json(snapshot()));
  render(<ConfigTab active />); await ready();
  choose("回复详细程度", "模型默认值");
  expect(button("回复详细程度").textContent).toBe("中");
  expect(button("回复详细程度")).toHaveProperty("disabled", true);
  await waitFor(() => expect(mocks.fetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));
  const request = mocks.fetch.mock.calls.find(([, init]) => init?.method === "PUT")!;
  expect(JSON.parse(request[1].body)).toEqual({ workspace_root: "C:/A", scope_id: "user", expected_version: "user-v1", model_verbosity: null });
  const next = snapshot({ model_verbosity: null });
  next.scopes[0].version = "user-v2";
  next.write = { status: "applied", path, version: "user-v2", fields: { model_verbosity: { requested_value: null, effective_value: null } } };
  await act(async () => finish(Response.json(next)));
  expect(button("回复详细程度").textContent).toBe("模型默认值");
  expect(button("回复详细程度")).toHaveProperty("disabled", false);
  expect(useAppStore.getState().draft).toBe("Unsent chat");
});

it("shows a real overridden receipt while preserving the saved raw User value", async () => {
  const origin = { source: { type: "project", display: "project (C:/A/.codex/config.toml)" }, version: "project-v1" };
  mocks.fetch.mockImplementation(async (_url, init) => {
    if (init?.method !== "PUT") return Response.json(snapshot());
    const next = snapshot({ web_search: "cached" }, { web_search: "live" });
    next.origins.web_search = origin;
    next.write = { status: "overridden", path, version: "user-v2", fields: {
      web_search: { requested_value: "live", effective_value: "cached", overriding_source: origin },
    } };
    return Response.json(next);
  });
  render(<ConfigTab active />); await ready();
  choose("网页搜索", "实时");
  expect((await screen.findByRole("status")).textContent).toContain("已保存，但当前生效值由project (C:/A/.codex/config.toml)覆盖。");
  expect(button("网页搜索").textContent).toBe("实时");
  expect(button("网页搜索")).toHaveProperty("disabled", false);
  expect(screen.queryByRole("alert")).toBeNull();
});

it("keeps the existing form and unrelated draft on a version conflict and a failed subsequent read", async () => {
  let reads = 0;
  mocks.fetch.mockImplementation(async (_url, init) => init?.method === "PUT"
    ? new Response(JSON.stringify({ detail: "Config file changed; reload its version" }), { status: 409 })
    : ++reads <= 2 ? Response.json(snapshot()) : new Response("Read failed", { status: 503 }));
  const view = render(<ConfigTab active><textarea aria-label="Unrelated draft" defaultValue="Keep this draft" /></ConfigTab>); await ready();
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Unsubmitted draft" } });
  choose("回复详细程度", "高");
  expect((await screen.findByRole("alert")).textContent).toContain("Config file changed");
  expect(button("回复详细程度").textContent).toBe("中");
  await waitFor(() => expect(button("回复详细程度")).toHaveProperty("disabled", false));
  view.rerender(<ConfigTab active={false}><textarea aria-label="Unrelated draft" defaultValue="Keep this draft" /></ConfigTab>);
  view.rerender(<ConfigTab active><textarea aria-label="Unrelated draft" defaultValue="Keep this draft" /></ConfigTab>);
  await screen.findByText("Read failed");
  expect(button("回复详细程度").textContent).toBe("中");
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Unsubmitted draft");
});

it.each([true, false])("keeps a retired workspace's save and errors with that workspace (success=%s)", async success => {
  let finish!: (response: Response) => void;
  mocks.fetch.mockImplementation(async (url, init) => init?.method === "PUT" ? new Promise<Response>(resolve => { finish = resolve; })
    : Response.json(snapshot({ network_access: new URL(url).searchParams.get("workspace_root") === "C:/B" })));
  render(<ConfigTab active />); await ready();
  fireEvent.click(screen.getByRole("switch", { name: "允许网络访问" }));
  await waitFor(() => expect(mocks.fetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));
  act(() => useAppStore.setState({ workingDirectory: "C:/B" }));
  await waitFor(() => expect(screen.getByRole("switch", { name: "允许网络访问" }).getAttribute("aria-checked")).toBe("true"));
  expect(screen.getByRole("switch", { name: "允许网络访问" })).toHaveProperty("disabled", false);
  await act(async () => finish(success ? Response.json(snapshot({ network_access: false })) : new Response("A write failed", { status: 500 })));
  expect(screen.getByRole("switch", { name: "允许网络访问" }).getAttribute("aria-checked")).toBe("true");
  expect(screen.queryByRole("alert")).toBeNull();
});

it("serializes two captured workspace edits and refreshes the actual User version before each dispatch", async () => {
  let finish!: (response: Response) => void;
  let current = snapshot();
  const writes: Record<string, unknown>[] = [];
  mocks.fetch.mockImplementation(async (_url, init) => {
    if (init?.method !== "PUT") return Response.json(current);
    const body = JSON.parse(init.body);
    writes.push(body);
    if (writes.length === 1) return new Promise<Response>(resolve => { finish = resolve; });
    current = snapshot({ model_verbosity: "high", model_reasoning_summary: "detailed" });
    current.scopes[0].version = "user-v3";
    return Response.json(current);
  });
  render(<ConfigTab active />); await ready();
  choose("回复详细程度", "高");
  choose("推理摘要", "详细");
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(button("回复详细程度")).toHaveProperty("disabled", true);
  expect(button("推理摘要")).toHaveProperty("disabled", true);
  act(() => useAppStore.setState({ workingDirectory: "C:/B" }));
  await ready();
  current = snapshot({ model_verbosity: "high" });
  current.scopes[0].version = "user-v2";
  await act(async () => finish(Response.json(current)));
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(writes).toEqual([
    { workspace_root: "C:/A", scope_id: "user", expected_version: "user-v1", model_verbosity: "high" },
    { workspace_root: "C:/A", scope_id: "user", expected_version: "user-v2", model_reasoning_summary: "detailed" },
  ]);
  expect(button("推理摘要").textContent).toBe("自动");
  expect(screen.queryByRole("alert")).toBeNull();
});

it("retains a granular approval value as readonly and opens the selected registered User file", async () => {
  mocks.fetch.mockResolvedValue(Response.json(snapshot({}, { approval_policy: { granular: { rules: ["keep-real-policy"] } } })));
  render(<ConfigTab active />); await ready();
  expect(button("审批策略").textContent).toBe("自定义");
  expect(button("审批策略")).toHaveProperty("disabled", true);
  expect(screen.queryByText("keep-real-policy")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "打开配置文件" }));
  expect(mocks.open).toHaveBeenCalledExactlyOnceWith({ kind: "config", workspaceRoot: "C:/A", scopeId: "user" });
});

it("localizes the real User/Admin scope and managed origin in English without an account gate", async () => {
  const config = snapshot();
  config.scopes.push({ id: "managed", kind: "managed", path: null, version: "managed-v1", readonly: true,
    workspace_root: null, disabled_reason: null,
    source: { type: "session_flags", display: "session-flags" }, values: {} });
  mocks.fetch.mockResolvedValue(Response.json(config));
  render(<LanguageProvider locale="en-US"><ConfigTab active /></LanguageProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Configuration scope，当前：User" }));
  fireEvent.click(screen.getByRole("option", { name: "Admin", exact: true }));
  expect(screen.getAllByText("This config source cannot be edited here.").length).toBeGreaterThan(0);
  expect(within(button("Approval policy")).getByText("On request")).toBeTruthy();
  expect(mocks.fetch).toHaveBeenCalledTimes(1);
});

it("defaults to the actual current project, groups every discovered layer, and uses each layer's raw values and path", async () => {
  const workspace = "C:/Projects/App";
  useAppStore.setState({ workingDirectory: workspace });
  const config = projectSnapshot(workspace, { sandbox_mode: "read-only" }, { web_search: "live" });
  config.scopes.unshift(projectScope("C:/Projects", { web_search: "cached" }));
  mocks.fetch.mockImplementation(async () => Response.json(config));
  render(<ConfigTab active />);
  await screen.findByRole("button", { name: "配置作用域，当前：App" });
  await waitFor(() => expect(button("推理摘要")).toHaveProperty("disabled", false));
  expect(button("配置作用域").title).toBe(`${workspace}/.minicode/config.toml`);
  expect(button("沙箱模式").textContent).toBe("只读");
  expect(button("网页搜索").textContent).toBe("实时");
  expect(screen.queryByRole("switch")).toBeNull();
  fireEvent.click(button("配置作用域"));
  const menu = screen.getByRole("listbox");
  expect(within(menu).getAllByRole("option").map(option => option.textContent)).toEqual(["Projects", "App", "用户"]);
  expect(within(menu).getByText("项目")).toBeTruthy();
  expect(within(menu).getByText("全局")).toBeTruthy();
  expect(within(menu).getByRole("option", { name: "Projects", exact: true }).title).toBe("C:/Projects/.minicode/config.toml");
  fireEvent.click(within(menu).getByRole("option", { name: "Projects", exact: true }));
  expect(button("网页搜索").textContent).toBe("缓存");
  expect(button("沙箱模式").textContent).toBe("工作区写入");
  expect(screen.getByRole("switch", { name: "允许网络访问" })).toHaveProperty("disabled", false);
});

it("retains each workspace's explicit selection across reloads and falls back when a real project scope disappears", async () => {
  let config = projectSnapshot("C:/A");
  config.scopes.unshift(projectScope("C:/", { web_search: "disabled" }));
  mocks.fetch.mockImplementation(async url => Response.json(new URL(url).searchParams.get("workspace_root") === "C:/B" ? projectSnapshot("C:/B") : config));
  const view = render(<ConfigTab active />);
  await screen.findByRole("button", { name: "配置作用域，当前：A" });
  choose("配置作用域", "用户");
  view.rerender(<ConfigTab active={false} />); view.rerender(<ConfigTab active />);
  await ready();
  act(() => useAppStore.setState({ workingDirectory: "C:/B" }));
  await screen.findByRole("button", { name: "配置作用域，当前：B" });
  act(() => useAppStore.setState({ workingDirectory: "C:/A" }));
  await ready();
  choose("配置作用域", "C:");
  config = projectSnapshot("C:/A");
  view.rerender(<ConfigTab active={false} />); view.rerender(<ConfigTab active />);
  await screen.findByRole("button", { name: "配置作用域，当前：A" });
  fireEvent.click(button("配置作用域"));
  expect(screen.getAllByRole("option").map(option => option.textContent)).toEqual(["A", "用户"]);
});

it("shows a disabled project's original warning once, disables editing, and still opens its registered file", async () => {
  const reason = "Project config files are disabled in this folder because it is not trusted.";
  const config = projectSnapshot("C:/A", { sandbox_mode: "workspace-write", model_verbosity: "low" }, { model_verbosity: "high" });
  config.scopes[0].disabled_reason = reason;
  mocks.fetch.mockImplementation(async () => Response.json(config));
  render(<ConfigTab active />);
  expect((await screen.findByRole("alert")).textContent).toBe(reason);
  expect(screen.getAllByText(reason)).toHaveLength(1);
  for (const label of ["审批策略", "沙箱模式", "网页搜索", "回复详细程度", "推理摘要"]) expect(button(label)).toHaveProperty("disabled", true);
  expect(screen.getByRole("switch", { name: "允许网络访问" })).toHaveProperty("disabled", true);
  expect(button("回复详细程度").textContent).toBe("低");
  const open = screen.getByRole("button", { name: "打开配置文件" });
  expect(open).toHaveProperty("disabled", false); fireEvent.click(open);
  expect(mocks.open).toHaveBeenCalledExactlyOnceWith({ kind: "config", workspaceRoot: "C:/A", scopeId: "project:C:/A" });
  expect(mocks.fetch.mock.calls.every(([, init]) => init?.method !== "PUT")).toBe(true);
  choose("配置作用域", "用户");
  expect(screen.queryByRole("alert")).toBeNull();
  await waitFor(() => expect(button("回复详细程度")).toHaveProperty("disabled", false));
});

it("unsets the selected project's Model default and uses the inherited effective value after an applied receipt", async () => {
  let finish!: (response: Response) => void;
  const initial = projectSnapshot("C:/A", { model_verbosity: "low" }, { model_verbosity: "high" });
  mocks.fetch.mockImplementation(async (_url, init) => init?.method === "PUT" ? new Promise<Response>(resolve => { finish = resolve; }) : Response.json(initial));
  render(<ConfigTab active />);
  await screen.findByRole("button", { name: "配置作用域，当前：A" });
  await waitFor(() => expect(button("回复详细程度")).toHaveProperty("disabled", false));
  choose("回复详细程度", "模型默认值");
  await waitFor(() => expect(mocks.fetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));
  const request = mocks.fetch.mock.calls.find(([, init]) => init?.method === "PUT")!;
  expect(JSON.parse(request[1].body)).toEqual({ workspace_root: "C:/A", scope_id: "project:C:/A", expected_version: "project-v1:C:/A", model_verbosity: null });
  const next = projectSnapshot("C:/A", {}, { model_verbosity: "high" });
  next.scopes[0].version = "project-v2:C:/A";
  next.write = { status: "applied", path: next.scopes[0].path!, version: next.scopes[0].version, fields: {
    model_verbosity: { requested_value: null, effective_value: "high" },
  } };
  await act(async () => finish(Response.json(next)));
  expect(button("回复详细程度").textContent).toBe("高");
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("queues parent-project edits with their original lookup workspace, actual layer id and refreshed layer versions", async () => {
  const owner = "C:/Projects/App", parent = "C:/Projects";
  useAppStore.setState({ workingDirectory: owner });
  const original = projectSnapshot(owner, { model_reasoning_summary: "auto" }, { model_verbosity: "low" });
  original.scopes.unshift(projectScope(parent, { model_verbosity: "low" }));
  let current = original, finish!: (response: Response) => void;
  const writes: Record<string, unknown>[] = [], reads: string[] = [];
  mocks.fetch.mockImplementation(async (url, init) => {
    if (init?.method !== "PUT") {
      const requested = new URL(url).searchParams.get("workspace_root")!; reads.push(requested);
      return Response.json(requested === owner ? current : projectSnapshot("C:/B"));
    }
    const body = JSON.parse(init.body); writes.push(body);
    if (writes.length === 1) return new Promise<Response>(resolve => { finish = resolve; });
    current.scopes[0] = { ...current.scopes[0], version: "parent-v3", values: { model_verbosity: "high", model_reasoning_summary: "detailed" } };
    const overridingSource = { source: current.scopes[1].source, version: current.scopes[1].version };
    current.origins.model_reasoning_summary = overridingSource;
    current.write = { status: "overridden", path: current.scopes[0].path!, version: "parent-v3", fields: {
      model_reasoning_summary: { requested_value: "detailed", effective_value: "auto", overriding_source: overridingSource },
    } };
    return Response.json(current);
  });
  render(<ConfigTab active />);
  await screen.findByRole("button", { name: "配置作用域，当前：App" });
  await waitFor(() => expect(button("推理摘要")).toHaveProperty("disabled", false));
  choose("配置作用域", "Projects"); choose("回复详细程度", "高"); choose("推理摘要", "详细");
  await waitFor(() => expect(writes).toHaveLength(1));
  choose("配置作用域", "App");
  expect(button("回复详细程度")).toHaveProperty("disabled", false);
  act(() => useAppStore.setState({ workingDirectory: "C:/B" }));
  await screen.findByRole("button", { name: "配置作用域，当前：B" });
  current = { ...original, model_verbosity: "high", effective: { ...original.effective, model_verbosity: "high" },
    scopes: original.scopes.map(item => item.id === `project:${parent}` ? { ...item, version: "parent-v2", values: { model_verbosity: "high" } } : item) };
  await act(async () => finish(Response.json(current)));
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(writes).toEqual([
    { workspace_root: owner, scope_id: `project:${parent}`, expected_version: `project-v1:${parent}`, model_verbosity: "high" },
    { workspace_root: owner, scope_id: `project:${parent}`, expected_version: "parent-v2", model_reasoning_summary: "detailed" },
  ]);
  expect(reads.filter(root => root === owner)).toHaveLength(3);
  expect(button("推理摘要").textContent).toBe("自动");
  expect(screen.queryByRole("alert")).toBeNull();
  act(() => useAppStore.setState({ workingDirectory: owner }));
  await screen.findByRole("button", { name: "配置作用域，当前：App" });
  choose("配置作用域", "Projects");
  await waitFor(() => expect(button("推理摘要").textContent).toBe("详细"));
  expect((await screen.findByRole("status")).textContent).toContain(`${owner}/.minicode/config.toml`);
});

it("keeps a delayed project write failure on its actual scope after selecting User", async () => {
  let finish!: (response: Response) => void;
  mocks.fetch.mockImplementation(async (_url, init) => init?.method === "PUT" ? new Promise<Response>(resolve => { finish = resolve; }) : Response.json(projectSnapshot("C:/A")));
  render(<ConfigTab active />);
  await screen.findByRole("button", { name: "配置作用域，当前：A" });
  await waitFor(() => expect(button("网页搜索")).toHaveProperty("disabled", false));
  choose("网页搜索", "实时");
  await waitFor(() => expect(mocks.fetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));
  choose("配置作用域", "用户");
  await act(async () => finish(new Response("Project TOML changed on disk", { status: 409 })));
  expect(screen.queryByRole("alert")).toBeNull();
  choose("配置作用域", "A");
  expect((await screen.findByRole("alert")).textContent).toContain("Project TOML changed on disk");
  expect(button("网页搜索").textContent).toBe("缓存");
});

it("uses Source's Chinese indexed label while saving the existing indexed wire value", async () => {
  let current = snapshot();
  mocks.fetch.mockImplementation(async (_url, init) => {
    if (init?.method === "PUT") current = snapshot({ web_search: JSON.parse(String(init.body)).web_search });
    return Response.json(current);
  });
  render(<ConfigTab active />); await ready();
  choose("网页搜索", "索引");
  await waitFor(() => expect(button("网页搜索").textContent).toBe("索引"));
  const [, request] = mocks.fetch.mock.calls.find(([, init]) => init?.method === "PUT")!;
  expect(JSON.parse(String(request.body))).toEqual({ workspace_root: "C:/A", scope_id: "user", expected_version: "user-v1", web_search: "indexed" });
  expect(screen.queryByText("Indexed")).toBeNull();
});

it("shows actual configuration diagnostics and keeps each workspace's warning collection separate", async () => {
  const warning = "Ignored unsupported project-local config keys in C:/A/.minicode/config.toml: llm";
  const configA = { ...snapshot(), warnings: [warning] };
  mocks.fetch.mockImplementation(async input => Response.json(new URL(String(input)).searchParams.get("workspace_root") === "C:/A" ? configA : snapshot()));
  render(<ConfigTab active />); await ready();
  expect(screen.getByRole("alert").textContent).toBe(warning);
  act(() => useAppStore.setState({ workingDirectory: "C:/B" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  act(() => useAppStore.setState({ workingDirectory: "C:/A" }));
  await waitFor(() => expect(screen.getByRole("alert").textContent).toBe(warning));
  expect(useAppStore.getState().draft).toBe("Unsent chat");
});

it("persists only the visible optional efforts after native acknowledgement and leaves runtime capabilities intact", async () => {
  mocks.desktop = true;
  mocks.preferences = { enabledReasoningEfforts: ["low", "medium", "high", "xhigh", "ultra", "persistent"] };
  const modelOptions = [{ model: "configured-model", reasoningEffortLevels: ["low", "medium", "high", "xhigh", "max", "ultra"], defaultReasoningEffort: "medium" }];
  useAppStore.setState({ availableModelOptions: modelOptions, effortLevel: "max" });
  mocks.fetch.mockImplementation(async () => Response.json(snapshot()));
  let acknowledge!: () => void;
  mocks.updatePreferences.mockImplementation((patch: { enabledReasoningEfforts: string[] }) => new Promise<void>(resolve => {
    acknowledge = () => { mocks.preferences = patch; resolve(); };
  }));
  render(<ConfigTab active />); await ready();
  fireEvent.click(screen.getByRole("button", { name: "已选择 5 个" }));
  for (const label of ["轻度", "中", "高", "极高"]) expect(screen.getByRole("menuitemcheckbox", { name: label })).toHaveProperty("disabled", true);
  expect(screen.getByRole("menuitemcheckbox", { name: "Max" }).getAttribute("aria-checked")).toBe("false");
  expect(screen.getByRole("menuitemcheckbox", { name: "Ultra" }).getAttribute("aria-checked")).toBe("true");
  fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Max" }));
  expect(screen.getByRole("button", { name: "已选择 5 个" })).toHaveProperty("disabled", true);
  expect(mocks.updatePreferences).toHaveBeenCalledWith({ enabledReasoningEfforts: ["low", "medium", "high", "xhigh", "ultra", "persistent", "max"] });
  await act(async () => acknowledge());
  expect(screen.getByRole("button", { name: "已选择 6 个" })).toHaveProperty("disabled", false);
  expect(useAppStore.getState()).toMatchObject({ effortLevel: "max", availableModelOptions: modelOptions, draft: "Unsent chat" });
  expect(mocks.fetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false);
});

it("does not invent workspace-bundle repair controls without an installed runtime producer", async () => {
  mocks.fetch.mockImplementation(async () => Response.json(snapshot()));
  render(<ConfigTab active />); await ready();
  expect(screen.queryByRole("button", { name: "诊断" })).toBeNull();
  expect(screen.queryByRole("button", { name: "重新安装" })).toBeNull();
  expect(screen.queryByRole("switch", { name: /依赖项/ })).toBeNull();
});
