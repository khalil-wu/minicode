/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ response: vi.fn(), external: vi.fn() }));
vi.mock("../protocol/ws-outbox", async original => ({ ...(await original<Record<string, unknown>>()), sendPromptResponseCommand: mocks.response }));
vi.mock("../desktop/runtime", async original => ({ ...(await original<Record<string, unknown>>()), isDesktop: () => true,
  getDesktopPreferences: async () => ({ urlOpenTarget: "external-browser" }), openExternal: mocks.external }));
import { useAppStore } from "../stores";
import { handleControlEvent } from "./controlEvents";
import { InlineAgentPrompt } from "./InlineAgentPrompt";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { buildNetworkApprovalResponseCommand } from "../protocol/prompt-responses";
import type { NetworkApprovalContext, NetworkApprovalDecision } from "../protocol/common-types";

const amendment = (action: "allow" | "deny") => ({ network_policy_amendment: { network_policy_amendment: { host: "Example.COM", action } } });
const decisions: NetworkApprovalDecision[] = ["approved", "approved_for_session", amendment("allow"), "abort"];
const event = (protocol: NetworkApprovalContext["protocol"] = "https", choices = decisions) => ({
  type: "control_request", request_id: "network#local#https#example.com#443#independent", conversation_id: "owner",
  turn_id: "review-turn", message_id: "review-message", replayed: true,
  request: { subtype: "can_use_tool", tool_name: "network_access", tool_use_id: "network#local#https#example.com#443#independent",
    input: { host: "Example.COM", protocol, port: 443 }, request_digest: "a".repeat(64),
    command: ["network-access", "https://Example.COM:443"], display_command: "network-access https://Example.COM:443", cwd: "C:/issuing-executor",
    reason: "Example.COM is not in the allowed_domains", network_approval_context: { host: "Example.COM", protocol },
    proposed_network_policy_amendments: [{ host: "Example.COM", action: "allow" }, { host: "Example.COM", action: "deny" }], available_decisions: choices },
});
const receive = (raw = event()) => { const wire = normalizeInboundServerEvent(raw); expect(wire).not.toBeNull(); handleControlEvent(wire!); };
beforeEach(() => {
  mocks.response.mockReset().mockResolvedValue({ type: "command.result", command: "control_response", level: "success", message: "", data: {} });
  mocks.external.mockReset().mockResolvedValue(undefined);
  useAppStore.setState({ conversationId: "owner", pendingConversationSwitchId: null, pendingApproval: null, approvalQueue: [],
    pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [], subagents: [],
    draft: "unsent original draft", attachments: [{ id: "original-attachment", name: "original.png" } as never] });
});
afterEach(cleanup);

