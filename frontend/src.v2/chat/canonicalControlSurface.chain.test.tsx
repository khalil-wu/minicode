/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  response: vi.fn(async () => ({ type: "command.result", command: "control_response", level: "success", message: "", data: {} })),
  external: vi.fn(async () => {}),
  preferences: vi.fn(async () => ({ urlOpenTarget: "external-browser", localUrlOpenTarget: "external-browser" })),
  notify: vi.fn(), toast: vi.fn(),
}));
vi.mock("../protocol/ws-outbox", async original => ({ ...(await original<Record<string, unknown>>()),
  sendPromptResponseCommand: mocks.response,
}));
vi.mock("../desktop/runtime", async original => ({ ...(await original<Record<string, unknown>>()),
  isDesktop: () => true, getDesktopPreferences: mocks.preferences, openExternal: mocks.external,
  desktop: () => ({ notify: mocks.notify }),
}));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));

import { useAppStore } from "../stores";
import { handleControlEvent } from "./controlEvents";
import { InlineAgentPrompt } from "./InlineAgentPrompt";
import { loadPromptDrafts, promptDraftKey } from "../stores/prompt-drafts";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { ActivityCell } from "./cells/ActivityCell";
import { AgentTimeline } from "../agent-loop/components/AgentTimeline";
import type { ServerEvent } from "../protocol/events";

const questions = [
  { id: "area", header: "Area", question: "Which area?", isOther: true, isSecret: false, options: [
    { label: "Parser", description: "Keep actual files." }, { label: "Runner", description: "Keep real exits." },
  ] },
  { id: "details", header: "Details", question: "Which details?", isOther: true, isSecret: false, options: [
    { label: "Bytes", description: "Exact bytes." }, { label: "Exit", description: "Actual exit." },
  ] },
];
const input = (turn = "turn-one", conversation = "owner", blocking = true): ServerEvent => ({
  type: "control_request", request_id: "same-model-call", conversation_id: conversation, turn_id: turn, message_id: `message-${turn}`, task_id: `task-${turn}`,
  replayed: true, request: { subtype: "request_user_input", tool_use_id: "same-model-call", questions, isBlocking: blocking },
});
const url = (): ServerEvent => ({ type: "control_request", request_id: "mcp-request", conversation_id: "owner", turn_id: "mcp-turn",
  message_id: "mcp-message", task_id: "mcp-task", replayed: true, request: { subtype: "elicitation", mode: "url", tool_use_id: "mcp-call",
    question: "Authenticate the actual MCP server.", mcp_server_name: "peer", url: "https://example.test/auth", elicitation_id: "peer-id", _meta: { retained: true } } });
