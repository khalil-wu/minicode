/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAppStore } from "../stores";
import { AgentEditor } from "./AgentEditor";

const mocks = vi.hoisted(() => ({
  pushToast: vi.fn(),
  showConfirm: vi.fn(async () => true),
}));

vi.mock("../protocol/api", () => ({
  apiBase: () => "http://test.local",
  authHeaders: (headers?: HeadersInit) => headers ?? {},
  errorMessageFromResponseText: (text: string, fallback: string) => text || fallback,
  fetchWithTimeout: (input: RequestInfo | URL, init?: RequestInit) => fetch(input, init),
}));

vi.mock("./ToastContainer", () => ({
  pushToast: mocks.pushToast,
}));

vi.mock("./DialogService", () => ({
  showConfirm: mocks.showConfirm,
}));

const response = (payload: unknown, ok = true) => ({
  ok,
  status: ok ? 200 : 400,
  statusText: ok ? "OK" : "Bad Request",
  json: async () => payload,
  text: async () => JSON.stringify(payload),
}) as Response;

const userAgent = {
  name: "reviewer",
  description: "user description",
  prompt: "User prompt",
  model: "sonnet",
  effort: "high",
  tools: ["Read"],
  disallowed_tools: ["Write"],
  source_path: "C:\\Users\\tester\\.minicode\\agents\\reviewer-user.md",
  filename: "reviewer-user",
  source: "user",
  location: "user",
  editable: true,
  deletable: true,
  can_override: false,
  active: false,
};

const projectAgent = {
  ...userAgent,
  description: "project description",
  prompt: "Project prompt",
  source_path: "C:\\repo\\.minicode\\agents\\reviewer-project.md",
  filename: "reviewer-project",
  source: "project",
  location: "project",
  active: true,
};

const managedAgent = {
  ...projectAgent,
  description: "managed description",
  source_path: "C:\\Program Files\\MiniCode\\agents\\reviewer.md",
  filename: "reviewer",
  source: "policy",
  location: "policy",
  editable: false,
  deletable: false,
  active: true,
};

