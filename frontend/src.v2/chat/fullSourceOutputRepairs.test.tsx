/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { projectChatTurnToAgentLoop } from "../agent-loop/projection/project-turn";
import { ActivityCell } from "./cells/ActivityCell";
import { ExecCell } from "./cells/ExecCell";
import type { ChatMessage, ContentBlock } from "../stores/types";
import { hydrateMessages } from "./transcriptHydration";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const assistant = (blocks: ContentBlock[]): ChatMessage => ({
  id: "audit-answer", role: "assistant", content: "", timestamp: 1, artifacts: [], blocks,
});

describe("tool and answer evidence reaches the actual rendered cell", () => {
  it.each(["", "output_bytes: actual program text\nnext_cursor: actual program text\nmore_output: actual program text"])("uses persisted typed streams even when stdout is %j", stdout => {
    const messages = hydrateMessages([{ id: "persisted", role: "assistant", timestamp: 1, blocks: [{ type: "tool_call", record: {
      id: "typed-command", name: "run_command", args: { command: "print" }, status: "success", activityKind: "commandExecution",
      outputPreview: "output_bytes: 0\nnext_cursor: 0\nmore_output: false\n\n--- stdout ---\ntransport snapshot",
      stdoutPreview: stdout, stderrPreview: "", exitCode: 0, startedAt: 1,
    } }] }]);
    const cell = projectMessagesToTurns(messages, false)[0].committedCells[0];
    expect(cell.kind).toBe("exec"); if (cell.kind !== "exec") throw new Error("Missing persisted command");
    expect(cell.stdoutFull).toBe(stdout);
    const view = render(<ExecCell cell={cell} />);
    fireEvent.click(screen.getByRole("button", { name: /^展开命令详情/ }));
    expect(view.container.textContent).not.toContain("transport snapshot");
    if (stdout) expect(view.container.querySelector(".exec-cell-output-stdout")?.textContent).toBe(stdout);
    else expect(screen.getByText("无输出")).toBeTruthy();
  });
  it("keeps successful stdout bytes in expansion and clipboard", async () => {
    const stdout = "<error>审计原文</error>\n<untrusted_text>原样</untrusted_text>";
    const turn = projectMessagesToTurns([assistant([{ type: "tool_call", record: { id: "literal", name: "run_command", args: { command: "print" },
      status: "success", activityKind: "commandExecution", stdoutPreview: stdout, stderrPreview: "", startedAt: 1, finishedAt: 2 } }])], false, "")[0];
    const cell = turn.committedCells.find(cell => cell.kind === "exec");
    expect(cell?.kind).toBe("exec"); if (!cell || cell.kind !== "exec") throw new Error("Missing actual command cell");
    expect(cell.stdoutFull).toBe(stdout);
    const copied = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: copied } });
    const view = render(<ExecCell cell={cell} />);
    fireEvent.click(screen.getByRole("button", { name: /^展开命令详情/ }));
    expect(view.container.textContent).toContain("<error>审计原文</error>");
    fireEvent.click(screen.getByRole("button", { name: "复制命令输出" }));
    await waitFor(() => expect(copied).toHaveBeenCalledWith(stdout));
    fireEvent.click(screen.getByRole("button", { name: "复制命令" }));
    await waitFor(() => expect(copied).toHaveBeenLastCalledWith("print"));
  });
  it("keeps model-only CodeVM computation out of the user trace while preserving the real read", async () => {
    const payload = JSON.stringify({ cell_id: "cell", status: "completed", output: ["before read", "computed 42"],
      completed_tools: [{ name: "read_file", content: "leaf receipt" }] });
    const turn = projectMessagesToTurns([assistant([
      { type: "tool_call", record: { id: "exec", name: "tool_exec", args: { code: "text(...); await tools.read_file(...)" }, status: "success",
        activityKind: "commandExecution", outputPreview: payload, startedAt: 1 } },
      { type: "tool_call", record: { id: "leaf", name: "read_file", args: { path: "source.py" }, status: "success", outputPreview: "leaf receipt",
        callSource: { kind: "code_mode", parent_call_id: "exec", cell_id: "cell", runtime_call_id: "1" }, startedAt: 2 } },
    ])], false, "")[0];
    expect(turn.committedCells.map(cell => cell.id)).toEqual(["leaf"]);
    const cell = turn.committedCells[0];
    expect(cell.kind).toBe("activity"); if (cell.kind !== "activity") throw new Error("Missing real read");
    render(<ActivityCell cell={cell} />);
    expect(screen.queryByText(/before read/)).toBeNull();
    expect(screen.queryByText(/computed 42/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "展开活动详情" }));
    expect(screen.getByText("leaf receipt")).toBeTruthy();
    expect(screen.queryByText(/text\(\.\.\.\)/)).toBeNull();
    expect(turn.committedCells.some(cell => cell.id === "leaf")).toBe(true);
  });
  it("distinguishes retained candidates from accepted final answers while a turn is live", () => {
    const retained = { ...assistant([{ type: "text" as const, itemId: "candidate", content: "old candidate", source: "retained" as const,
      status: "completed" as const, isStreaming: false }]), isStreaming: true };
    const projection = projectChatTurnToAgentLoop(projectMessagesToTurns([retained], true, "")[0]);
    expect(projection.hasCompleteFinalAnswer).toBe(false);
    expect(projection.status).toBe("running");
    expect(projection.processCells.some(cell => cell.kind === "thinking" && cell.content.includes("old candidate"))).toBe(true);
    const accepted = { ...retained, blocks: [{ type: "text" as const, itemId: "accepted", content: "final", source: "model_final" as const,
      status: "completed" as const, isStreaming: false }] };
    expect(projectChatTurnToAgentLoop(projectMessagesToTurns([accepted], true, "")[0]).hasCompleteFinalAnswer).toBe(true);
  });
});
