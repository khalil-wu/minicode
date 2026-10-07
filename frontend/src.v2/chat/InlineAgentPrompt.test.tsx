/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  const sendClientCommand = vi.fn(() => true);
  const sendPromptResponseCommand = vi.fn(async (command: { type?: string }) => {
    sendClientCommand(command);
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
  commandResultSucceeded: (event: { level: string }) => !["error", "failed"].includes(event.level),
}));

import { InlineAgentPrompt } from "./InlineAgentPrompt";
import { useAppStore } from "../stores";
import { loadPromptDrafts } from "../stores/prompt-drafts";

describe("InlineAgentPrompt control protocol responses", () => {
  it("renders the upstream structured schema and submits typed content while preserving its draft across remount", async () => {
    useAppStore.getState().setAskUser({ requestId: "structured", conversationId: "conv-inline", question: "Profile",
      inputSchema: { type: "object", properties: { name: { type: "string" }, age: { type: "integer", default: 30 }, score: { type: "number", default: 95.5 },
        verified: { type: "boolean", default: true }, status: { type: "string", enum: ["active", "inactive"], default: "active" },
        template: { type: "string", oneOf: [{ const: "monthly-review", title: "Monthly review" }] } }, required: ["name", "template"] } });
    let mounted = render(<InlineAgentPrompt />);
    fireEvent.change(screen.getByRole("textbox", { name: "name" }), { target: { value: "Shanghai" } });
    fireEvent.change(screen.getByRole("combobox", { name: "template" }), { target: { value: "monthly-review" } });
    mounted.unmount(); mounted = render(<InlineAgentPrompt />);
    expect((screen.getByRole("textbox", { name: "name" }) as HTMLInputElement).value).toBe("Shanghai");
    fireEvent.click(screen.getByRole("button", { name: "继续" }));
    await waitFor(() => expect(mocks.sendPromptResponseCommand).toHaveBeenCalledWith(expect.objectContaining({
      response: { subtype: "success", response: { action: "accept", content: { name: "Shanghai", age: 30, score: 95.5, verified: true, status: "active", template: "monthly-review" } } },
    })));
  });

  it("cancels a structured MCP request with the same action contract", async () => {
    useAppStore.getState().setAskUser({ requestId: "structured-cancel", conversationId: "conv-inline", question: "City",
      inputSchema: { type: "object", properties: { city: { type: "string" } }, required: ["city"] } });
    render(<InlineAgentPrompt />);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await waitFor(() => expect(mocks.sendPromptResponseCommand).toHaveBeenCalledWith(expect.objectContaining({ response: { subtype: "success", response: { action: "cancel" } } })));
  });
  beforeEach(() => {
    localStorage.removeItem("minicode.agentPromptDrafts");
    mocks.sendClientCommand.mockClear();
    mocks.sendClientCommandAwaitResult.mockClear();
    mocks.sendPromptResponseCommand.mockClear();
    useAppStore.setState({
      conversationId: "conv-inline",
      promptDrafts: {},
      pendingApproval: null,
      approvalQueue: [],
      pendingDiffReview: null,
      diffReviewQueue: [],
      diffReview: null,
      pendingAskUser: null,
      askUserQueue: [],
      runtimeSession: null,
    });
  });

  afterEach(() => {
    cleanup();
  });

  it("keeps a chosen answer through a conversation switch and clears its saved draft only after submission", async () => {
    useAppStore.getState().setAskUser({ requestId: "draft-answer", conversationId: "conv-inline", question: "Which view?",
      options: [{ label: "Code", value: "code" }, { label: "Preview", value: "preview" }] });
    render(<InlineAgentPrompt />);
    fireEvent.click(screen.getByRole("radio", { name: /Preview/ }));
    expect(mocks.sendPromptResponseCommand).not.toHaveBeenCalled();
    act(() => useAppStore.setState({ conversationId: "other" }));
    expect(screen.queryByText("Which view?")).toBeNull();
    act(() => useAppStore.setState({ conversationId: "conv-inline", promptDrafts: loadPromptDrafts() }));
    expect(screen.getByRole("radio", { name: /Preview/ }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "继续" }));
    await waitFor(() => expect(screen.queryByText("Which view?")).toBeNull());
    expect(loadPromptDrafts()).toEqual({});
    expect(mocks.sendPromptResponseCommand).toHaveBeenCalledWith(expect.objectContaining({ response: expect.objectContaining({ response: { answer: "preview" } }) }));
  });

  it("restores edited plans and rejection feedback after leaving their owner", () => {
    useAppStore.getState().setApproval({ requestId: "draft-plan", conversationId: "conv-inline", toolName: "exit_plan_mode",
      args: { plan: "# Initial plan" } });
    render(<InlineAgentPrompt />);
    fireEvent.click(screen.getByRole("button", { name: "编辑计划" }));
    fireEvent.change(screen.getByRole("textbox", { name: "编辑计划" }), { target: { value: "# Revised plan\nKeep the current layout." } });
    fireEvent.click(screen.getByRole("button", { name: "拒绝计划" }));
    fireEvent.change(screen.getByRole("textbox", { name: "计划拒绝反馈" }), { target: { value: "Complete the existing chain first." } });
    act(() => useAppStore.setState({ conversationId: "other" }));
    act(() => useAppStore.setState({ conversationId: "conv-inline", promptDrafts: loadPromptDrafts() }));
    expect((screen.getByRole("textbox", { name: "编辑计划" }) as HTMLTextAreaElement).value).toBe("# Revised plan\nKeep the current layout.");
    expect((screen.getByRole("textbox", { name: "计划拒绝反馈" }) as HTMLTextAreaElement).value).toBe("Complete the existing chain first.");
    expect(mocks.sendPromptResponseCommand).not.toHaveBeenCalled();
    act(() => useAppStore.getState().clearApproval("draft-plan"));
    expect(loadPromptDrafts()).toEqual({});
  });

  it("keeps authentication input transient instead of saving it with ordinary answers", () => {
    useAppStore.getState().setAskUser({ requestId: "auth-draft", conversationId: "conv-inline", question: "Credential", secret: true });
    render(<InlineAgentPrompt />);
    fireEvent.change(screen.getByLabelText("输入认证密钥"), { target: { value: "test-credential" } });
    expect(loadPromptDrafts()).toEqual({});
    act(() => useAppStore.setState({ conversationId: "other" }));
    act(() => useAppStore.setState({ conversationId: "conv-inline" }));
    expect((screen.getByLabelText("输入认证密钥") as HTMLInputElement).value).toBe("");
  });

  it.each(["approval", "plan", "diff", "answer", "cancel"])(
    "keeps an unresolved %s prompt visible and retryable after semantic refusal",
    async (kind) => {
      let finish!: (result: { type: "command.result"; command: string; level: string; message: string; data: {} }) => void;
      mocks.sendPromptResponseCommand.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
      if (kind === "approval" || kind === "plan") {
        useAppStore.getState().setApproval({ requestId: "unconfirmed", conversationId: "conv-inline",
          toolName: kind === "plan" ? "exit_plan_mode" : "write_file", args: kind === "plan" ? { plan: "# Plan\nImplement verified behavior" } : {} });
      } else if (kind === "diff") {
        useAppStore.getState().setDiffReview({ requestId: "unconfirmed", conversationId: "conv-inline", diff: "+proposed" });
      } else {
        useAppStore.getState().setAskUser({ requestId: "unconfirmed", conversationId: "conv-inline", question: "Still unresolved?" });
      }
      render(<InlineAgentPrompt />);
      if (kind === "answer") fireEvent.change(screen.getByPlaceholderText("输入你的回答…"), { target: { value: "yes" } });
      const label = kind === "approval" ? "允许使用工具" : kind === "plan" ? "批准计划并开始实现"
        : kind === "diff" ? "允许文件更改" : kind === "answer" ? "发送" : "取消";
      fireEvent.click(screen.getByRole("button", { name: label }));
      expect((screen.getByRole("button", { name: label }) as HTMLButtonElement).disabled).toBe(true);
      await act(async () => { await Promise.resolve(); });
      expect(mocks.sendPromptResponseCommand).toHaveBeenCalledOnce();
      await act(async () => finish({ type: "command.result", command: kind === "cancel" ? "control_cancel_request" : "control_response",
        level: "error", message: "The request was not accepted; retry explicitly", data: {} }));
      expect(screen.getByText("The request was not accepted; retry explicitly")).toBeTruthy();
      expect((screen.getByRole("button", { name: label }) as HTMLButtonElement).disabled).toBe(false);
      expect(mocks.sendPromptResponseCommand).toHaveBeenCalledOnce();
      fireEvent.click(screen.getByRole("button", { name: label }));
      await waitFor(() => expect(screen.queryByRole("button", { name: label })).toBeNull());
      expect(mocks.sendPromptResponseCommand).toHaveBeenCalledTimes(2);
    },
  );

  it.each(["comments", "partial"])("retains DiffPanel %s review until the semantic outcome", async (action) => {
    let fail!: (reason: Error) => void;
    mocks.sendPromptResponseCommand.mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }));
    const review = { requestId: "panel-unconfirmed", conversationId: "conv-inline", diff: "+new",
      files: [{ path: "file.txt" }], status: "pending" as const, fileDecisions: { "file.txt": "approved" as const }, lineComments: [] };
    useAppStore.getState().setDiffReview({ requestId: review.requestId, conversationId: review.conversationId, diff: review.diff, reviewState: review });
    const pending = action === "comments" ? useAppStore.getState().submitDiffReviewWithComments() : useAppStore.getState().submitPartialApproval();
    expect(useAppStore.getState().pendingDiffReview?.requestId).toBe(review.requestId);
    expect(useAppStore.getState().diffReview?.status).toBe("submitted");
    fail(new Error("command.persistence"));
    await pending;
    expect(useAppStore.getState().pendingDiffReview?.requestId).toBe(review.requestId);
    expect(useAppStore.getState().diffReview).toMatchObject({ status: "error", error: "command.persistence" });
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

  it("explains an unisolated network boundary and requires individual command approval", () => {
    useAppStore.setState({
      pendingApproval: {
        requestId: "network-command",
        conversationId: "conv-inline",
        toolName: "run_command",
        args: { command: "python -V" },
        networkUnisolated: true,
      },
      approvalQueue: [{
        requestId: "next-command",
        conversationId: "conv-inline",
        toolName: "run_command",
        args: { command: "python -m pytest" },
        networkUnisolated: true,
      }],
    });

    render(<InlineAgentPrompt />);

    expect(screen.getByRole("alert").textContent).toContain("无法隔离网络");
    expect(screen.queryByRole("button", { name: /全局始终允许/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "允许所有未提升权限的待处理工具请求" })).toBeNull();
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

  it("keeps short plans directly readable without an unnecessary disclosure control", () => {
    useAppStore.setState({
      pendingApproval: {
        requestId: "short-plan",
        conversationId: "conv-inline",
        toolName: "exit_plan_mode",
        args: { plan: "# Small change\n\nUpdate the button label." },
      },
    });

    render(<InlineAgentPrompt />);
    expect(screen.getByText("Update the button label.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "展开计划" })).toBeNull();
    expect(screen.queryByRole("button", { name: "收起计划" })).toBeNull();
  });

  it("expands and collapses a long plan while preserving its full content through edit and approval", () => {
    const plan = `# Detailed plan\n\n${Array.from({ length: 18 }, (_, index) => `- Step ${index + 1}`).join("\n")}\n\nFinal exact text.  `;
    useAppStore.setState({
      pendingApproval: {
        requestId: "long-plan",
        conversationId: "conv-inline",
        turnId: "plan-turn",
        messageId: "plan-message",
        toolName: "exit_plan_mode",
        args: { plan },
      },
    });

    render(<InlineAgentPrompt />);
    expect(screen.getByRole("button", { name: "展开计划" }).getAttribute("aria-expanded")).toBe("false");
    expect(screen.getByText("Step 18")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "展开计划" }));
    expect(screen.getByRole("button", { name: "收起计划" }).getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "收起计划" }));
    expect(screen.getByRole("button", { name: "展开计划" }).getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(screen.getByRole("button", { name: "编辑计划" }));
    const editor = screen.getByRole("textbox", { name: "编辑计划" }) as HTMLTextAreaElement;
    expect(editor.value).toBe(plan);
    expect(screen.queryByRole("button", { name: "展开计划" })).toBeNull();
    const editedPlan = `${plan}\n\nOne additional requirement.  `;
    fireEvent.change(editor, { target: { value: editedPlan } });
    fireEvent.click(screen.getByRole("button", { name: "预览计划" }));
    expect(screen.getByText("One additional requirement.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "批准计划并开始实现" }));

    expect(mocks.sendPromptResponseCommand).toHaveBeenCalledWith({
      type: "control_response",
      request_id: "long-plan",
      conversation_id: "conv-inline",
      turn_id: "plan-turn",
      message_id: "plan-message",
      response: { subtype: "success", response: { action: "approve", plan: editedPlan } },
    });
  });

  it("starts a different long plan collapsed with its own original content", () => {
    const plan = `# Current plan\n\n${"Detailed requirement.\n".repeat(18)}`;
    const approval = {
      requestId: "current-plan",
      conversationId: "conv-inline",
      toolName: "exit_plan_mode",
      args: { plan },
    };
    useAppStore.setState({ pendingApproval: approval });
    render(<InlineAgentPrompt />);
    fireEvent.click(screen.getByRole("button", { name: "展开计划" }));
    expect(screen.getByRole("button", { name: "收起计划" })).toBeTruthy();

    const nextPlan = plan.replace("Current plan", "Next plan");
    act(() => useAppStore.setState({
      pendingApproval: { ...approval, requestId: "next-plan", args: { plan: nextPlan } },
    }));
    expect(screen.getByRole("button", { name: "展开计划" }).getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("button", { name: "收起计划" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "编辑计划" }));
    expect((screen.getByRole("textbox", { name: "编辑计划" }) as HTMLTextAreaElement).value).toBe(nextPlan);
    expect(mocks.sendPromptResponseCommand).not.toHaveBeenCalled();
  });

  it.each(["", " \n "])("blocks approval of an empty teammate plan while retaining rejection", async (planContent) => {
    useAppStore.setState({
      pendingAskUser: {
        requestId: "empty-teammate-plan",
        conversationId: "conv-inline",
        question: "Review the teammate plan",
        planReview: { subagentId: "teammate-1", teammateName: "builder", planContent },
      },
    });

    render(<InlineAgentPrompt />);
    const approve = screen.getByRole("button", { name: "批准子智能体的计划" }) as HTMLButtonElement;
    expect(approve.disabled).toBe(true);
    fireEvent.click(approve);
    expect(mocks.sendClientCommandAwaitResult).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "拒绝子智能体的计划" }));

    expect(mocks.sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "subagent.plan_review",
      subagent_id: "teammate-1",
      request_id: "empty-teammate-plan",
      approved: false,
      conversation_id: "conv-inline",
    }, "subagent.plan_review");
    await waitFor(() => expect(useAppStore.getState().pendingAskUser).toBeNull());
  });

  it("selects an ask-user option without sending and confirms its exact value with Continue", () => {
    useAppStore.setState({
      pendingAskUser: {
        requestId: "ctrl-choice",
        conversationId: "conv-inline",
        turnId: "turn-choice",
        messageId: "message-choice",
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
    expect((screen.getByRole("button", { name: "继续" }) as HTMLButtonElement).disabled).toBe(true);

    const keepOption = screen.getByRole("radio", { name: /不删除/ });
    fireEvent.click(keepOption);

    expect(keepOption.getAttribute("aria-checked")).toBe("true");
    expect(mocks.sendPromptResponseCommand).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: "继续" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "继续" }));

    expect(mocks.sendPromptResponseCommand).toHaveBeenCalledWith({
      type: "control_response",
      request_id: "ctrl-choice",
      conversation_id: "conv-inline",
      turn_id: "turn-choice",
      message_id: "message-choice",
      response: {
        subtype: "success",
        response: { answer: "keep" },
      },
    });
  });

  it("switches exclusively between options and custom input and submits custom text through the same form", () => {
    useAppStore.setState({
      pendingAskUser: {
        requestId: "custom-choice",
        conversationId: "conv-inline",
        question: "Choose a direction",
        options: [{ label: "Suggested direction", value: "suggested" }],
      },
    });

    render(<InlineAgentPrompt />);
    const option = screen.getByRole("radio", { name: /Suggested direction/ });
    const input = screen.getByRole("textbox", { name: "回答 Agent 的问题" }) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "earlier custom answer" } });
    fireEvent.click(option);
    expect(input.value).toBe("");
    expect(option.getAttribute("aria-checked")).toBe("true");

    fireEvent.focus(input);
    expect(option.getAttribute("aria-checked")).toBe("true");
    expect((screen.getByRole("button", { name: "继续" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.change(input, { target: { value: "  My own direction  " } });
    expect(option.getAttribute("aria-checked")).toBe("false");
    expect(mocks.sendPromptResponseCommand).not.toHaveBeenCalled();
    fireEvent.submit(input.form!);

    expect(mocks.sendPromptResponseCommand).toHaveBeenCalledWith({
      type: "control_response",
      request_id: "custom-choice",
      conversation_id: "conv-inline",
      response: { subtype: "success", response: { answer: "  My own direction  " } },
    });
  });

  it("preserves an option through empty-input focus and keyboard form confirmation", () => {
    useAppStore.setState({
      pendingAskUser: {
        requestId: "keyboard-choice", conversationId: "conv-inline", question: "Choose an answer",
        options: [{ label: "Keep this choice", value: "retained-option" }],
      },
    });
    render(<InlineAgentPrompt />);
    fireEvent.click(screen.getByRole("radio", { name: /Keep this choice/ }));
    const input = screen.getByRole("textbox", { name: "回答 Agent 的问题" }) as HTMLInputElement;
    fireEvent.focus(input);
    fireEvent.submit(input.form!);
    expect(mocks.sendPromptResponseCommand).toHaveBeenCalledWith({
      type: "control_response", request_id: "keyboard-choice", conversation_id: "conv-inline",
      response: { subtype: "success", response: { answer: "retained-option" } },
    });
  });

  it.each(["refused", "failed"])("retains a selected option after a %s submission for an explicit retry", async (outcome) => {
    if (outcome === "refused") {
      mocks.sendPromptResponseCommand.mockResolvedValueOnce({
        type: "command.result", command: "control_response", level: "error", message: "Answer was not accepted", data: {},
      });
    } else {
      mocks.sendPromptResponseCommand.mockRejectedValueOnce(new Error("Answer was not accepted"));
    }
    useAppStore.setState({
      pendingAskUser: {
        requestId: "retry-choice",
        conversationId: "conv-inline",
        question: "Choose an answer",
        allowCustom: false,
        options: [{ label: "Displayed label", value: "actual-option-id" }],
      },
    });

    render(<InlineAgentPrompt />);
    fireEvent.click(screen.getByRole("radio", { name: /Displayed label/ }));
    fireEvent.click(screen.getByRole("button", { name: "继续" }));
    await screen.findByText("Answer was not accepted");
    expect(screen.getByRole("radio", { name: /Displayed label/ }).getAttribute("aria-checked")).toBe("true");
    expect((screen.getByRole("button", { name: "继续" }) as HTMLButtonElement).disabled).toBe(false);
    expect(useAppStore.getState().pendingAskUser?.requestId).toBe("retry-choice");
    expect(mocks.sendPromptResponseCommand).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: "继续" }));
    await waitFor(() => expect(useAppStore.getState().pendingAskUser).toBeNull());
    const expectedCommand = {
      type: "control_response",
      request_id: "retry-choice",
      conversation_id: "conv-inline",
      response: { subtype: "success", response: { answer: "actual-option-id" } },
    };
    expect(mocks.sendPromptResponseCommand).toHaveBeenNthCalledWith(1, expectedCommand);
    expect(mocks.sendPromptResponseCommand).toHaveBeenNthCalledWith(2, expectedCommand);
  });

  it("moves through a single choice group with arrow and boundary keys without submitting", () => {
    useAppStore.setState({
      pendingAskUser: {
        requestId: "keyboard-navigation", conversationId: "conv-inline", question: "选择一个方向",
        options: [
          { label: "界面", value: "ui" },
          { label: "编辑器", value: "editor" },
          { label: "预览", value: "preview" },
        ],
      },
    });
    render(<InlineAgentPrompt />);
    expect(screen.getByRole("radiogroup", { name: "选择一个方向" })).toBeTruthy();
    const choices = screen.getAllByRole("radio") as HTMLButtonElement[];
    expect(choices.map((choice) => choice.tabIndex)).toEqual([0, -1, -1]);
    choices[0].focus();
    fireEvent.keyDown(choices[0], { key: "ArrowDown" });
    expect(document.activeElement).toBe(choices[1]);
    expect(choices[1].getAttribute("aria-checked")).toBe("true");
    expect(choices.map((choice) => choice.tabIndex)).toEqual([-1, 0, -1]);
    fireEvent.keyDown(choices[1], { key: "End" });
    expect(document.activeElement).toBe(choices[2]);
    fireEvent.keyDown(choices[2], { key: "ArrowRight" });
    expect(document.activeElement).toBe(choices[0]);
    fireEvent.keyDown(choices[0], { key: "ArrowUp" });
    expect(document.activeElement).toBe(choices[2]);
    fireEvent.keyDown(choices[2], { key: "Home" });
    expect(document.activeElement).toBe(choices[0]);
    expect(mocks.sendPromptResponseCommand).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "继续" }));
    expect(mocks.sendPromptResponseCommand).toHaveBeenCalledWith(expect.objectContaining({
      response: { subtype: "success", response: { answer: "ui" } },
    }));
  });

  it.each([{ isComposing: true }, { keyCode: 229 }])("keeps Enter inside IME composition before explicit answer submission: %j", (composition) => {
    useAppStore.setState({
      pendingAskUser: { requestId: "ime-answer", conversationId: "conv-inline", question: "描述你的想法" },
    });
    render(<InlineAgentPrompt />);
    const input = screen.getByRole("textbox", { name: "回答 Agent 的问题" }) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "继续完善编辑器" } });
    const allowsDefaultSubmission = fireEvent.keyDown(input, { key: "Enter", ...composition });
    expect(allowsDefaultSubmission).toBe(false);
    expect(input.value).toBe("继续完善编辑器");
    expect(mocks.sendPromptResponseCommand).not.toHaveBeenCalled();
    expect(fireEvent.keyDown(input, { key: "Enter", isComposing: false })).toBe(true);
    fireEvent.submit(input.form!);
    expect(mocks.sendPromptResponseCommand).toHaveBeenCalledWith(expect.objectContaining({
      response: { subtype: "success", response: { answer: "继续完善编辑器" } },
    }));
  });

  it("shows pending submission and locks the selected answer until the result arrives", async () => {
    let finish!: (result: { type: "command.result"; command: string; level: string; message: string; data: {} }) => void;
    mocks.sendPromptResponseCommand.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    useAppStore.setState({
      pendingAskUser: {
        requestId: "pending-answer", conversationId: "conv-inline", question: "选择一个方向",
        options: [{ label: "完善当前版本", value: "increment" }, { label: "重新设计", value: "redesign" }],
      },
    });
    render(<InlineAgentPrompt />);
    const choices = screen.getAllByRole("radio") as HTMLButtonElement[];
    fireEvent.click(choices[0]);
    fireEvent.click(screen.getByRole("button", { name: "继续" }));
    expect(screen.getByRole("status").textContent).toBe("正在提交…");
    expect(choices.every((choice) => choice.disabled)).toBe(true);
    expect((screen.getByRole("textbox") as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(choices[1]);
    expect(choices[0].getAttribute("aria-checked")).toBe("true");
    expect(mocks.sendPromptResponseCommand).toHaveBeenCalledOnce();
    await act(async () => finish({ type: "command.result", command: "control_response", level: "error", message: "连接中断，请重试", data: {} }));
    expect(screen.getByRole("alert").textContent).toBe("连接中断，请重试");
    expect(choices.every((choice) => !choice.disabled)).toBe(true);
    expect(choices[0].getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("status").textContent).toBe("选好后点击继续");
  });

  it("keeps plan reading keyboard accessible and preserves edited content after a rejected submission", async () => {
    let finish!: (result: { type: "command.result"; command: string; level: string; message: string; data: {} }) => void;
    mocks.sendPromptResponseCommand.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const plan = `# 计划\n\n${"- 完善一个交互细节\n".repeat(18)}`;
    useAppStore.setState({
      pendingApproval: {
        requestId: "readable-plan", conversationId: "conv-inline", toolName: "exit_plan_mode", args: { plan },
      },
    });
    render(<InlineAgentPrompt />);
    const document = screen.getByRole("region", { name: "计划内容" });
    const expand = screen.getByRole("button", { name: "展开计划" });
    expect(expand.getAttribute("aria-controls")).toBe(document.id);
    expect(document.getAttribute("tabindex")).toBeNull();
    fireEvent.click(expand);
    expect(document.tabIndex).toBe(0);
    fireEvent.click(screen.getByRole("button", { name: "编辑计划" }));
    const editor = screen.getByRole("textbox", { name: "编辑计划" }) as HTMLTextAreaElement;
    const revised = `${plan}\n- 保留现有动画`;
    fireEvent.change(editor, { target: { value: revised } });
    fireEvent.click(screen.getByRole("button", { name: "批准计划并开始实现" }));
    expect(editor.disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toBe("正在提交计划…");
    await act(async () => finish({ type: "command.result", command: "control_response", level: "error", message: "计划尚未接受", data: {} }));
    expect(editor.disabled).toBe(false);
    expect(editor.value).toBe(revised);
    expect(screen.getByRole("alert").textContent).toBe("计划尚未接受");
  });

  it("keeps the prompt focused on the question without a technical action disclosure", () => {
    useAppStore.setState({
      pendingAskUser: { requestId: "diagnostic-prompt", conversationId: "conv-inline", question: "继续吗？" },
    });
    render(<InlineAgentPrompt />);
    expect(screen.getByText("继续吗？")).toBeTruthy();
    expect(screen.queryByText("更多操作")).toBeNull();
    expect(screen.queryByRole("button", { name: "技术诊断" })).toBeNull();
  });

  it.each(["approval", "diff", "question"])("does not add %s technical controls to a scoped side prompt", (kind) => {
    const owner = { requestId: "side-diagnostic", conversationId: "side-owner" };
    useAppStore.setState({ conversationId: "parent-owner", inspectorEntries: [], inspectorFocus: null,
      ...(kind === "approval" ? { pendingApproval: { ...owner, toolName: "read_file", args: { path: "side.ts" } } }
        : kind === "diff" ? { pendingDiffReview: { ...owner, diff: "+side" } }
        : { pendingAskUser: { ...owner, question: "Continue the side task?" } }) });
    render(<InlineAgentPrompt conversationId="side-owner" />);
    expect(screen.queryByText("更多操作")).toBeNull();
    expect(screen.queryByRole("button", { name: "技术诊断" })).toBeNull();
    const state = useAppStore.getState();
    expect(state.inspectorFocus).toBeNull();
    expect(state.inspectorEntries).toEqual([]);
  });

  it.each(["option", "custom"])("resets a local %s answer when switching to a different request", (answerMode) => {
    const question = {
      requestId: "previous-question",
      conversationId: "conv-inline",
      question: "Previous question",
      options: [{ label: "Choose this", value: "chosen" }],
    };
    useAppStore.setState({ pendingAskUser: question });
    render(<InlineAgentPrompt />);
    if (answerMode === "option") {
      fireEvent.click(screen.getByRole("radio", { name: /Choose this/ }));
    } else {
      fireEvent.change(screen.getByRole("textbox", { name: "回答 Agent 的问题" }), { target: { value: "Previous answer" } });
    }
    expect((screen.getByRole("button", { name: "继续" }) as HTMLButtonElement).disabled).toBe(false);

    act(() => useAppStore.setState({
      pendingAskUser: { ...question, requestId: "next-question", question: "Next question" },
    }));
    expect(screen.getByText("Next question")).toBeTruthy();
    expect(screen.getByRole("radio", { name: /Choose this/ }).getAttribute("aria-checked")).toBe("false");
    expect((screen.getByRole("textbox", { name: "回答 Agent 的问题" }) as HTMLInputElement).value).toBe("");
    expect((screen.getByRole("button", { name: "继续" }) as HTMLButtonElement).disabled).toBe(true);
    expect(mocks.sendPromptResponseCommand).not.toHaveBeenCalled();
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
    expect((screen.getByRole("button", { name: "继续" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole("radio", { name: /Device code login/ }));
    expect(mocks.sendPromptResponseCommand).not.toHaveBeenCalled();
    expect(screen.getByRole("radio", { name: /Device code login/ }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "继续" }));
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

    expect(screen.getByText("npm run build")).toBeTruthy();
    expect(screen.getByTitle("url: https://example.com/noise")).toBeTruthy();
    expect(screen.queryByTitle("cwd: frontend")).toBeTruthy();
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

    expect(screen.getByText("curl https://example.com")).toBeTruthy();
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