describe("AgentEditor MiniCode source contract", () => {
  let agentsPayload: Record<string, unknown>;
  let settingsPayload: Record<string, unknown>;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    agentsPayload = { agents: [userAgent, projectAgent], model_catalog: [] };
    settingsPayload = {};
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = String(init?.method || "GET").toUpperCase();
      if (url.endsWith("/api/llm/settings")) return response(settingsPayload);
      if (url.endsWith("/api/agents") && method === "GET") return response(agentsPayload);
      if (url.endsWith("/api/agents") && method === "POST") {
        return response({ agent: projectAgent });
      }
      if (url.includes("/api/agents/") && method === "DELETE") {
        return response({ deleted: true });
      }
      return response({ detail: "not found" }, false);
    });
    vi.stubGlobal("fetch", fetchMock);
    useAppStore.setState({
      conversationId: undefined,
      conversations: [],
      workingDirectory: "",
      agentEditorOpen: true,
      currentModel: "",
      currentProvider: "",
      currentProviderId: "",
      availableModels: [],
      runtimeCapabilities: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("keeps an explicitly projectless conversation out of the globally active project when creating an agent", async () => {
    agentsPayload = { agents: [], model_catalog: [] };
    useAppStore.setState({ conversationId: "projectless", workingDirectory: "C:/other-project", conversations: [{ id: "projectless", title: "Projectless", updatedAt: "now" }] });
    render(<AgentEditor />);
    await screen.findByText("暂无自定义 Agent");
    const request = fetchMock.mock.calls.find(([url, init]) => String(url).includes("/api/agents") && !init?.method)!;
    expect(new URL(String(request[0])).searchParams.has("workspace_root")).toBe(false);
    const location = screen.getByLabelText("位置", { selector: "select" }) as HTMLSelectElement;
    expect(location.value).toBe("user");
    expect(location.querySelector('option[value="project"]')?.hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "personal-helper" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true));
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST")!;
    expect(JSON.parse(String(post[1]?.body))).toMatchObject({ workspace_root: "", location: "user" });
  });

  it("does not publish a previous owner's agent/model catalog after deferred settings JSON finishes", async () => {
    let finishOldSettings!: (settings: unknown) => void;
    const oldSettings = new Promise((resolve) => { finishOldSettings = resolve; });
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      const root = useAppStore.getState().workingDirectory;
      if (url.pathname === "/api/llm/settings") return root === "C:/A" ? { ...response({}), json: () => oldSettings } as Response : response({});
      return response({ agents: [{ ...projectAgent, description: url.searchParams.get("workspace_root") === "C:/A" ? "A old" : "B current" }], model_catalog: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    useAppStore.setState({ workingDirectory: "C:/A" });
    render(<AgentEditor />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    act(() => useAppStore.setState({ workingDirectory: "C:/B" }));
    await screen.findByText("B current");
    await act(async () => finishOldSettings({ openai: { model: "old-owner-model" } }));
    expect(screen.getByText("B current")).toBeTruthy();
    expect(screen.queryByText("A old")).toBeNull();
    expect(screen.queryByRole("option", { name: /old-owner-model/ })).toBeNull();
  });

  it("shows same-name user and project files as separate source records", async () => {
    render(<AgentEditor />);

    expect(await screen.findByText("user description")).toBeTruthy();
    expect(screen.getByText("project description")).toBeTruthy();
    expect(screen.getByText("用户 · 已被覆盖")).toBeTruthy();
    expect(screen.getByText("项目 · 生效中")).toBeTruthy();
    expect(screen.getAllByText("reviewer")).toHaveLength(2);
  });

  it("keeps drafts for each source when switching agents and reopening the editor", async () => {
    render(<AgentEditor />);
    fireEvent.click((await screen.findByText("user description")).closest("button")!);
    fireEvent.change(screen.getByLabelText("说明"), { target: { value: "未保存用户指令" } });
    fireEvent.click(screen.getByText("project description").closest("button")!);
    fireEvent.change(screen.getByLabelText("说明"), { target: { value: "未保存项目指令" } });
    fireEvent.click(screen.getByText("user description").closest("button")!);
    expect(screen.getByLabelText("说明")).toHaveProperty("value", "未保存用户指令");
    fireEvent.click(screen.getByRole("button", { name: "关闭 Agent 编辑器" }));
    act(() => useAppStore.setState({ agentEditorOpen: true }));
    expect(screen.getByLabelText("说明")).toHaveProperty("value", "未保存用户指令");
    expect(screen.getByText("未保存")).toBeTruthy();
  });

  it("updates an existing source in place with source and source_path", async () => {
    render(<AgentEditor />);
    const userDescription = await screen.findByText("user description");
    fireEvent.click(userDescription.closest("button") as HTMLButtonElement);

    expect((screen.getByLabelText("名称") as HTMLInputElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("说明"), {
      target: { value: "updated user description" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "http://test.local/api/agents",
        expect.objectContaining({ method: "POST" }),
      );
    });
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    const body = JSON.parse(String(post?.[1]?.body || "{}"));
    expect(body).toMatchObject({
      name: "reviewer",
      description: "updated user description",
      source: "user",
      location: "",
      source_path: userAgent.source_path,
    });
  });

  it("creates user and project agents through MiniCode's location field", async () => {
    agentsPayload = { agents: [], model_catalog: [] };
    render(<AgentEditor />);
    await screen.findByText("暂无自定义 Agent");

    fireEvent.change(screen.getByLabelText("位置", { selector: "select" }), {
      target: { value: "user" },
    });
    fireEvent.change(screen.getByLabelText("名称"), {
      target: { value: "new-agent" },
    });
    fireEvent.change(screen.getByLabelText("系统提示词"), {
      target: { value: "Do the work." },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
    ).toBe(true));
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    const body = JSON.parse(String(post?.[1]?.body || "{}"));
    expect(body).toMatchObject({
      name: "new-agent",
      location: "user",
      source: "",
      source_path: "",
    });
  });

  it("keeps managed agents read-only without promising an ineffective override", async () => {
    agentsPayload = { agents: [managedAgent], model_catalog: [] };
    render(<AgentEditor />);
    const description = await screen.findByText("managed description");
    fireEvent.click(description.closest("button") as HTMLButtonElement);

    expect((screen.getByLabelText("位置", { selector: "select" }) as HTMLSelectElement).disabled).toBe(true);
    expect((screen.getByLabelText("说明") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByRole("button", { name: "只读来源" })).toBeTruthy();
    expect(screen.queryByText(/创建项目覆盖/)).toBeNull();
    expect(screen.getByRole("button", { name: "删除 Agent reviewer" }).hasAttribute("disabled")).toBe(true);
  });

  it("deletes the exact source path selected in the duplicate list", async () => {
    agentsPayload = { agents: [userAgent], model_catalog: [] };
    render(<AgentEditor />);
    await screen.findByText("user description");

    fireEvent.click(screen.getByRole("button", { name: "删除 Agent reviewer" }));

    await waitFor(() => expect(mocks.showConfirm).toHaveBeenCalled());
    await waitFor(() => expect(
      fetchMock.mock.calls.some(([url, init]) => String(url).includes("/api/agents/reviewer?") && init?.method === "DELETE"),
    ).toBe(true));
    const call = fetchMock.mock.calls.find(([url, init]) => String(url).includes("/api/agents/reviewer?") && init?.method === "DELETE");
    const url = new URL(String(call?.[0]));
    expect(url.searchParams.get("source")).toBe("user");
    expect(url.searchParams.get("source_path")).toBe(userAgent.source_path);
  });

  it.each(["delete", "save"] as const)("keeps the current project draft and loaded list when an old-project %s finishes", async (operation) => {
    const a = { ...projectAgent, name: "scope-A", filename: "scope-A", description: "A 已保存", source_path: "C:/A/.minicode/agents/a.md" };
    const b = { ...projectAgent, name: "scope-B", filename: "scope-B", description: "B 已保存", source_path: "C:/B/.minicode/agents/b.md" };
    let finish!: (value: Response) => void;
    const pending = new Promise<Response>((resolve) => { finish = resolve; });
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const method = init?.method || "GET";
      if (url.pathname === "/api/llm/settings") return response({});
      if (method === "POST" || method === "DELETE") return pending;
      if (url.pathname === "/api/agents") return response({ agents: [url.searchParams.get("workspace_root") === "C:/B" ? b : a], model_catalog: [] });
      throw new Error(`Unexpected request: ${input}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    useAppStore.setState({ conversationId: "B", workingDirectory: "C:/B", conversations: [
      { id: "A", title: "A", workspaceRoot: "C:/A", updatedAt: "2026-10-04" },
      { id: "B", title: "B", workspaceRoot: "C:/B", updatedAt: "2026-10-04" },
    ] });
    render(<AgentEditor />);
    fireEvent.click((await screen.findByText("B 已保存")).closest("button")!);
    fireEvent.change(screen.getByLabelText("说明"), { target: { value: "B 尚未保存的说明" } });
    act(() => useAppStore.setState({ conversationId: "A", workingDirectory: "C:/A" }));
    fireEvent.click((await screen.findByText("A 已保存")).closest("button")!);
    if (operation === "save") fireEvent.change(screen.getByLabelText("说明"), { target: { value: "A 修改" } });
    fireEvent.click(screen.getByRole("button", { name: operation === "save" ? "保存" : "删除 Agent scope-A" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === (operation === "save" ? "POST" : "DELETE"))).toBe(true));
    act(() => useAppStore.setState({ conversationId: "B", workingDirectory: "C:/B" }));
    await screen.findByText("B 已保存");
    expect(screen.queryByText("正在加载…")).toBeNull();
    expect(screen.getByLabelText("说明")).toHaveProperty("value", "B 尚未保存的说明");
    await act(async () => finish(response(operation === "save" ? { agent: { ...a, description: "A 修改" } } : { deleted: true })));
    expect(screen.getByLabelText("说明")).toHaveProperty("value", "B 尚未保存的说明");
    expect(screen.getByText("B 已保存")).toBeTruthy();
    expect(screen.queryByText("正在加载…")).toBeNull();
    expect(fetchMock.mock.calls.filter(([input, init]) => {
      const url = new URL(String(input));
      return url.pathname === "/api/agents" && !init?.method && url.searchParams.get("workspace_root") === "C:/A";
    })).toHaveLength(1);
  });

  it("combines MiniCode aliases with the published ModelRuntime catalog", async () => {
    agentsPayload = {
      agents: [],
      model_catalog: [{
        provider: "zai",
        provider_name: "Z.AI",
        model: "glm-5",
        model_name: "GLM-5",
        reasoning_effort_levels: ["off", "low", "high"],
        default_reasoning_effort: "high",
      }],
    };
    render(<AgentEditor />);
    await screen.findByText("暂无自定义 Agent");

    const model = screen.getByLabelText("模型", { selector: "select" }) as HTMLSelectElement;
    expect(Array.from(model.options).map((option) => option.text)).toEqual(expect.arrayContaining([
      "Sonnet（均衡）",
      "Opus（复杂推理）",
      "Haiku（快速）",
      "Z.AI · GLM-5",
    ]));

    fireEvent.change(model, { target: { value: "zai/glm-5" } });
    const effort = screen.getByLabelText("推理强度", { selector: "select" }) as HTMLSelectElement;
    expect(Array.from(effort.options).map((option) => option.text)).toEqual(expect.arrayContaining(["off", "low", "high"]));
    expect(Array.from(effort.options).map((option) => option.text)).not.toContain("medium");
    expect(screen.getByText("目标模型默认：high")).toBeTruthy();
  });

  it("preserves an existing custom model and effort not present in the live catalog", async () => {
    agentsPayload = {
      agents: [{
        ...projectAgent,
        model: "legacy-provider/custom-model",
        effort: "legacy-effort",
      }],
      model_catalog: [],
    };
    render(<AgentEditor />);
    const description = await screen.findByText("project description");
    fireEvent.click(description.closest("button") as HTMLButtonElement);

    const model = screen.getByLabelText("模型", { selector: "select" }) as HTMLSelectElement;
    const effort = screen.getByLabelText("推理强度", { selector: "select" }) as HTMLSelectElement;
    expect(model.value).toBe("legacy-provider/custom-model");
    expect(Array.from(model.options).map((option) => option.text)).toContain("legacy-provider/custom-model（现有定义）");
    expect(effort.value).toBe("legacy-effort");
    expect(Array.from(effort.options).map((option) => option.text)).toContain("legacy-effort");
  });
});
