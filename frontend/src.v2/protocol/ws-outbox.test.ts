import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  LONG_COMMAND_RESULT_TIMEOUT_MS,
  rejectClientCommandResult,
  registerWebSocketSender,
  resetPendingCommandResultsForTests,
  resolveClientCommandResult,
  sendClientCommand,
  sendClientCommandAwaitResult,
  sendConversationDeleteCommand,
  sendPromptResponseCommand,
} from "./ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import {
  embeddedBrowserCloseConversation,
  isDesktop,
  ptyKillConversation,
} from "../desktop/runtime";

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: vi.fn(),
}));

vi.mock("../desktop/runtime", () => ({
  embeddedBrowserCloseConversation: vi.fn(),
  isDesktop: vi.fn(),
  ptyKillConversation: vi.fn(),
}));

describe("ws-outbox", () => {
  beforeEach(() => {
    resetPendingCommandResultsForTests();
    registerWebSocketSender(null);
    vi.clearAllMocks();
    vi.mocked(isDesktop).mockReturnValue(false);
    vi.mocked(embeddedBrowserCloseConversation).mockResolvedValue(0);
    vi.mocked(ptyKillConversation).mockResolvedValue(0);
  });

  it("reports offline commands instead of silently succeeding", () => {
    const sent = sendClientCommand({ type: "conversation.list" });

    expect(sent).toBe(false);
    expect(pushToast).toHaveBeenCalledWith(
      "操作失败：连接已断开。",
      "error",
      3000,
    );
  });

  it("returns false when the registered sender rejects the command", () => {
    registerWebSocketSender(() => false);

    const sent = sendClientCommand({ type: "conversation.list" });

    expect(sent).toBe(false);
    expect(pushToast).toHaveBeenCalledOnce();
  });

  it("can send background commands silently while offline", () => {
    const sent = sendClientCommand({ type: "conversation.list" }, { silent: true });

    expect(sent).toBe(false);
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("correlates authoritative command results by client command id and command name", async () => {
    const commands: Array<Record<string, unknown>> = [];
    registerWebSocketSender((command) => {
      commands.push(command as unknown as Record<string, unknown>);
      return true;
    });

    const pending = sendClientCommandAwaitResult(
      { type: "permissions.content_rule.add", rule: "run_command(git status:*)", deny: false },
      "permissions.content_rule.add",
    );
    const clientCommandId = String(commands[0]?.client_command_id || "");

    expect(resolveClientCommandResult({
      type: "command.result",
      command: "permissions.content_rule.remove",
      level: "success",
      message: "wrong command",
      data: { client_command_id: clientCommandId },
    })).toBe(false);

    const result = {
      type: "command.result" as const,
      command: "permissions.content_rule.add",
      level: "success",
      message: "saved",
      data: { client_command_id: clientCommandId },
    };
    expect(resolveClientCommandResult(result)).toBe(true);
    await expect(pending).resolves.toEqual(result);
  });

  it("rejects a pending authoritative result when the backend rejects command ownership", async () => {
    const commands: Array<Record<string, unknown>> = [];
    registerWebSocketSender((command) => {
      commands.push(command as unknown as Record<string, unknown>);
      return true;
    });

    const pending = sendClientCommandAwaitResult(
      { type: "permissions.content_rule.add", rule: "edit_file(src/**)", deny: false },
      "permissions.content_rule.add",
    );
    const clientCommandId = String(commands[0]?.client_command_id || "");

    expect(rejectClientCommandResult(clientCommandId, "command.persistence")).toBe(true);
    await expect(pending).rejects.toThrow("command.persistence");
  });

  it("sends control responses without registering a command-result timeout", async () => {
    const sender = vi.fn(() => true);
    registerWebSocketSender(sender);

    await expect(sendPromptResponseCommand({
      type: "control_response",
      request_id: "control-1",
      conversation_id: "conversation-1",
      response: {
        subtype: "success",
        response: { action: "approve" },
      },
    })).resolves.toBeNull();

    expect(sender).toHaveBeenCalledWith({
      type: "control_response",
      request_id: "control-1",
      conversation_id: "conversation-1",
      response: {
        subtype: "success",
        response: { action: "approve" },
      },
    });
    expect(sender.mock.calls[0]?.[0]).not.toHaveProperty("client_command_id");
  });

  it("rejects control responses immediately when the websocket sender refuses them", async () => {
    registerWebSocketSender(() => false);

    await expect(sendPromptResponseCommand({
      type: "control_cancel_request",
      request_id: "control-2",
      conversation_id: "conversation-1",
    })).rejects.toThrow("连接已断开");
  });

  it("keeps waiting for authoritative command results for legacy prompt responses", async () => {
    const commands: Array<Record<string, unknown>> = [];
    registerWebSocketSender((command) => {
      commands.push(command as unknown as Record<string, unknown>);
      return true;
    });

    const pending = sendPromptResponseCommand({
      type: "approval",
      tool_call_id: "legacy-approval",
      conversation_id: "conversation-1",
      action: "approve",
    });
    const clientCommandId = String(commands[0]?.client_command_id || "");
    const observed = vi.fn();
    void pending.then(observed);
    await Promise.resolve();

    expect(clientCommandId).not.toBe("");
    expect(observed).not.toHaveBeenCalled();

    const result = {
      type: "command.result" as const,
      command: "approval",
      level: "success",
      message: "accepted",
      data: { client_command_id: clientCommandId },
    };
    expect(resolveClientCommandResult(result)).toBe(true);
    await expect(pending).resolves.toEqual(result);
  });

  it("sends authoritative conversation deletion before desktop cleanup settles", async () => {
    const lifecycle: string[] = [];
    let resolvePtyCleanup!: (value: number) => void;
    let resolveBrowserCleanup!: (value: number) => void;
    vi.mocked(isDesktop).mockReturnValue(true);
    vi.mocked(ptyKillConversation).mockImplementation(() => {
      lifecycle.push("pty-cleanup");
      return new Promise<number>((resolve) => {
        resolvePtyCleanup = resolve;
      });
    });
    vi.mocked(embeddedBrowserCloseConversation).mockImplementation(() => {
      lifecycle.push("browser-cleanup");
      return new Promise<number>((resolve) => {
        resolveBrowserCleanup = resolve;
      });
    });
    registerWebSocketSender((command) => {
      lifecycle.push("delete-command");
      queueMicrotask(() => resolveClientCommandResult({
        type: "command.result",
        command: "conversation.delete",
        level: "success",
        message: "",
        data: { client_command_id: command.client_command_id },
      }));
      return true;
    });

    const deletion = sendConversationDeleteCommand({
      type: "conversation.delete",
      conversation_id: "conv_delete_owner",
    });

    expect(lifecycle).toEqual(["delete-command", "pty-cleanup", "browser-cleanup"]);
    await expect(deletion).resolves.toBe(true);

    expect(ptyKillConversation).toHaveBeenCalledWith("conv_delete_owner");
    expect(embeddedBrowserCloseConversation).toHaveBeenCalledWith("conv_delete_owner");
    resolvePtyCleanup(2);
    resolveBrowserCleanup(1);
    await Promise.resolve();
  });

  it("uses the long-operation deadline for authoritative conversation deletion", async () => {
    vi.useFakeTimers();
    try {
      registerWebSocketSender(() => true);
      let settled = false;
      const deletion = sendConversationDeleteCommand({
        type: "conversation.delete",
        conversation_id: "conv-long-delete",
      }).then((value) => {
        settled = true;
        return value;
      });

      await vi.advanceTimersByTimeAsync(60_000);
      expect(settled).toBe(false);

      await vi.advanceTimersByTimeAsync(LONG_COMMAND_RESULT_TIMEOUT_MS - 60_000);
      await expect(deletion).resolves.toBe(false);
      expect(pushToast).toHaveBeenCalledWith(
        expect.stringContaining("conversation.delete"),
        "error",
        6000,
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps deletion authoritative when desktop cleanup fails and reports a warning", async () => {
    vi.mocked(isDesktop).mockReturnValue(true);
    vi.mocked(ptyKillConversation).mockRejectedValue(new Error("terminal still running"));
    const sender = vi.fn((command) => {
      queueMicrotask(() => resolveClientCommandResult({
        type: "command.result",
        command: "conversation.delete",
        level: "success",
        message: "",
        data: { client_command_id: command.client_command_id },
      }));
      return true;
    });
    registerWebSocketSender(sender);

    await expect(sendConversationDeleteCommand({
      type: "conversation.delete",
      conversation_id: "conv_delete_owner",
    })).resolves.toBe(true);
    await Promise.resolve();

    expect(sender).toHaveBeenCalledOnce();
    expect(pushToast).toHaveBeenCalledWith(
      expect.stringContaining("会话删除已继续：terminal still running"),
      "warning",
      5000,
    );
  });

  it("bounds hanging desktop cleanup without delaying backend deletion", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(isDesktop).mockReturnValue(true);
      vi.mocked(ptyKillConversation).mockReturnValue(new Promise<number>(() => {}));
      vi.mocked(embeddedBrowserCloseConversation).mockReturnValue(new Promise<number>(() => {}));
      registerWebSocketSender((command) => {
        queueMicrotask(() => resolveClientCommandResult({
          type: "command.result",
          command: "conversation.delete",
          level: "success",
          message: "",
          data: { client_command_id: command.client_command_id },
        }));
        return true;
      });

      await expect(sendConversationDeleteCommand({
        type: "conversation.delete",
        conversation_id: "conv_hanging_cleanup",
      })).resolves.toBe(true);

      await vi.advanceTimersByTimeAsync(2_500);
      expect(pushToast).toHaveBeenCalledWith(
        expect.stringContaining("清理超过 2.5 秒"),
        "warning",
        5000,
      );
    } finally {
      vi.useRealTimers();
    }
  });
});