beforeEach(() => {
  localStorage.removeItem("minicode.agentPromptDrafts");
  mocks.response.mockReset().mockResolvedValue({ type: "command.result", command: "control_response", level: "success", message: "", data: {} });
  mocks.external.mockReset().mockResolvedValue(undefined); mocks.preferences.mockClear(); mocks.toast.mockClear();
  useAppStore.setState({ conversationId: "owner", pendingConversationSwitchId: null, promptDrafts: {},
    pendingAskUser: null, askUserQueue: [], pendingApproval: null, approvalQueue: [], pendingDiffReview: null, diffReviewQueue: [],
    draft: "unsent main draft", attachments: [], runtimeSession: null, viewMode: "normal",
  });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("Source native command prefix approval", () => {
  it("projects the frozen array and digest through the actual approval menu and semantic response", async () => {
    const digest = "a".repeat(64);
    handleControlEvent({ type: "control_request", request_id: "native-exec", conversation_id: "owner",
      turn_id: "native-turn", message_id: "native-message", replayed: true,
      request: { subtype: "can_use_tool", tool_name: "run_command", tool_use_id: "native-exec",
        input: { command: "git status", login: false, sandbox_permissions: "require_escalated", justification: "raw argument reason" }, request_digest: digest,
        command: ["powershell.exe", "-Command", "__INTERNAL_WRAPPER__"], display_command: "git status", reason: "Inspect the actual worktree changes.",
        cwd: "C:/owned", source_agent: "run_internal", source_thread: "owner", proposed_execpolicy_amendment: ["git", "status"],
        available_decisions: ["approved", { approved_execpolicy_amendment: { proposed_execpolicy_amendment: ["git", "status"] } }, "abort"] } });
    const view = render(<InlineAgentPrompt />);
    expect(useAppStore.getState().pendingApproval?.proposedExecpolicyAmendment).toEqual(["git", "status"]);
    expect(useAppStore.getState().pendingApproval).toMatchObject({ displayCommand: "git status", approvalReason: "Inspect the actual worktree changes.", cwd: "C:/owned" });
    expect(screen.getByText("终端")).toBeTruthy();
    expect(screen.getByText("Inspect the actual worktree changes.")).toBeTruthy();
    expect(screen.getByLabelText("即将运行的命令").textContent).toBe("git status");
    expect(view.container.querySelector(".inline-approval-summary")).toBeNull();
    expect(view.container.querySelector(".inline-approval-escalation")).toBeNull();
    expect(screen.queryByText(/login|raw argument reason|__INTERNAL_WRAPPER__|来源：|C:\/owned/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /选择允许范围/ }));
    expect(screen.queryByRole("menuitem", { name: "允许此次对话" })).toBeNull();
    expect(screen.queryByRole("menuitemradio")).toBeNull();
    expect(screen.queryByRole("listbox")).toBeNull();
    const prefix = screen.getByRole("menuitem", { name: "允许类似命令" });
    expect(prefix.getAttribute("title")).toBeNull();
    fireEvent.pointerEnter(prefix.querySelector(".mc-tip")!);
    expect(screen.getByText("允许在此对话中使用以 git status 开头的命令")).toBeTruthy();
    fireEvent.click(prefix);
    await waitFor(() => expect(mocks.response).toHaveBeenCalledWith({ type: "control_response", request_id: "native-exec",
      conversation_id: "owner", turn_id: "native-turn", message_id: "native-message",
      response: { subtype: "success", response: { action: "approve", request_digest: digest,
        decision: { approved_execpolicy_amendment: { proposed_execpolicy_amendment: ["git", "status"] } } } } }));
    expect(useAppStore.getState().draft).toBe("unsent main draft");
  });
  it("does not invent a reusable prefix when the admitted model supplied no amendment", () => {
    handleControlEvent({ type: "control_request", request_id: "native-once", conversation_id: "owner", replayed: true,
      request: { subtype: "can_use_tool", tool_name: "run_command", tool_use_id: "native-once",
        input: { command: "git status" }, command: ["git", "status"], display_command: "git status", proposed_execpolicy_amendment: null,
        available_decisions: ["approved", "abort"] } });
    render(<InlineAgentPrompt />);
    expect(screen.queryByRole("button", { name: /选择允许范围/ })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "允许类似命令" })).toBeNull();
  });
  it.each(["line\nnext", "line\rnext"])("keeps Source's allow-once surface when the real prefix contains a line break %j", token => {
    const prefix = ["tool", token];
    handleControlEvent({ type: "control_request", request_id: "native-multiline-prefix", conversation_id: "owner", replayed: true,
      request: { subtype: "can_use_tool", tool_name: "run_command", tool_use_id: "native-multiline-prefix", input: { command: "tool" },
        command: ["tool"], display_command: "tool", proposed_execpolicy_amendment: prefix,
        available_decisions: ["approved", { approved_execpolicy_amendment: { proposed_execpolicy_amendment: prefix } }, "abort"] } });
    render(<InlineAgentPrompt />);
    expect(screen.queryByRole("button", { name: /选择允许范围/ })).toBeNull();
    expect(screen.getByRole("button", { name: "允许使用工具" })).toBeTruthy();
  });
  it("uses Source's quoted prefix only in the tooltip and sends the exact original token array", async () => {
    const prefix = ["C:/work folder/tool", "$state", "O'Reilly"];
    handleControlEvent({ type: "control_request", request_id: "native-quoted-prefix", conversation_id: "owner", replayed: true,
      request: { subtype: "can_use_tool", tool_name: "run_command", tool_use_id: "native-quoted-prefix", input: { command: "actual public command" },
        command: ["powershell.exe", "-Command", "actual public command"], display_command: "actual public command", proposed_execpolicy_amendment: prefix,
        available_decisions: ["approved", { approved_execpolicy_amendment: { proposed_execpolicy_amendment: prefix } }, "abort"] } });
    render(<InlineAgentPrompt />);
    fireEvent.click(screen.getByRole("button", { name: /选择允许范围/ }));
    const action = screen.getByRole("menuitem", { name: "允许类似命令" });
    fireEvent.pointerEnter(action.querySelector(".mc-tip")!);
    expect(screen.getByText(`允许在此对话中使用以 "C:/work folder/tool" '$state' "O'Reilly" 开头的命令`)).toBeTruthy();
    fireEvent.click(action);
    await waitFor(() => expect(mocks.response).toHaveBeenCalledWith(expect.objectContaining({
      response: { subtype: "success", response: { action: "approve", decision: {
        approved_execpolicy_amendment: { proposed_execpolicy_amendment: prefix },
      } } },
    })));
  });
  it.each(["normal", "verbose", "summary"] as const)("keeps the Source COMMANDS default independent of MiniCode's legacy %s view mode", viewMode => {
    useAppStore.setState({ viewMode });
    handleControlEvent({ type: "control_request", request_id: "native-default", conversation_id: "owner", replayed: true,
      request: { subtype: "can_use_tool", tool_name: "run_command", tool_use_id: "native-default", input: { command: "git status" },
        command: ["git", "status"], display_command: "git status", reason: "  ", available_decisions: ["approved", "abort"] } });
    render(<InlineAgentPrompt />);
    expect(screen.getByText("终端")).toBeTruthy();
    expect(screen.getByText("允许 MiniCode 运行此命令？")).toBeTruthy();
    expect(screen.getByLabelText("即将运行的命令").textContent).toBe("git status");
  });
  it("opens a native action menu from the keyboard and dismisses Escape without denying the actual request", () => {
    handleControlEvent({ type: "control_request", request_id: "native-keyboard", conversation_id: "owner", replayed: true,
      request: { subtype: "can_use_tool", tool_name: "run_command", tool_use_id: "native-keyboard", input: { command: "git status" },
        command: ["git", "status"], display_command: "git status", proposed_execpolicy_amendment: ["git"],
        available_decisions: ["approved", { approved_execpolicy_amendment: { proposed_execpolicy_amendment: ["git"] } }, "abort"] } });
    render(<InlineAgentPrompt />);
    const trigger = screen.getByRole("button", { name: /选择允许范围/ });
    trigger.focus(); fireEvent.keyDown(trigger, { key: "ArrowDown" });
    expect(screen.getAllByRole("menuitem").map(item => item.textContent)).toEqual(["允许一次", "允许类似命令"]);
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(mocks.response).not.toHaveBeenCalled();
  });
  it("uses the Source three-line disclosure while retaining the entire exact command", () => {
    const scroll = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(64);
    const getStyle = window.getComputedStyle.bind(window);
    const style = vi.spyOn(window, "getComputedStyle").mockImplementation((element, pseudo) => {
      const computed = getStyle(element, pseudo);
      if (element.parentElement?.classList.contains("inline-approval-native-command-text")) computed.lineHeight = "16px";
      return computed;
    });
    try {
      const command = "Write-Output one\nWrite-Output two\nWrite-Output three\nWrite-Output four";
      handleControlEvent({ type: "control_request", request_id: "native-long", conversation_id: "owner", replayed: true,
        request: { subtype: "can_use_tool", tool_name: "run_command", tool_use_id: "native-long", input: { command },
          command: ["powershell.exe", "-Command", command], display_command: command, available_decisions: ["approved", "abort"] } });
      render(<InlineAgentPrompt />);
      const preview = screen.getByLabelText("即将运行的命令");
      expect(preview.textContent).toBe(command);
      expect(preview.firstElementChild?.getAttribute("style")).toContain("max-height: 3lh");
      fireEvent.click(screen.getByRole("button", { name: "展开" }));
      expect(preview.firstElementChild?.getAttribute("style")).not.toContain("max-height");
      expect(screen.getByRole("button", { name: "收起" }).getAttribute("aria-expanded")).toBe("true");
      fireEvent.click(screen.getByRole("button", { name: "收起" }));
      expect(preview.textContent).toBe(command);
      expect(screen.getByRole("button", { name: "展开" }).getAttribute("aria-expanded")).toBe("false");
    } finally { scroll.mockRestore(); style.mockRestore(); }
  });
});

