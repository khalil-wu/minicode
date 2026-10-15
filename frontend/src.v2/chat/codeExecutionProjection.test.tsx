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
  expect(screen.queryByText("工具组合")).toBeNull();
  expect(turn.committedCells[0]).toMatchObject({ callSource: { parent_call_id: "script-parent" } });
});

it("retains standalone model output without manufacturing a user transcript operation", () => {
  const code = 'const result = await tools.read_file({file_path:"a.py"});\ntext(result.content);';
  const messages = hydrateMessages([{ id: "answer", role: "assistant", content: "", timestamp: 1,
    blocks: [{ type: "tool_call", record: { id: "script-parent", name: "tool_exec", args: { code }, status: "success", displaySummary: "Script completed", summary: "Selected output", startedAt: 1 } }],
  }]);
  const turn = projectMessagesToTurns(messages, false)[0];
  expect(turn.committedCells).toEqual([]);
  expect(messages[0].blocks?.[0]).toMatchObject({ type: "tool_call", record: { summary: "Selected output", args: { code } } });
});

it("restores extension command provenance without labelling it as a code cell", () => {
  const extensionSource = { kind: "extension", parent_call_id: "extension-wrapper", cell_id: "" };
  const messages = hydrateMessages([{ id: "answer", role: "assistant", content: "", timestamp: 1,
    blocks: [{ type: "tool_call", record: { id: "nested", name: "run_command", args: { command: "echo result" },
      status: "success", startedAt: 1, call_source: extensionSource } }],
  }]);
  const turn = projectMessagesToTurns(messages, false)[0];
  render(<HistoryCellRenderer cell={turn.committedCells[0]} />);
  expect(screen.queryByText("扩展")).toBeNull();
  expect(turn.committedCells[0]).toMatchObject({ callSource: { parent_call_id: "extension-wrapper" } });
  expect(screen.queryByText("工具组合")).toBeNull();
});

it("shows real operations while retaining forwarded and computed model output only in the transcript data", () => {
  const messages = hydrateMessages([{ id: "answer", role: "assistant", content: "", timestamp: 1, blocks: [
    { type: "tool_call", record: { id: "nested", name: "read_file", args: { file_path: "a.py" }, status: "success",
      summary: "file bytes", startedAt: 1, callSource: source } },
    { type: "tool_call", record: { id: "script-parent", name: "tool_exec", args: { code: "not a visible action" }, status: "success", startedAt: 1,
      summary: JSON.stringify({cell_id:"cell-123",status:"completed",output:[JSON.stringify({content:"file bytes",is_error:false}),JSON.stringify({content:"file bytes",is_error:false,note:"Computed summary"})],output_tool_call_ids:["nested",""]}) } },
  ] }]);
  const turn = projectMessagesToTurns(messages, false)[0];
  expect(turn.committedCells.map(cell => cell.kind)).toEqual(["activity"]);
  expect(messages[0].blocks?.[1]).toMatchObject({ type: "tool_call", record: { summary: expect.stringContaining("Computed summary") } });
  render(<>{turn.committedCells.map(cell => <HistoryCellRenderer key={cell.id} cell={cell} />)}</>);
  expect(screen.queryByText("运行")).toBeNull();
  expect(screen.queryByText("输出")).toBeNull();
  expect(screen.queryByText(/Computed summary/)).toBeNull();
});

it("keeps a script's independent failure alongside successful leaf evidence", () => {
  const messages = hydrateMessages([{ id: "answer", role: "assistant", content: "", timestamp: 1, blocks: [
    { type: "tool_call", record: { id: "nested", name: "read_file", args: { file_path: "a.py" }, status: "success", summary: "file bytes", startedAt: 1, callSource: source } },
    { type: "tool_call", record: { id: "script-parent", name: "tool_exec", args: {}, status: "failed", startedAt: 1,
      summary: JSON.stringify({cell_id:"cell-123",status:"failed",output:[],error:"Computation failed"}) } },
  ] }]);
  const turn = projectMessagesToTurns(messages, false)[0];
  render(<>{turn.committedCells.map(cell => <HistoryCellRenderer key={cell.id} cell={cell} />)}</>);
  expect(screen.getByRole("alert").textContent).toContain("Computation failed");
  expect(screen.getByText("代码执行失败")).toBeTruthy();
});
