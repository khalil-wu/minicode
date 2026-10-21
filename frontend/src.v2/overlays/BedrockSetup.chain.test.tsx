/* @vitest-environment jsdom */
import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { BedrockSetup, type BedrockGuidance } from "./BedrockSetup";
import { ProviderTab } from "./ProviderTab";
import type { LLMSettingsPayload } from "./settingsShared";
import { useAppStore } from "../stores";
import { resolveModelBrand } from "../components/ModelBrandIcon";

const fixtures = vi.hoisted(() => ({ fetch: vi.fn(), command: vi.fn(), open: vi.fn(), toast: vi.fn() }));
vi.mock("../protocol/api", async importOriginal => ({ ...await importOriginal<object>(),
  apiBase: () => "http://owning-host", authHeaders: (headers: object = {}) => headers, fetchWithTimeout: fixtures.fetch }));
vi.mock("../protocol/ws-outbox", async importOriginal => ({ ...await importOriginal<object>(), sendClientCommandAwaitResult: fixtures.command }));
vi.mock("./ToastContainer", () => ({ pushToast: fixtures.toast }));
vi.mock("../desktop/runtime", async importOriginal => ({ ...await importOriginal<object>(), openExternal: fixtures.open }));

const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
const none = { profiles: [] as Array<{ name: string; region: string | null }>,
  environmentCredentials: [] as Array<{ type: "accessKeys" | "bedrockApiKey"; region: string | null }> };
const profileDiscovery: typeof none = { profiles: [{ name: "first", region: "us-east-2" }, { name: "second", region: "eu-west-1" }],
  environmentCredentials: [{ type: "accessKeys", region: "ap-southeast-1" }] };
const originalSection = { display_name: "Saved gateway", base_url: "https://original.example/v1", model: "original-model",
  available_models: ["original-model"], wire_api: "responses" as const, api_key: "saved-fixture-key" };
const originalPayload: LLMSettingsPayload = { provider: "custom", custom: originalSection,
  provider_history: [{ ...originalSection, provider: "custom", provider_id: "custom" }] };
const awsPayload: LLMSettingsPayload = { provider: "custom", active_model: "gpt-bedrock", custom: {
  display_name: "Amazon Bedrock", base_url: "https://bedrock-mantle.us-east-2.api.aws/openai/v1", model: "gpt-bedrock",
  available_models: ["gpt-bedrock"], wire_api: "responses", api_key: "", aws: { region: "us-east-2", endpoint: "mantle", credential_id: "vault-reference" },
  models_source: "source_bedrock_catalog" }, provider_history: originalPayload.provider_history };

beforeEach(() => {
  vi.clearAllMocks();
  fixtures.command.mockImplementation(async (command: { type: string }) => ({ type: "command.result", command: command.type,
    level: "success", message: "", data: {} }));
  fixtures.fetch.mockImplementation(async (url: string) => {
    if (url.endsWith("/bedrock/discover")) return response(none);
    if (url.endsWith("/bedrock/gov-cloud-check")) return response({ isGovCloud: false, shouldWarn: false });
    if (url.endsWith("/bedrock/setup")) return response(awsPayload);
    throw new Error(`Unexpected request ${url}`);
  });
  useAppStore.setState({ conversationId: "A", workingDirectory: "C:/A", currentProvider: "custom", currentModel: "original-model",
    currentProviderBaseUrl: originalSection.base_url, currentProviderId: "custom", currentWireApi: "responses", pendingConversationSwitchId: null,
    draft: "Keep the conversation draft", attachments: [], isStreaming: false });
});
afterEach(() => cleanup());

function setup(discovery = none, result: BedrockGuidance = null) {
  fixtures.fetch.mockResolvedValueOnce(response(discovery));
  const onSubmit = vi.fn(async () => result);
  const onComplete = vi.fn(), onBack = vi.fn(), onCheck = vi.fn(async () => null as BedrockGuidance);
  const view = render(<BedrockSetup onSubmit={onSubmit} onComplete={onComplete} onBack={onBack} onCheckGovCloud={onCheck} />);
  return { ...view, onSubmit, onComplete, onBack, onCheck };
}

it("uses the detected profile selector with its region subtext and submits that profile's actual region", async () => {
  const current = setup(profileDiscovery as typeof none);
  const selector = await screen.findByRole("button", { name: "AWS 配置文件，当前：first" });
  expect(screen.queryByLabelText("AWS 区域")).toBeNull();
  fireEvent.click(selector);
  const alternate = screen.getByRole("option", { name: /second/ });
  expect(alternate.textContent).toContain("eu-west-1");
  fireEvent.click(alternate);
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
  await waitFor(() => expect(current.onSubmit).toHaveBeenCalledExactlyOnceWith({ method: "profile", profile: "second", region: "eu-west-1" }));
  expect(current.onComplete).toHaveBeenCalledOnce();
});

