/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  const sendClientCommand = vi.fn(() => true);
  const sendPromptResponseCommand = vi.fn(async (command: { type?: string }) => {
    sendClientCommand(command);
    if (command.type === "control_response" || command.type === "control_cancel_request") return null;
    return {
      type: "command.result" as const,
      command: command.type || "approval",
      level: "success",
      message: "",
      data: {},
    };
  });
  return {
    sendClientCommand,
    sendPromptResponseCommand,
    sendClientCommandAwaitResult: vi.fn(async (command: unknown, expectedCommand: string) => {
      sendClientCommand(command);
      return {
        type: "command.result",
        command: expectedCommand,
        level: "success",
        message: "",
        data: {},
      };
    }),
  };
});

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
});

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: mocks.sendClientCommand,
  sendClientCommandAwaitResult: mocks.sendClientCommandAwaitResult,
  sendPromptResponseCommand: mocks.sendPromptResponseCommand,
  commandResultSucceeded: () => true,
}));

import { InlineAgentPrompt } from "./InlineAgentPrompt";
import { useAppStore } from "../stores";

describe("InlineAgentPrompt control protocol responses", () => {
  beforeEach(() => {
    mocks.sendClientCommand.mockClear();
    mocks.sendClientCommandAwaitResult.mockClear();
    mocks.sendPromptResponseCommand.mockClear();
    useAppStore.setState({
      conversationId: "conv-inline",
      pendingApproval: null,
      approvalQueue: [],
      pendingDiffReview: null,
      diffReviewQueue: [],
      diffReview: null,
      pendingAskUser: null,
      askUserQueue: [],
    });
  });

  afterEach(() => {
    cleanup();
  });

  it("responds to control approval prompts with control_response", () => {
    useAppStore.setState({
      pendingApproval: {
        requestId: "ctrl-approval",
        conversationId: "conv-inline",
        toolName: "write_file",
        args: { path: "demo.txt" },
        protocol: "control",
      },
    });

    render(<InlineAgentPrompt />);
    fireEvent.click(screen.getByRole("button", { name: "允许使用工具" }));

    expect(mocks.sendPromptResponseCommand).toHaveBeenCalledWith({
      type: "control_response",
      request_id: "ctrl-approval",
      conversation_id: "conv-inline",
      response: {
        subtype: "success",
        response: { action: "approve" },
      },
    });
  });

  it("persists always-allow command rules with explicit global scope", async () => {
    mocks.sendClientCommandAwaitResult.mockResolvedValueOnce({
      type: "command.result",
      command: "permissions.content_rule.add",
      level: "success",
      message: "",
      data: { rule: "run_command(git status:*)", deny: false, scope: "global" },
    });
    useAppStore.setState({
      pendingApproval: {
        requestId: "global-rule-approval",
        conversationId: "conv-inline",
        toolName: "run_command",
        args: { command: "git status" },
        protocol: "control",
      },
    });

    render(<InlineAgentPrompt />);
    fireEvent.click(screen.getByRole("button", { name: "全局始终允许 git status 命令" }));

    await waitFor(() => expect(mocks.sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "permissions.content_rule.add",
      rule: "run_command(git status:*)",
      deny: false,
      scope: "global",
      source: "approval.always_allow_prefix",
    }, "permissions.content_rule.add"));
  });

  it("sends rejection feedback when rejecting a completed plan", async () => {
    useAppStore.setState({
      pendingApproval: {
        requestId: "plan-rejection",
        conversationId: "conv-inline",
        toolName: "exit_plan_mode",
        args: { plan: "# Implementation plan\n\nChange the runtime." },
        protocol: "control",
      },
    });

    render(<InlineAgentPrompt />);
    fireEvent.click(screen.getByRole("button", { name: "拒绝计划" }));
    fireEvent.change(screen.getByRole("textbox", { name: "计划拒绝反馈" }), {
      target: { value: "先补充回滚步骤" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交计划拒绝反馈" }));

    await waitFor(() => expect(mocks.sendPromptResponseCommand).toHaveBeenCalledWith({
      type: "control_response",
      request_id: "plan-rejection",
      conversation_id: "conv-inline",
      response: {
        subtype: "success",
        response: { action: "reject", feedback: "先补充回滚步骤" },
      },
    }));
  });

  it("responds to control ask-user prompts with control_response", () => {
    useAppStore.setState({
      pendingAskUser: {
        requestId: "ctrl-ask",
        conversationId: "conv-inline",
        question: "Proceed?",
        protocol: "control",
      },
    });

    render(<InlineAgentPrompt />);
    fireEvent.change(screen.getByPlaceholderText("输入你的回答…"), {
      target: { value: "yes" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(mocks.sendPromptResponseCommand).toHaveBeenCalledWith({
      type: "control_response",
      request_id: "ctrl-ask",
      conversation_id: "conv-inline",
      response: {
        subtype: "success",
        response: { answer: "yes" },
      },
    });
  });

  it("renders ask-user options with A/B badges and sends the selected option", () => {
    useAppStore.setState({
      pendingAskUser: {
        requestId: "ctrl-choice",
        conversationId: "conv-inline",
        question: "删除临时文件吗？",
        protocol: "control",
        options: [
          { label: "删除", value: "delete" },
          { label: "不删除", value: "keep", description: "保留工作区中的临时文件" },
        ],
      },
    });

    render(<InlineAgentPrompt />);

    expect(screen.getByText("A")).toBeTruthy();
    expect(screen.getByText("B")).toBeTruthy();
    expect(screen.getByText("C")).toBeTruthy();
    expect(screen.getByText("自定义回答")).toBeTruthy();
    expect(screen.getByText("保留工作区中的临时文件")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /不删除/ }));

    expect(mocks.sendPromptResponseCommand).toHaveBeenCalledWith({
      type: "control_response",
      request_id: "ctrl-choice",
      conversation_id: "conv-inline",
      response: {
        subtype: "success",
        response: { answer: "keep" },
      },
    });
  });

  it("shows the provider and distinct prompt context for control elicitations", () => {
    useAppStore.setState({
      pendingAskUser: {
        requestId: "provider-auth",
        conversationId: "conv-inline",
        protocol: "control",
        provider: "github-copilot",
        prompt: "Complete device authorization in the browser first.",
        question: "Enter the verification code",
      },
    });

    render(<InlineAgentPrompt />);

    expect(screen.getByText("认证提供商：github-copilot")).toBeTruthy();
    expect(screen.getByText("Complete device authorization in the browser first.")).toBeTruthy();
    expect(screen.getByText("Enter the verification code")).toBeTruthy();
  });

  it("renders provider secrets as password input and preserves the exact answer", () => {
    useAppStore.setState({
      pendingAskUser: {
        requestId: "provider-secret",
        conversationId: "conv-inline",
        protocol: "control",
        provider: "provider-one",
        question: "Enter the API key exactly",
        promptType: "secret",
        placeholder: "paste exactly",
        allowEmpty: false,
        allowCustom: true,
        secret: true,
      },
    });

    render(<InlineAgentPrompt />);
    const input = screen.getByPlaceholderText("paste exactly") as HTMLInputElement;
    expect(input.type).toBe("password");
    expect(input.autocomplete).toBe("new-password");

    fireEvent.change(input, { target: { value: "  sk-sensitive-value  " } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(mocks.sendPromptResponseCommand).toHaveBeenCalledWith({
      type: "control_response",
      request_id: "provider-secret",
      conversation_id: "conv-inline",
      response: {
        subtype: "success",
        response: { answer: "  sk-sensitive-value  " },
      },
    });
  });

  it("allows an explicitly empty provider response and sends full owner data on cancel", () => {
    useAppStore.setState({
      pendingAskUser: {
        requestId: "provider-empty",
        conversationId: "conv-inline",
        turnId: "turn-auth",
        messageId: "message-auth",
        protocol: "control",
        provider: "provider-one",
        question: "Optional account label",
        allowEmpty: true,
        allowCustom: true,
      },
    });

    const { rerender } = render(<InlineAgentPrompt />);
    const sendButton = screen.getByRole("button", { name: "发送" }) as HTMLButtonElement;
    expect(sendButton.disabled).toBe(false);
    fireEvent.click(sendButton);
    expect(mocks.sendPromptResponseCommand).toHaveBeenLastCalledWith({
      type: "control_response",
      request_id: "provider-empty",
      conversation_id: "conv-inline",
      turn_id: "turn-auth",
      message_id: "message-auth",
      response: {
        subtype: "success",
        response: { answer: "" },
      },
    });

    useAppStore.setState({
      pendingAskUser: {
        requestId: "provider-cancel",
        conversationId: "conv-inline",
        turnId: "turn-auth",
        messageId: "message-auth",
        protocol: "control",
        provider: "provider-one",
        question: "Cancel this prompt",
        allowCustom: true,
      },
    });
    rerender(<InlineAgentPrompt />);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(mocks.sendPromptResponseCommand).toHaveBeenLastCalledWith({
      type: "control_cancel_request",
      request_id: "provider-cancel",
      conversation_id: "conv-inline",
      turn_id: "turn-auth",
      message_id: "message-auth",
    });
  });

  it("renders select-only provider prompts without custom input and submits the real option id", () => {
    useAppStore.setState({
      pendingAskUser: {
        requestId: "provider-select",
        conversationId: "conv-inline",
        protocol: "control",
        provider: "openai-codex",
        question: "Choose a login method",
        promptType: "select",
        allowEmpty: false,
        allowCustom: false,
        options: [
          { label: "Browser login", value: "browser", description: "Use a local callback page" },
          { label: "Device code login", value: "device_code" },
        ],
      },
    });

    render(<InlineAgentPrompt />);
    expect(screen.getByText("Use a local callback page")).toBeTruthy();
    expect(screen.queryByText("自定义回答")).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Device code login/ }));
    expect(mocks.sendPromptResponseCommand).toHaveBeenCalledWith({
      type: "control_response",
      request_id: "provider-select",
      conversation_id: "conv-inline",
      response: {
        subtype: "success",
        response: { answer: "device_code" },
      },
    });
  });

  it("renders generic approval argument summaries without tool-name routing", () => {
    useAppStore.setState({
      pendingApproval: {
        requestId: "cmd-approval",
        conversationId: "conv-inline",
        toolName: "run_command",
        args: { command: "npm run build", cwd: "frontend", url: "https://example.com/noise" },
      },
    });

    render(<InlineAgentPrompt />);

    expect(screen.getByTitle("command: npm run build")).toBeTruthy();
    expect(screen.getByTitle("url: https://example.com/noise")).toBeTruthy();
    expect(screen.queryByTitle("cwd: frontend")).toBeNull();
  });

  it("shows the server-owned approval deadline and highlights the last minute", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-03T00:00:00Z"));
    useAppStore.setState({
      pendingApproval: {
        requestId: "expiring-approval",
        conversationId: "conv-inline",
        toolName: "run_command",
        args: { command: "npm test" },
        expiresAt: Date.now() + 59_000,
      },
    });

    render(<InlineAgentPrompt />);

    expect(screen.getByText(/将在 0:59 后过期/)).toBeTruthy();
    vi.useRealTimers();
  });

  it("uses Codex-style MCP names in approval prompts and their queue", () => {
    useAppStore.setState({
      pendingApproval: {
        requestId: "mcp-approval",
        conversationId: "conv-inline",
        toolName: "mcp__github__search_users",
        args: { query: "octocat" },
      },
      approvalQueue: [{
        requestId: "mcp-queued",
        conversationId: "conv-inline",
        toolName: "mcp__github__get_user",
        args: { login: "octocat" },
      }],
    });

    render(<InlineAgentPrompt />);

    expect(screen.getByText("允许使用 github.search_users？")).toBeTruthy();
    expect(screen.getByText("接下来：github.get_user")).toBeTruthy();
    expect(document.body.textContent).not.toContain("mcp__github__");
  });

  it("uses the same argument ordering for every approval tool", () => {
    useAppStore.setState({
      pendingApproval: {
        requestId: "fetch-approval",
        conversationId: "conv-inline",
        toolName: "web_fetch",
        args: { command: "curl https://example.com", url: "https://docs.example.com/page" },
      },
    });

    render(<InlineAgentPrompt />);

    expect(screen.getByTitle("command: curl https://example.com")).toBeTruthy();
    expect(screen.getByTitle("url: https://docs.example.com/page")).toBeTruthy();
  });

  it("renders diff approval stats with readable copy", () => {
    useAppStore.setState({
      pendingDiffReview: {
        requestId: "diff-approval",
        conversationId: "conv-inline",
        filePath: "src/app.ts",
        diff: "@@ -1 +1 @@\n-old\n+new",
      },
    });

    const { container } = render(<InlineAgentPrompt />);

    expect(screen.getByText(/src\/app\.ts/).textContent).toContain("+1 -1");
    expect(container.textContent).not.toContain("路");
  });

  it("shows and resolves queued diff and ask-user prompts owned by the active conversation", async () => {
    useAppStore.setState({
      pendingDiffReview: {
        requestId: "diff-other",
        conversationId: "conv-other",
        diff: "+other",
      },
      diffReviewQueue: [{
        requestId: "diff-inline",
        conversationId: "conv-inline",
        filePath: "src/queued.ts",
        diff: "@@ -1 +1 @@\n-old\n+queued",
      }],
      pendingAskUser: {
        requestId: "ask-other",
        conversationId: "conv-other",
        question: "Other question?",
      },
      askUserQueue: [{
        requestId: "ask-inline",
        conversationId: "conv-inline",
        question: "Active question?",
      }],
    });

    render(<InlineAgentPrompt />);

    expect(screen.getByText(/src\/queued\.ts/)).toBeTruthy();
    expect(screen.getByText("Active question?")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "允许文件更改" }));
    fireEvent.change(screen.getByPlaceholderText("输入你的回答…"), {
      target: { value: "continue" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(useAppStore.getState().diffReviewQueue).toEqual([]));
    await waitFor(() => expect(useAppStore.getState().askUserQueue).toEqual([]));

    const state = useAppStore.getState();
    expect(state.pendingDiffReview?.requestId).toBe("diff-other");
    expect(state.diffReviewQueue).toEqual([]);
    expect(state.pendingAskUser?.requestId).toBe("ask-other");
    expect(state.askUserQueue).toEqual([]);
  });
});
