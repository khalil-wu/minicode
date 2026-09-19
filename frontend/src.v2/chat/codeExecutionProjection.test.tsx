/* @vitest-environment jsdom */
import { afterEach, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { hydrateMessages } from "./transcriptHydration";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { HistoryCellRenderer } from "./components/ChatTurn";

afterEach(cleanup);
const source = { kind: "code_mode" as const, cell_id: "cell-123", parent_call_id: "script-parent", runtime_call_id: "1" };

it("retains nested-call provenance across persisted transcript hydration and command projection", () => {
  const messages = hydrateMessages([{ id: "answer", role: "assistant", content: "", timestamp: 1,
    blocks: [{ type: "tool_call", record: { id: "nested", name: "run_command", args: { command: "echo result" }, status: "success", startedAt: 1, callSource: source } }],
  }]);
  const turn = projectMessagesToTurns(messages, false)[0];
  expect(turn.committedCells[0]).toMatchObject({ kind: "exec", callSource: source });
  render(<HistoryCellRenderer cell={turn.committedCells[0]} />);
  expect(screen.getByText("工具组合").getAttribute("data-parent-call")).toBe("script-parent");
});

it("shows the actual JavaScript only when the composition row is expanded", () => {
  const code = 'const result = await tools.read_file({file_path:"a.py"});\ntext(result.content);';
  const messages = hydrateMessages([{ id: "answer", role: "assistant", content: "", timestamp: 1,
    blocks: [{ type: "tool_call", record: { id: "script-parent", name: "tool_exec", args: { code }, status: "success", displaySummary: "Script completed", summary: "Selected output", startedAt: 1 } }],
  }]);
  const turn = projectMessagesToTurns(messages, false)[0];
  render(<HistoryCellRenderer cell={turn.committedCells[0]} />);
  expect(screen.queryByLabelText("组合脚本")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "展开活动详情" }));
  expect(screen.getByLabelText("组合脚本").textContent).toBe(code);
});

it("restores extension command provenance without labelling it as a code cell", () => {
  const extensionSource = { kind: "extension", parent_call_id: "extension-wrapper", cell_id: "" };
  const messages = hydrateMessages([{ id: "answer", role: "assistant", content: "", timestamp: 1,
    blocks: [{ type: "tool_call", record: { id: "nested", name: "run_command", args: { command: "echo result" },
      status: "success", startedAt: 1, call_source: extensionSource } }],
  }]);
  const turn = projectMessagesToTurns(messages, false)[0];
  render(<HistoryCellRenderer cell={turn.committedCells[0]} />);
  expect(screen.getByText("扩展").getAttribute("data-parent-call")).toBe("extension-wrapper");
  expect(screen.queryByText("工具组合")).toBeNull();
});