it("requests a missing profile region explicitly and retains the real failure and entered fields", async () => {
  const current = setup({ profiles: [{ name: "no-region", region: null }], environmentCredentials: [{ type: "accessKeys", region: "us-west-2" }] } as typeof none);
  current.onSubmit.mockRejectedValueOnce(new Error("AWS profile credentials expired"));
  await screen.findByRole("button", { name: "AWS 配置文件，当前：no-region" });
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
  const region = screen.getByLabelText("AWS 区域") as HTMLInputElement;
  expect((screen.getByLabelText("AWS 配置文件") as HTMLInputElement).value).toBe("no-region");
  expect(region.value).toBe("");
  expect(region.type).toBe("text");
  expect((screen.getByRole("button", { name: "继续" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(region, { target: { value: " eu-central-1 " } });
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", "AWS profile credentials expired");
  expect(current.onComplete).not.toHaveBeenCalled();
  expect(region.value).toBe(" eu-central-1 ");
  expect(current.onSubmit).toHaveBeenCalledWith({ method: "profile", profile: "no-region", region: "eu-central-1" });
});

it("keeps environment credentials host-side and rechecks discovery without submitting the previous credential choice", async () => {
  const current = setup({ profiles: [], environmentCredentials: [{ type: "bedrockApiKey", region: "us-west-2" }] } as typeof none);
  fireEvent.click(await screen.findByRole("button", { name: "使用检测到的凭据继续" }));
  expect((screen.getByLabelText("AWS 区域") as HTMLInputElement).value).toBe("us-west-2");
  expect(screen.queryByLabelText("Bedrock API 密钥")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "再次检查凭据" }));
  expect(await screen.findByText("未找到 AWS 凭据")).toBeTruthy();
  expect(current.onSubmit).not.toHaveBeenCalled();
  expect(fixtures.fetch).toHaveBeenCalledTimes(2);
});

it("masks manually entered credentials and preserves them while choosing another sign-in method", async () => {
  const current = setup();
  fireEvent.click(await screen.findByRole("button", { name: "AWS 访问密钥" }));
  const key = screen.getByLabelText("AWS 访问密钥 ID") as HTMLInputElement;
  const secret = screen.getByLabelText("AWS 私密访问密钥") as HTMLInputElement;
  const token = screen.getByLabelText("AWS 会话令牌（可选）") as HTMLInputElement;
  expect(key.type).toBe("text"); expect(secret.type).toBe("password"); expect(token.type).toBe("password");
  fireEvent.change(key, { target: { value: "fixture-access" } }); fireEvent.change(secret, { target: { value: "fixture-secret" } });
  fireEvent.change(token, { target: { value: "   " } }); fireEvent.change(screen.getByLabelText("AWS 区域"), { target: { value: "us-east-2" } });
  fireEvent.click(screen.getByRole("button", { name: "选择其他登录方式" }));
  fireEvent.click(screen.getByRole("button", { name: "AWS 访问密钥" }));
  expect((screen.getByLabelText("AWS 私密访问密钥") as HTMLInputElement).value).toBe("fixture-secret");
  fireEvent.change(screen.getByLabelText("AWS 区域"), { target: { value: "us-east-2" } });
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
  await waitFor(() => expect(current.onSubmit).toHaveBeenCalledExactlyOnceWith({ method: "accessKeys", accessKeyId: "fixture-access",
    secretAccessKey: "fixture-secret", sessionToken: null, region: "us-east-2" }));
});

it("aborts a cancelled discovery and does not invent a no-credentials result on an HTTP error", async () => {
  let finish!: (value: Response) => void;
  fixtures.fetch.mockImplementationOnce(() => new Promise<Response>(resolve => { finish = resolve; }));
  const onBack = vi.fn();
  const view = render(<BedrockSetup onBack={onBack} onSubmit={vi.fn()} onComplete={vi.fn()} onCheckGovCloud={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "返回" }));
  expect(onBack).toHaveBeenCalledOnce();
  const signal = fixtures.fetch.mock.calls[0][1].signal;
  view.unmount();
  expect(signal.aborted).toBe(true);
  await act(async () => finish(response(profileDiscovery)));
  fixtures.fetch.mockResolvedValueOnce(response({ detail: "Owning host discovery failed" }, 503));
  render(<BedrockSetup onBack={vi.fn()} onSubmit={vi.fn()} onComplete={vi.fn()} onCheckGovCloud={vi.fn()} />);
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Owning host discovery failed");
  expect(screen.queryByText("未找到 AWS 凭据")).toBeNull();
});

it.each(["warning", "unverified"] as const)("waits for the Source acknowledgement after the actual GovCloud result %s", async guidance => {
  const current = setup(none, guidance);
  fireEvent.click(await screen.findByRole("button", { name: "Bedrock API 密钥" }));
  const key = screen.getByLabelText("Bedrock API 密钥") as HTMLInputElement;
  expect(key.type).toBe("password");
  fireEvent.change(key, { target: { value: "fixture-bedrock-key" } });
  fireEvent.change(screen.getByLabelText("AWS 区域"), { target: { value: "us-east-2" } });
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
  const acknowledgement = await screen.findByRole("button", { name: "知悉" });
  expect(current.onComplete).not.toHaveBeenCalled();
  if (guidance === "unverified") {
    current.onCheck.mockResolvedValueOnce("warning");
    fireEvent.click(screen.getByRole("button", { name: "再次检查" }));
    await screen.findByRole("heading", { name: "在 AWS GovCloud 中使用 MiniCode" });
    expect(current.onSubmit).toHaveBeenCalledOnce();
    expect(current.onCheck).toHaveBeenCalledOnce();
  }
  fireEvent.click(acknowledgement);
  expect(current.onComplete).toHaveBeenCalledOnce();
});

function ProviderHarness({ onSaved = vi.fn() }: { onSaved?: (payload: LLMSettingsPayload) => void }) {
  const [payload, setPayload] = useState(originalPayload);
  const [provider, setProvider] = useState<"custom" | "openai" | "anthropic">("custom");
  const [payloadRef] = useState({ current: payload });
  return <ProviderTab selectedProvider={provider} settingsPayload={payload} settingsPayloadRef={payloadRef}
    onProviderChange={setProvider} onSettingsPayloadChange={saved => { setPayload(saved); onSaved(saved); }} />;
}
const openFromProviderDraft = async () => {
  fireEvent.click(screen.getByRole("button", { name: "编辑 Saved gateway" }));
  fireEvent.change(screen.getByLabelText("提供商显示名称"), { target: { value: "Unsent gateway draft" } });
  fireEvent.change(screen.getByLabelText("API 密钥"), { target: { value: "unsent-fixture-key" } });
  fireEvent.click(screen.getByRole("button", { name: "使用 Amazon Bedrock" }));
  await screen.findByRole("button", { name: "Bedrock API 密钥" });
};

it("returns from the Source setup to the original unsaved provider draft and trigger without saving credentials", async () => {
  render(<ProviderHarness />);
  await openFromProviderDraft();
  fireEvent.click(screen.getByRole("button", { name: "返回" }));
  expect((await screen.findByLabelText("提供商显示名称") as HTMLInputElement).value).toBe("Unsent gateway draft");
  expect((screen.getByLabelText("API 密钥") as HTMLInputElement).value).toBe("unsent-fixture-key");
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "使用 Amazon Bedrock" }));
  expect(fixtures.fetch.mock.calls.some(([_url, options]) => options?.method === "POST")).toBe(false);
  expect(useAppStore.getState().draft).toBe("Keep the conversation draft");
});

