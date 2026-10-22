// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ConfigTab, type AgentConfigFields, type AgentConfigSnapshot } from "./ConfigTab";
import { useAppStore } from "../stores";
import { LanguageProvider } from "../i18n";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), open: vi.fn() }));
vi.mock("../protocol/api", async load => ({ ...await load<typeof import("../protocol/api")>(),
  apiBase: () => "http://config.test", authHeaders: () => ({}), fetchWithTimeout: mocks.fetch }));
vi.mock("../desktop/runtime", async load => ({ ...await load<typeof import("../desktop/runtime")>(), openManagedSettingsTarget: mocks.open }));

const path = "C:/Profile/config.toml";
const defaults: AgentConfigFields = { approval_policy: "on-request", sandbox_mode: "workspace-write", network_access: false,
  web_search: "cached", model_verbosity: "medium", model_reasoning_summary: "auto" };
const snapshot = (effective: Partial<AgentConfigFields> = {}, values: Partial<AgentConfigFields> = {}): AgentConfigSnapshot => ({
  path, ...defaults, ...effective, effective: { ...defaults, ...effective }, origins: {}, requirements: {},
  scopes: [{ id: "user", kind: "user", path, version: "user-v1", values, readonly: false,
    source: { type: "user", file: path, profile: "review", display: `user (${path}, profile=review)` } }],
});
const button = (label: string) => screen.getByRole("button", { name: new RegExp(`^${label}，当前：`) });
const choose = (label: string, option: string) => {
  fireEvent.click(button(label));
  fireEvent.click(screen.getByRole("option", { name: new RegExp(`^${option}`) }));
};
const ready = async () => { await screen.findByRole("button", { name: "配置作用域，当前：用户" }); await waitFor(() => expect(button("推理摘要")).toHaveProperty("disabled", false)); };

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  mocks.open.mockResolvedValue(true);
  useAppStore.setState({ workingDirectory: "C:/A", draft: "Unsent chat", attachments: [] });
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
    source: { type: "session_flags", display: "session-flags" }, values: {} });
  mocks.fetch.mockResolvedValue(Response.json(config));
  render(<LanguageProvider locale="en-US"><ConfigTab active /></LanguageProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Configuration scope，当前：User" }));
  fireEvent.click(screen.getByRole("option", { name: "Admin", exact: true }));
  expect(screen.getAllByText("This config source cannot be edited here.").length).toBeGreaterThan(0);
  expect(within(button("Approval policy")).getByText("On request")).toBeTruthy();
  expect(mocks.fetch).toHaveBeenCalledTimes(1);
});
