/* @vitest-environment jsdom */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { handleCommandResultEvent } from "./commandResultEvents";
import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";
import { pushToast } from "../overlays/ToastContainer";

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(() => true),
}));

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: vi.fn(),
}));

describe("handleCommandResultEvent", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      conversationId: "conv-command",
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: false,
      agentProgress: [],
      budgetBuckets: [],
      totalBudgetPercent: 0,
      contextUsage: null,
      runtimeCapabilities: null,
      skillsMarketplaceOpen: false,
      quickOpenVisible: false,
      agentEditorOpen: false,
      settingsOpen: false,
      pluginCommandPanelOpen: false,
      pluginCommandPanelPayload: null,
      subagents: [],
    });
  });

  it("surfaces inspect-type results as an ephemeral toast, never as a persistent transcript message", () => {
    expect(handleCommandResultEvent({
      type: "command.result",
      command: "status",
      level: "info",
      message: "Runtime status: model gpt-5 | mode auto",
    } as ServerEvent)).toBe(true);

    const state = useAppStore.getState();
    // The result must NOT pollute the conversation transcript.
    expect(state.messages).toEqual([]);
    // It is shown transiently via toast...
    expect(pushToast).toHaveBeenCalledWith(
      "/status — Runtime status: model gpt-5 | mode auto",
      "info",
      expect.any(Number),
    );
    // ...and recorded in the compact activity trace for later review.
    expect(state.agentProgress).toEqual([
      expect.objectContaining({
        id: "command-result-status",
        stage: "status",
        status: "completed",
        label: "/status",
        message: "Runtime status: model gpt-5 | mode auto",
        visibility: "compact",
      }),
    ]);
  });

  it("routes error-level results to an error toast", () => {
    expect(handleCommandResultEvent({
      type: "command.result",
      command: "permissions",
      level: "error",
      message: "Unknown permission rule",
    } as ServerEvent)).toBe(true);

    expect(pushToast).toHaveBeenCalledWith(
      "/permissions — Unknown permission rule",
      "error",
      expect.any(Number),
    );
    expect(useAppStore.getState().messages).toEqual([]);
  });

  it("keeps usage budget side effects while surfacing the result transiently", () => {
    expect(handleCommandResultEvent({
      type: "command.result",
      command: "usage",
      level: "success",
      message: "Usage: context 20/100 tokens",
      data: {
        budget: {
          used: 20,
          total: 100,
          breakdown: { prompt: 12, tools: 8 },
        },
      },
    } as ServerEvent)).toBe(true);

    const state = useAppStore.getState();
    // Data side effects (budget ring) are preserved — they are functionality, not noise.
    expect(state.contextUsage).toMatchObject({ used: 20, limit: 100 });
    expect(state.totalBudgetPercent).toBe(0.2);
    // But the result itself stays out of the transcript.
    expect(state.messages).toEqual([]);
    expect(pushToast).toHaveBeenCalledWith(
      "/usage — Usage: context 20/100 tokens",
      "info",
      expect.any(Number),
    );
  });

  it("does not apply usage budget data from an inactive conversation", () => {
    expect(handleCommandResultEvent({
      type: "command.result",
      command: "usage",
      level: "success",
      message: "Usage: context 90/100 tokens",
      data: {
        conversation_id: "conv-other",
        budget: {
          used: 90,
          total: 100,
          breakdown: { prompt: 90 },
        },
      },
    } as ServerEvent)).toBe(true);

    const state = useAppStore.getState();
    expect(state.contextUsage).toBeNull();
    expect(state.budgetBuckets).toEqual([]);
    expect(state.totalBudgetPercent).toBe(0);
    expect(pushToast).toHaveBeenCalledWith(
      "/usage — Usage: context 90/100 tokens",
      "info",
      expect.any(Number),
    );
  });

  it("executes whitelisted plugin ui actions from command results", () => {
    expect(handleCommandResultEvent({
      type: "command.result",
      command: "agent-ui",
      level: "info",
      message: "Opening plugin command: /agent-ui.",
      data: { ui_action: "open_agent_editor" },
    } as ServerEvent)).toBe(true);

    expect(useAppStore.getState().agentEditorOpen).toBe(true);
    expect(pushToast).toHaveBeenCalledWith(
      "/agent-ui — Opening plugin command: /agent-ui.",
      "info",
      expect.any(Number),
    );
  });

  it("keeps disabled feature-gated ui actions closed", () => {
    useAppStore.setState({
      runtimeCapabilities: {
        feature_flags: {
          agent_editor: { enabled: false, source: "settings" },
          global_search: { enabled: false, source: "settings" },
        },
      },
    });

    expect(handleCommandResultEvent({
      type: "command.result",
      command: "agent-ui",
      level: "info",
      message: "Opening plugin command: /agent-ui.",
      data: { ui_action: "open_agent_editor" },
    } as ServerEvent)).toBe(true);
    expect(handleCommandResultEvent({
      type: "command.result",
      command: "files-ui",
      level: "info",
      message: "Opening plugin command: /files-ui.",
      data: { ui_action: "open_quick_open" },
    } as ServerEvent)).toBe(true);

    expect(useAppStore.getState().agentEditorOpen).toBe(false);
    expect(useAppStore.getState().quickOpenVisible).toBe(false);
  });

  it.each([
    ["info", "sent"],
    ["error", "failed"],
  ] as const)("projects send_message %s results onto optimistic delivery state", (level, expected) => {
    useAppStore.setState({
      subagents: [{
        id: "subagent-delivery",
        role: "reviewer",
        status: "done",
        messages: [{
          messageId: "message-delivery",
          senderId: "user",
          recipientId: "subagent-delivery",
          content: "Review this",
          createdAt: Date.now(),
          deliveryStatus: "sending",
        }],
      }],
    });

    handleCommandResultEvent({
      type: "command.result",
      command: "send_message",
      level,
      message: level === "error" ? "Resume failed" : "Message sent",
      data: {
        recipient: "subagent-delivery",
        message_id: "message-delivery",
      },
    } as ServerEvent);

    expect(useAppStore.getState().subagents[0].messages?.[0].deliveryStatus).toBe(expected);
  });

  it.each(["browser", "diff", "artifacts"] as const)(
    "opens the existing %s right-stack destination",
    (tab) => {
      useAppStore.setState({ rightPanelOpen: false, rightStackTab: "tasks" });

      expect(handleCommandResultEvent({
        type: "command.result",
        command: `open-${tab}`,
        level: "info",
        message: `Opening ${tab}.`,
        data: { ui_action: `open_right_stack:${tab}` },
      } as ServerEvent)).toBe(true);

      expect(useAppStore.getState()).toMatchObject({
        rightPanelOpen: true,
        rightStackTab: tab,
      });
    },
  );

  it("routes legacy plan and terminal destinations to their canonical surfaces", () => {
    useAppStore.setState({
      rightPanelOpen: false,
      rightStackTab: "preview",
      dockCollapsed: true,
      activeBottomTab: "git",
    });

    handleCommandResultEvent({
      type: "command.result",
      command: "open-plan",
      level: "info",
      message: "Opening plan.",
      data: { ui_action: "open_right_stack:plan" },
    } as ServerEvent);
    expect(useAppStore.getState()).toMatchObject({ rightPanelOpen: true, rightStackTab: "tasks" });

    handleCommandResultEvent({
      type: "command.result",
      command: "open-terminal",
      level: "info",
      message: "Opening terminal.",
      data: { ui_action: "open_right_stack:terminal" },
    } as ServerEvent);
    expect(useAppStore.getState()).toMatchObject({ dockCollapsed: false, activeBottomTab: "terminal" });
  });

  it.each([
    ["mcp", "connectors"],
    ["model", "provider"],
    ["plugins", "plugins"],
    ["keyboard", "shortcuts"],
    ["workspaceGit", "workspaceGit"],
    ["workspace-git", "workspaceGit"],
    ["git", "workspaceGit"],
    ["automations", "scheduler"],
  ])("routes the %s settings alias to %s", async (alias, expectedTab) => {
    expect(handleCommandResultEvent({
      type: "command.result",
      command: "settings",
      level: "info",
      message: "Opening settings.",
      data: { ui_action: `open_settings:${alias}` },
    } as ServerEvent)).toBe(true);

    expect(useAppStore.getState().settingsOpen).toBe(true);
    expect(useAppStore.getState().settingsTab).toBe(expectedTab);
  });

  it("downloads a versioned conversation export from the command result", () => {
    const createObjectURL = vi.fn(() => "blob:minicode-export");
    const revokeObjectURL = vi.fn();
    const click = vi.fn();
    const remove = vi.fn();
    const appendChild = vi.fn();
    vi.stubGlobal("document", {
      createElement: vi.fn(() => ({ href: "", download: "", style: {}, click, remove })),
      body: { appendChild },
    });
    vi.stubGlobal("window", { setTimeout: (callback: () => void) => { callback(); return 0; } });
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });

    expect(handleCommandResultEvent({
      type: "command.result",
      command: "conversation.export",
      level: "success",
      message: "Conversation export is ready to download.",
      data: {
        filename: "tree:unsafe?.json",
        mime_type: "application/json;charset=utf-8",
        content: '{"schema":"minicode.conversation.export","version":1}',
      },
    } as ServerEvent)).toBe(true);

    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(click).toHaveBeenCalledOnce();
    expect(appendChild).toHaveBeenCalledOnce();
    expect(remove).toHaveBeenCalledOnce();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:minicode-export");
    vi.unstubAllGlobals();
  });
});