it.each([
  ["apiKey", "amazonBedrock"], ["accessKeys", "amazonBedrockAccessKeys"], ["profile", "profile"], ["environment", "environment"],
] as const)("maps Source %s to its real backend type %s and keeps application on the initiating owner", async (method, type) => {
  let commit!: (value: Response) => void;
  const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
  fixtures.fetch.mockImplementation(async (url: string, options?: RequestInit) => {
    if (url.endsWith("/bedrock/discover")) return response(method === "environment"
      ? { profiles: [], environmentCredentials: [{ type: "accessKeys", region: "us-east-2" }] } : none);
    if (url.endsWith("/bedrock/setup")) {
      requests.push({ url, body: JSON.parse(String(options?.body)) });
      return new Promise<Response>(resolve => { commit = resolve; });
    }
    if (url.endsWith("/bedrock/gov-cloud-check")) return response({ isGovCloud: false, shouldWarn: false });
    throw new Error(`Unexpected request ${url}`);
  });
  const saved = vi.fn();
  render(<ProviderHarness onSaved={saved} />);
  fireEvent.click(screen.getByRole("button", { name: "编辑 Saved gateway" }));
  fireEvent.change(screen.getByLabelText("提供商显示名称"), { target: { value: "Unsent gateway draft" } });
  fireEvent.click(screen.getByRole("button", { name: "使用 Amazon Bedrock" }));
  fireEvent.click(await screen.findByRole("button", { name: method === "apiKey" ? "Bedrock API 密钥"
    : method === "accessKeys" ? "AWS 访问密钥" : method === "environment" ? "使用检测到的凭据继续" : "AWS 配置文件" }));
  if (method === "apiKey") fireEvent.change(screen.getByLabelText("Bedrock API 密钥"), { target: { value: "fixture-key" } });
  if (method === "accessKeys") {
    fireEvent.change(screen.getByLabelText("AWS 访问密钥 ID"), { target: { value: "fixture-access" } });
    fireEvent.change(screen.getByLabelText("AWS 私密访问密钥"), { target: { value: "fixture-secret" } });
  }
  if (method === "profile") fireEvent.change(screen.getByLabelText("AWS 配置文件"), { target: { value: "manual-profile" } });
  fireEvent.change(screen.getByLabelText("AWS 区域"), { target: { value: "us-east-2" } });
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0].body).toMatchObject({ type, region: "us-east-2", confirm_sensitive_change: true });
  expect(requests[0].body).not.toHaveProperty("endpoint");
  act(() => useAppStore.setState({ conversationId: "B", workingDirectory: "C:/B", currentModel: "B-model", currentProvider: "openai" }));
  await act(async () => commit(response(awsPayload)));
  await screen.findByRole("button", { name: "Amazon Bedrock 已启用" });
  const apply = fixtures.command.mock.calls.find(([command]) => command.type === "llm.config.set");
  expect(apply?.[0]).toMatchObject({ provider: "custom", model: "gpt-bedrock", conversation_id: "A", workspace_root: "C:/A", source: "settings.provider.bedrock" });
  expect(useAppStore.getState().currentModel).toBe("B-model");
  expect(saved).toHaveBeenCalledWith(awsPayload);
  fireEvent.click(screen.getByRole("button", { name: "编辑 Saved gateway" }));
  expect((screen.getByLabelText("提供商显示名称") as HTMLInputElement).value).toBe("Unsent gateway draft");
});

