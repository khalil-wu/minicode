/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

  it("shows same-name user and project files as separate source records", async () => {
    render(<AgentEditor />);

    expect(await screen.findByText("user description")).toBeTruthy();
    expect(screen.getByText("project description")).toBeTruthy();
    expect(screen.getByText("用户 · 已被覆盖")).toBeTruthy();
    expect(screen.getByText("项目 · 生效中")).toBeTruthy();
    expect(screen.getAllByText("reviewer")).toHaveLength(2);
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

    fireEvent.change(screen.getByLabelText("位置"), {
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

    expect((screen.getByLabelText("位置") as HTMLSelectElement).disabled).toBe(true);
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

    const model = screen.getByLabelText("模型") as HTMLSelectElement;
    expect(Array.from(model.options).map((option) => option.text)).toEqual(expect.arrayContaining([
      "Sonnet（均衡）",
      "Opus（复杂推理）",
      "Haiku（快速）",
      "Z.AI · GLM-5",
    ]));

    fireEvent.change(model, { target: { value: "zai/glm-5" } });
    const effort = screen.getByLabelText("推理强度") as HTMLSelectElement;
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

    const model = screen.getByLabelText("模型") as HTMLSelectElement;
    const effort = screen.getByLabelText("推理强度") as HTMLSelectElement;
    expect(model.value).toBe("legacy-provider/custom-model");
    expect(Array.from(model.options).map((option) => option.text)).toContain("legacy-provider/custom-model（现有定义）");
    expect(effort.value).toBe("legacy-effort");
    expect(Array.from(effort.options).map((option) => option.text)).toContain("legacy-effort");
  });
});