describe("source request-input wizard through wire, owner store and semantic response", () => {
  it("shows the Source recommended badge while numeric selection submits the untouched original labels", async () => {
    handleControlEvent({ ...input(), request: { subtype: "request_user_input", tool_use_id: "same-model-call", isBlocking: true,
      questions: questions.map((question, index) => index === 0 ? { ...question, options: [
        { label: "Parser (Recommended)", description: "Keep actual files." }, question.options[1],
      ] } : question) } } as ServerEvent);
    render(<InlineAgentPrompt />);
    const recommended = screen.getByRole("radio", { name: "Parser (Recommended)" });
    expect(recommended.textContent).toContain("推荐");
    expect(recommended.textContent).not.toContain("(Recommended)");
    vi.useFakeTimers();
    fireEvent.keyDown(recommended, { key: "1" });
    expect(recommended.getAttribute("data-advancing")).toBe("true");
    expect(mocks.response).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(180));
    fireEvent.keyDown(screen.getByRole("radio", { name: "Bytes" }), { key: "1" });
    act(() => vi.advanceTimersByTime(180));
    vi.useRealTimers();
    await waitFor(() => expect(mocks.response).toHaveBeenCalledWith(expect.objectContaining({
      conversation_id: "owner", turn_id: "turn-one", message_id: "message-turn-one", task_id: "task-turn-one",
      response: { subtype: "success", response: { answers: { area: { answers: ["Parser (Recommended)"] }, details: { answers: ["Bytes"] } } } },
    })));
    expect(useAppStore.getState().draft).toBe("unsent main draft");
  });

  it("moves from the last choice or its numbered Other shortcut into freeform without consuming typed digits", () => {
    handleControlEvent(input()); render(<InlineAgentPrompt />);
    const first = screen.getByRole("radio", { name: "Parser" }), last = screen.getByRole("radio", { name: "Runner" });
    fireEvent.keyDown(first, { key: "ArrowDown" });
    expect(document.activeElement).toBe(last);
    fireEvent.keyDown(last, { key: "ArrowDown" });
    const editor = screen.getByRole("textbox", { name: "回答当前问题" });
    expect(document.activeElement).toBe(editor);
    expect(last.getAttribute("aria-checked")).toBe("false");
    act(() => first.focus());
    fireEvent.keyDown(first, { key: "3" });
    expect(document.activeElement).toBe(editor);
    fireEvent.change(editor, { target: { value: "Literal digit 2" } });
    fireEvent.keyDown(editor, { key: "2" });
    expect((editor as HTMLTextAreaElement).value).toBe("Literal digit 2");
    expect(mocks.response).not.toHaveBeenCalled();
    expect(screen.getByText("Which area?")).toBeTruthy();
  });

  it("keeps the original action label disabled while awaiting acceptance and restores it after a real refusal", async () => {
    let finish!: (value: any) => void;
    mocks.response.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    handleControlEvent(input()); render(<InlineAgentPrompt />);
    fireEvent.click(screen.getByRole("button", { name: "跳过" }));
    fireEvent.click(screen.getByRole("button", { name: "跳过" }));
    const action = screen.getByRole("button", { name: "跳过" });
    expect((action as HTMLButtonElement).disabled).toBe(true);
    expect(action.closest("form")!.getAttribute("aria-busy")).toBe("true");
    expect(action.querySelector("svg")).toBeNull();
    await act(async () => finish({ type: "command.result", command: "control_response", level: "error", message: "Original request refused", data: {} }));
    expect(screen.getByRole("alert").textContent).toBe("Original request refused");
    expect((screen.getByRole("button", { name: "跳过" }) as HTMLButtonElement).disabled).toBe(false);
    expect(useAppStore.getState().pendingAskUser).toMatchObject({ conversationId: "owner", turnId: "turn-one" });
    expect(useAppStore.getState().draft).toBe("unsent main draft");
  });

  it("retains every typed question and description, advances after 180ms and submits labels plus literal custom text", async () => {
    handleControlEvent(input()); render(<InlineAgentPrompt />);
    expect(screen.getByText("Which area?")).toBeTruthy(); expect(screen.getByText("Keep actual files.")).toBeTruthy();
    expect(screen.getByRole("radio", { name: /Parser/ }).getAttribute("aria-checked")).toBe("true");
    vi.useFakeTimers(); fireEvent.click(screen.getByRole("radio", { name: /Runner/ }));
    expect(screen.queryByText("Which details?")).toBeNull();
    act(() => vi.advanceTimersByTime(179)); expect(screen.queryByText("Which details?")).toBeNull();
    act(() => vi.advanceTimersByTime(1)); expect(screen.getByText("Which details?")).toBeTruthy(); vi.useRealTimers();
    fireEvent.change(screen.getByRole("textbox", { name: "回答当前问题" }), { target: { value: "Exact 原始内容" } });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "回答当前问题" }), { key: "Enter" });
    await waitFor(() => expect(mocks.response).toHaveBeenCalledWith({ type: "control_response", request_id: "same-model-call",
      conversation_id: "owner", turn_id: "turn-one", message_id: "message-turn-one", task_id: "task-turn-one",
      response: { subtype: "success", response: { answers: { area: { answers: ["Runner"] }, details: { answers: ["Exact 原始内容"] } } } },
    }));
    await waitFor(() => expect(useAppStore.getState().pendingAskUser).toBeNull());
    expect(useAppStore.getState().draft).toBe("unsent main draft"); expect(loadPromptDrafts()).toEqual({});
  });

  it("keeps Skip distinct from cancellation and never submits a preselected option automatically", async () => {
    handleControlEvent(input()); render(<InlineAgentPrompt />);
    expect(mocks.response).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "跳过" }));
    expect(screen.getByText("Which details?")).toBeTruthy(); expect(mocks.response).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "跳过" }));
    await waitFor(() => expect(mocks.response).toHaveBeenCalledWith(expect.objectContaining({ response: { subtype: "success", response: { answers: {} } } })));
  });

  it("sends an owner-bound cancel for Escape and restores the original draft only after acceptance", async () => {
    handleControlEvent(input()); render(<InlineAgentPrompt />);
    fireEvent.keyDown(screen.getByText("Which area?").closest("form")!, { key: "Escape" });
    await waitFor(() => expect(mocks.response).toHaveBeenCalledWith(expect.objectContaining({ turn_id: "turn-one", task_id: "task-turn-one",
      response: { subtype: "success", response: { action: "cancel" } } })));
    await waitFor(() => expect(useAppStore.getState().pendingAskUser).toBeNull());
    expect(useAppStore.getState().draft).toBe("unsent main draft");
  });

  it("restores an edited custom answer on replay and retains it after a semantic refusal", async () => {
    handleControlEvent(input()); let view = render(<InlineAgentPrompt />);
    fireEvent.change(screen.getByRole("textbox", { name: "回答当前问题" }), { target: { value: "Literal answer" } });
    view.unmount(); handleControlEvent(input()); view = render(<InlineAgentPrompt />);
    expect((screen.getByRole("textbox", { name: "回答当前问题" }) as HTMLTextAreaElement).value).toBe("Literal answer");
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    mocks.response.mockResolvedValueOnce({ type: "command.result", command: "control_response", level: "error", message: "Actual owner refusal", data: {} });
    fireEvent.click(screen.getByRole("button", { name: "跳过" }));
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Actual owner refusal");
    expect(useAppStore.getState().pendingAskUser?.turnId).toBe("turn-one");
    expect(Object.values(loadPromptDrafts())[0].userInputDraft?.answers.area.freeformText).toBe("Literal answer");
  });

  it("does not let the same call ID in another turn or conversation borrow a draft or a late ACK", async () => {
    let finish!: (value: any) => void;
    mocks.response.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    handleControlEvent(input()); const view = render(<InlineAgentPrompt />);
    fireEvent.change(screen.getByRole("textbox", { name: "回答当前问题" }), { target: { value: "old answer" } });
    fireEvent.click(screen.getByRole("button", { name: "下一步" })); fireEvent.click(screen.getByRole("button", { name: "跳过" }));
    const old = useAppStore.getState().pendingAskUser!;
    act(() => useAppStore.getState().clearAskUser(old.requestId, old.conversationId,
      { turnId: old.turnId, messageId: old.messageId, taskId: old.taskId }));
    act(() => { handleControlEvent(input("turn-two")); handleControlEvent(input("foreign-turn", "foreign")); });
    expect((screen.getByRole("textbox", { name: "回答当前问题" }) as HTMLTextAreaElement).value).toBe("");
    fireEvent.change(screen.getByRole("textbox", { name: "回答当前问题" }), { target: { value: "new answer" } });
    await act(async () => finish({ type: "command.result", command: "control_response", level: "success", message: "", data: {} }));
    expect(useAppStore.getState().pendingAskUser?.turnId).toBe("turn-two");
    expect(useAppStore.getState().askUserQueue[0].conversationId).toBe("foreign");
    expect((screen.getByRole("textbox", { name: "回答当前问题" }) as HTMLTextAreaElement).value).toBe("new answer");
    view.unmount(); render(<InlineAgentPrompt />);
    expect((screen.getByRole("textbox", { name: "回答当前问题" }) as HTMLTextAreaElement).value).toBe("new answer");
  });
});