it("keeps a saved-but-unapplied credential result honest and retains the provider draft", async () => {
  fixtures.command.mockImplementation(async (command: { type: string }) => ({ type: "command.result", command: command.type,
    level: command.type === "llm.config.set" ? "error" : "success", message: "Original session retired", data: {} }));
  render(<ProviderHarness />);
  await openFromProviderDraft();
  fireEvent.click(screen.getByRole("button", { name: "Bedrock API 密钥" }));
  fireEvent.change(screen.getByLabelText("Bedrock API 密钥"), { target: { value: "fixture-key" } });
  fireEvent.change(screen.getByLabelText("AWS 区域"), { target: { value: "us-east-2" } });
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
  expect((await screen.findByRole("alert")).textContent).toContain("提供商已保存，但应用失败");
  expect(screen.queryByRole("button", { name: "Amazon Bedrock 已启用" })).toBeNull();
  expect(fixtures.fetch.mock.calls.filter(([url]) => String(url).endsWith("/gov-cloud-check"))).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "选择其他登录方式" }));
  fireEvent.click(screen.getByRole("button", { name: "返回" }));
  expect((screen.getByLabelText("提供商显示名称") as HTMLInputElement).value).toBe("Unsent gateway draft");
});

it("rechecks a real GovCloud lookup failure without submitting the credentials again", async () => {
  let checks = 0;
  fixtures.fetch.mockImplementation(async (url: string) => {
    if (url.endsWith("/bedrock/discover")) return response(none);
    if (url.endsWith("/bedrock/setup")) return response(awsPayload);
    if (url.endsWith("/bedrock/gov-cloud-check")) return ++checks === 1
      ? response({ detail: "Check unavailable" }, 503) : response({ isGovCloud: false, shouldWarn: false });
    throw new Error(`Unexpected request ${url}`);
  });
  render(<ProviderHarness />);
  await openFromProviderDraft();
  fireEvent.click(screen.getByRole("button", { name: "Bedrock API 密钥" }));
  fireEvent.change(screen.getByLabelText("Bedrock API 密钥"), { target: { value: "fixture-key" } });
  fireEvent.change(screen.getByLabelText("AWS 区域"), { target: { value: "us-east-2" } });
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
  fireEvent.click(await screen.findByRole("button", { name: "再次检查" }));
  await screen.findByRole("button", { name: "Amazon Bedrock 已启用" });
  expect(checks).toBe(2);
  expect(fixtures.fetch.mock.calls.filter(([url]) => String(url).endsWith("/bedrock/setup"))).toHaveLength(1);
});

it("uses the existing packaged Bedrock brand glyph", () => expect(resolveModelBrand("Amazon Bedrock")?.id).toBe("bedrock"));
