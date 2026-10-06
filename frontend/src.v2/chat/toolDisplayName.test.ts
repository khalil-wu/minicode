import { describe, expect, it } from "vitest";
import { readableToolLabel } from "./toolDisplayName";

describe("readableToolLabel", () => {
  it("localizes exact runtime labels without rewriting file names", () => {
    expect(readableToolLabel("Read")).toBe("读取");
    expect(readableToolLabel("Script failed")).toBe("错误详情");
    expect(readableToolLabel("src/Read.ts")).toBe("src/Read.ts");
  });
  it("never exposes concatenated provider web protocol identifiers", () => {
    const label = readableToolLabel("webfetchweb_fetch, web_fetch web_search");

    expect(label).not.toMatch(/web_?fetch|web_?search/i);
    expect(label).toContain("读取网页");
    expect(label).toContain("搜索");
  });

  it("renders MCP identifiers as a service and operation label", () => {
    expect(readableToolLabel("mcp__github__search_users")).toBe("github.search_users");
  });

  it("renders built-in tool identifiers as concise Chinese actions", () => {
    expect(readableToolLabel("run_command")).toBe("运行");
    expect(readableToolLabel("write_file")).toBe("编辑");
    expect(readableToolLabel("read_file")).toBe("读取");
    expect(readableToolLabel("edit_file")).toBe("编辑");
    expect(readableToolLabel("update_plan")).toBe("更新计划");
  });

  it.each([
    ["ask_user", "向你提问"], ["Ask user", "向你提问"],
    ["task_status", "查看子智能体"], ["Check agents", "查看子智能体"],
    ["Run command", "运行"],
  ])("localizes actual persisted chrome %s", (value, label) => {
    expect(readableToolLabel(value)).toBe(label);
  });

  it("keeps the question lifecycle and failure state explicit", () => {
    expect(readableToolLabel("Ask user", true)).toBe("等待你回复");
    expect(readableToolLabel("Failed: read_file src/Failed.ts")).toBe("失败：读取 src/Failed.ts");
  });

  it("leaves unknown tool identifiers untouched", () => {
    expect(readableToolLabel("todo_write")).toBe("todo_write");
  });
});
