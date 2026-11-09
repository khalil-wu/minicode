/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ response: vi.fn() }));
vi.mock("../protocol/ws-outbox", async original => ({ ...(await original<Record<string, unknown>>()), sendPromptResponseCommand: mocks.response }));
import { useAppStore } from "../stores";
import { LanguageProvider } from "../i18n";
import { handleControlEvent } from "./controlEvents";
import { InlineAgentPrompt } from "./InlineAgentPrompt";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";

const appId = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App";
const meta = (persist = ["session", "always"], app = appId, riskLevel = "low", appDisplayName = "Calculator") => ({
  codex_approval_kind: "mcp_tool_call", connector_id: "computer-use", connector_name: "Computer Use", persist, riskLevel,
  tool_params: { app }, tool_params_display: [{ name: "app", display_name: "App", value: appDisplayName }],
});
const event = (metadata = meta(), requestId = "native-approval") => ({
  type: "control_request", request_id: requestId, conversation_id: "native-conversation", turn_id: "native-turn",
  message_id: "native-message", task_id: "native-task", replayed: true,
  request: { subtype: "elicitation", tool_use_id: requestId, mode: "form", mcp_server_name: "cua_repl", schema: {},
    question: "Allow Codex to use Calculator?", prompt: "Allow Codex to use Calculator?", _meta: metadata },
});
function receive(raw = event()) {
  const wire = normalizeInboundServerEvent(raw);
  expect(wire).not.toBeNull();
  handleControlEvent(wire!);
}
beforeEach(() => {
  localStorage.removeItem("minicode:has-approved-computer-use-app");
  mocks.response.mockReset().mockResolvedValue({ type: "command.result", command: "control_response", level: "success", message: "", data: {} });
  useAppStore.setState({ conversationId: "native-conversation", pendingConversationSwitchId: null,
    pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [],
    draft: "unsent app task", attachments: [{ id: "original-attachment", name: "original.png" } as never] });
});
afterEach(cleanup);

describe("Source Computer Use approval from real elicitation metadata", () => {
  it.each(["session", "always", "decline"])("sends explicit %s and original owner, keeping draft, attachment and unrelated queue", async choice => {
    receive();
    const original = useAppStore.getState().pendingAskUser!;
    useAppStore.getState().setAskUser({ ...original, conversationId: "another-conversation", turnId: "another-turn" });
    const view = render(<InlineAgentPrompt />);
    expect(view.container.querySelector(".inline-computer-use-approval")?.getAttribute("data-computer-use-app-id")).toBe(appId);
    expect(screen.getByText("允许 ChatGPT 使用 Calculator？")).toBeTruthy();
    expect(screen.queryByText("Allow Codex to use Calculator?")).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "继续" })).toBeNull();
    expect(useAppStore.getState().pendingAskUser?.elicitation?.meta).toEqual(meta());
    fireEvent.click(screen.getByRole("button", { name: choice === "always" ? "始终允许" : choice === "decline" ? /拒绝/ : /允许此对话/ }));
    await waitFor(() => expect(mocks.response).toHaveBeenCalledWith({ type: "control_response", request_id: "native-approval",
      conversation_id: "native-conversation", turn_id: "native-turn", message_id: "native-message", task_id: "native-task",
      response: { subtype: "success", response: choice === "decline" ? { action: "decline" } : { action: "accept", content: { persist: choice } } } }));
    await waitFor(() => expect(useAppStore.getState().pendingAskUser?.conversationId).toBe("another-conversation"));
    expect(useAppStore.getState().draft).toBe("unsent app task");
    expect(useAppStore.getState().attachments).toEqual([{ id: "original-attachment", name: "original.png" }]);
    expect(localStorage.getItem("minicode:has-approved-computer-use-app")).toBe(choice === "decline" ? null : "true");
  });
  it("only offers conversation access for audio and shows Source elevated risk", async () => {
    receive(event(meta(["session"], "computer-audio", "high", "Computer audio")));
    render(<InlineAgentPrompt />);
    expect(screen.getByText("较高风险")).toBeTruthy();
    expect(screen.getByText("允许 ChatGPT 使用 Computer audio？")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "始终允许" })).toBeNull();
    fireEvent.keyDown(document.body, { key: "Enter" });
    await waitFor(() => expect(mocks.response).toHaveBeenCalledOnce());
    expect(mocks.response.mock.calls[0][0].response.response).toEqual({ action: "accept", content: { persist: "session" } });
  });
  it("keeps the pending application request and local drafts on semantic refusal", async () => {
    mocks.response.mockResolvedValueOnce({ type: "command.result", command: "control_response", level: "error", message: "Application approval was replaced", data: {} });
    receive(); render(<InlineAgentPrompt />);
    fireEvent.click(screen.getByRole("button", { name: /允许此对话/ }));
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Application approval was replaced");
    expect(useAppStore.getState().pendingAskUser?.requestId).toBe("native-approval");
    expect(useAppStore.getState().draft).toBe("unsent app task");
    expect(localStorage.getItem("minicode:has-approved-computer-use-app")).toBeNull();
  });
  it("rejects with Escape, never converting refusal to a cancelled form or granting app access", async () => {
    receive(); render(<InlineAgentPrompt />); fireEvent.keyDown(document.body, { key: "Escape" });
    await waitFor(() => expect(mocks.response).toHaveBeenCalledOnce());
    expect(mocks.response.mock.calls[0][0].response.response).toEqual({ action: "decline" });
  });
  it("shows the first-use explanation until an accepted app choice, and suppresses repeat submissions", async () => {
    let finish!: (value: unknown) => void;
    mocks.response.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    receive(); render(<InlineAgentPrompt />);
    expect(screen.getByText(/可让 ChatGPT 使用您电脑上的应用/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /允许此对话/ }));
    fireEvent.keyDown(document.body, { key: "Enter" });
    expect(mocks.response).toHaveBeenCalledOnce();
    await act(async () => finish({ type: "command.result", command: "control_response", level: "success", message: "", data: {} }));
    act(() => receive(event(meta(), "next-native-approval")));
    expect(screen.queryByText(/可让 ChatGPT 使用您电脑上的应用/)).toBeNull();
  });
  it.each(["zh-CN", "en-US"] as const)("uses the actual app display name and Source %s title independently of the helper question", locale => {
    receive();
    render(<LanguageProvider locale={locale}><InlineAgentPrompt /></LanguageProvider>);
    expect(screen.getByText(locale === "zh-CN" ? "允许 ChatGPT 使用 Calculator？" : "Allow ChatGPT to use Calculator?")).toBeTruthy();
    expect(screen.queryByText("Allow Codex to use Calculator?")).toBeNull();
  });
});