describe("MCP URL surface through the actual preferred-link consumer", () => {
  it("requires a successful link opening, then a separate explicit Continue and sends no content", async () => {
    handleControlEvent(url()); render(<InlineAgentPrompt />);
    mocks.external.mockRejectedValueOnce(new Error("Native browser refused"));
    fireEvent.click(screen.getByRole("button", { name: "打开链接" }));
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith("Native browser refused", "error"));
    expect(screen.getByRole("button", { name: "打开链接" })).toBeTruthy(); expect(mocks.response).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "打开链接" }));
    const next = await screen.findByRole("button", { name: "继续" });
    expect(mocks.external).toHaveBeenCalledWith("https://example.test/auth"); expect(mocks.response).not.toHaveBeenCalled();
    fireEvent.click(next);
    await waitFor(() => expect(mocks.response).toHaveBeenCalledWith({ type: "control_response", request_id: "mcp-request",
      conversation_id: "owner", turn_id: "mcp-turn", message_id: "mcp-message", task_id: "mcp-task",
      response: { subtype: "success", response: { action: "accept" } },
    }));
  });

  it.each(["decline", "cancel"])("retains the exact %s action and SDK identity through replay", async action => {
    handleControlEvent(url());
    expect(useAppStore.getState().pendingAskUser?.elicitation).toEqual({ mode: "url", url: "https://example.test/auth", elicitationId: "peer-id", serverName: "peer", meta: { retained: true } });
    render(<InlineAgentPrompt />);
    if (action === "decline") fireEvent.click(screen.getByRole("button", { name: "拒绝" }));
    else fireEvent.keyDown(screen.getByText("Authenticate the actual MCP server.").closest("form")!, { key: "Escape" });
    await waitFor(() => expect(mocks.response).toHaveBeenCalledWith(expect.objectContaining({ response: { subtype: "success", response: { action } } })));
    expect(mocks.external).not.toHaveBeenCalled();
  });

  it("does not open a stale request after the active conversation changes while preferences are loading", async () => {
    let preferenceResult!: (value: any) => void;
    mocks.preferences.mockImplementationOnce(() => new Promise(resolve => { preferenceResult = resolve; }));
    handleControlEvent(url()); render(<InlineAgentPrompt />); fireEvent.click(screen.getByRole("button", { name: "打开链接" }));
    act(() => useAppStore.setState({ conversationId: "foreign" }));
    await act(async () => preferenceResult({ urlOpenTarget: "external-browser", localUrlOpenTarget: "external-browser" }));
    expect(mocks.external).not.toHaveBeenCalled(); expect(mocks.response).not.toHaveBeenCalled();
    expect(useAppStore.getState().pendingAskUser?.conversationId).toBe("owner");
  });
});