describe("Source network approval through wire, owner store, existing surface and response", () => {
  it.each(["approved", "approved_for_session", "amendment", "abort"])("sends the exact advertised %s decision without generic remember flags", async choice => {
    receive(); const view = render(<InlineAgentPrompt />);
    expect(useAppStore.getState().pendingApproval).toMatchObject({ networkApprovalContext: { host: "Example.COM", protocol: "https" },
      proposedNetworkPolicyAmendments: [{ host: "Example.COM", action: "allow" }, { host: "Example.COM", action: "deny" }] });
    expect(screen.getByText("互联网访问")).toBeTruthy();
    expect(screen.getByText("Example.COM 不在当前网络允许列表中")).toBeTruthy();
    expect(screen.getByRole("link", { name: "https://Example.COM" }).getAttribute("href")).toBe("https://Example.COM");
    expect(view.container.querySelector('[data-native-approval-source-icon="hand"]')).toBeTruthy();
    expect(screen.queryByLabelText("即将运行的命令")).toBeNull();
    expect(view.container.querySelector(".inline-approval-summary")).toBeNull();
    expect(view.container.textContent).not.toMatch(/network-access|issuing-executor|allowed_domains|始终拒绝/);
    let selected: NetworkApprovalDecision = choice === "amendment" ? amendment("allow") : choice as NetworkApprovalDecision;
    if (choice === "approved_for_session") {
      fireEvent.click(screen.getByRole("button", { name: "选择允许范围" }));
      expect(screen.getAllByRole("menuitem").map(item => item.textContent)).toEqual(["允许一次", "允许此对话"]);
      fireEvent.click(screen.getByRole("menuitem", { name: "允许此对话" }));
    } else fireEvent.click(screen.getByRole("button", { name: choice === "amendment" ? "始终允许" : choice === "abort" ? "拒绝使用工具" : "允许使用工具" }));
    await waitFor(() => expect(mocks.response).toHaveBeenCalledWith({ type: "control_response", request_id: event().request_id,
      conversation_id: "owner", turn_id: "review-turn", message_id: "review-message", response: { subtype: "success", response: {
        action: choice === "abort" ? "reject" : "approve", decision: selected, request_digest: "a".repeat(64) } } }));
    await waitFor(() => expect(useAppStore.getState().pendingApproval).toBeNull());
    expect(useAppStore.getState().draft).toBe("unsent original draft");
    expect(useAppStore.getState().attachments).toEqual([{ id: "original-attachment", name: "original.png" }]);
  });
  it("uses the existing preferred-link route and preserves the pending request", async () => {
    receive(); render(<InlineAgentPrompt />); fireEvent.click(screen.getByRole("link"));
    await waitFor(() => expect(mocks.external).toHaveBeenCalledWith("https://Example.COM"));
    expect(mocks.response).not.toHaveBeenCalled(); expect(useAppStore.getState().pendingApproval).not.toBeNull();
  });
  it.each(["socks5_tcp", "socks5_udp"] as const)("renders the %s host as text", protocol => {
    receive(event(protocol)); render(<InlineAgentPrompt />); expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("允许 MiniCode 与 Example.COM 建立连接？")).toBeTruthy();
  });
  it("creates persistent and conversation actions only from advertised decisions", () => {
    receive(event("http", ["approved", amendment("deny"), "abort"])); render(<InlineAgentPrompt />);
    expect(screen.queryByRole("button", { name: "始终允许" })).toBeNull();
    expect(screen.queryByRole("button", { name: "选择允许范围" })).toBeNull();
    expect(screen.queryByText("始终拒绝")).toBeNull();
  });
  it("keeps the actual rejection visible and the original draft after a semantic refusal", async () => {
    mocks.response.mockResolvedValueOnce({ type: "command.result", command: "control_response", level: "error", message: "Exact request is stale", data: {} });
    receive(); render(<InlineAgentPrompt />); fireEvent.click(screen.getByRole("button", { name: "允许使用工具" }));
    expect(await screen.findByText("Exact request is stale")).toBeTruthy();
    expect(useAppStore.getState().pendingApproval?.requestId).toBe(event().request_id);
    expect(useAppStore.getState().draft).toBe("unsent original draft");
  });
  it("serializes an explicitly advertised deny amendment with reject", () => {
    expect(buildNetworkApprovalResponseCommand("network-explicit-deny", amendment("deny"), { requestDigest: "digest",
      owner: { conversationId: "owner", turnId: "turn", messageId: "message" } })).toEqual({ type: "control_response", request_id: "network-explicit-deny",
      conversation_id: "owner", turn_id: "turn", message_id: "message", response: { subtype: "success", response: {
        action: "reject", decision: amendment("deny"), request_digest: "digest" } } });
  });
});

describe("network approval boundary", () => {
  it.each(["http", "https", "socks5_tcp", "socks5_udp"] as const)("accepts exact Source protocol %s and double-nested amendments", protocol => {
    expect(normalizeInboundServerEvent(event(protocol, [...decisions, amendment("deny")]))).not.toBeNull();
  });
  it.each([
    { network_approval_context: { host: "Example.COM", protocol: "socks5-tcp" } },
    { network_approval_context: { host: "", protocol: "https" } },
    { network_approval_context: undefined }, { available_decisions: undefined },
    { available_decisions: [{ network_policy_amendment: { host: "Example.COM", action: "allow" } }] },
    { available_decisions: [{ network_policy_amendment: { network_policy_amendment: { host: "Example.COM", action: "ask" } } }] },
    { proposed_network_policy_amendments: [{ host: "Example.COM", action: "ask" }] },
  ])("rejects malformed network wire data %j", patch => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const raw = event(); expect(normalizeInboundServerEvent({ ...raw, request: { ...raw.request, ...patch } })).toBeNull(); warn.mockRestore();
  });
});