describe("canonical user-input standalone process projection", () => {
  it.each([{}, { area: { answers: ["Parser"] } }])("shows answered questions and unprovided answers without raw JSON or generic tool chrome", answers => {
    const records = [
      { id: "read", name: "read_file", args: { file_path: "real.py" }, status: "success" as const, startedAt: 1 },
      { id: "input", name: "request_user_input", args: { questions }, status: "success" as const, startedAt: 2, outputPreview: JSON.stringify({ answers }) },
      { id: "read-after", name: "read_file", args: { file_path: "after.py" }, status: "success" as const, startedAt: 3 },
    ];
    const turn = projectMessagesToTurns([{ id: "message", role: "assistant", content: "", timestamp: 1, artifacts: [],
      blocks: records.map(record => ({ type: "tool_call" as const, record })) }], false)[0];
    render(<AgentTimeline cells={turn.committedCells} renderCell={({ cell, key }) => cell.kind === "activity" ? <ActivityCell key={key} cell={cell} /> : null} />);
    const question = screen.getByRole("button", { name: "已询问 2 个问题" }); fireEvent.click(question);
    expect(screen.getByText("Which area?")).toBeTruthy(); expect(screen.getByText("Which details?")).toBeTruthy();
    expect(screen.getAllByText("未提供回答").length).toBe(Object.keys(answers).length ? 1 : 2);
    expect(screen.queryByText("request_user_input")).toBeNull(); expect(screen.queryByText(/\{"answers"/)).toBeNull(); expect(screen.queryByText("你的回答")).toBeNull();
  });

  it("keeps a real rejected control result neutral and readable in its own disclosure", () => {
    render(<ActivityCell cell={{ kind: "activity", id: "refused", activityKind: "genericTool", title: "request_user_input", status: "failed", collapsed: true,
      toolCallRecords: [{ id: "refused", name: "request_user_input", args: { questions }, status: "failed", startedAt: 1, outputPreview: "request_user_input can only be used by the root thread" }] }} />);
    fireEvent.click(screen.getByRole("button", { name: "提问" }));
    expect(screen.getByText("request_user_input can only be used by the root thread")).toBeTruthy(); expect(screen.queryByText("失败")).toBeNull();
  });
});
