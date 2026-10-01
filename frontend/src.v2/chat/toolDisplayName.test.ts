import { describe, expect, it } from "vitest";
import { readableToolLabel } from "./toolDisplayName";

describe("readableToolLabel", () => {
  it("localizes exact runtime labels without rewriting file names", () => {
    expect(readableToolLabel("Read")).toBe("读取文件");
    expect(readableToolLabel("Script failed")).toBe("脚本执行失败");
    expect(readableToolLabel("src/Read.ts")).toBe("src/Read.ts");
  });
  it("never exposes concatenated provider web protocol identifiers", () => {
    const label = readableToolLabel("webfetchweb_fetch, web_fetch web_search");

    expect(label).not.toMatch(/web_?fetch|web_?search/i);
    expect(label).toContain("获取网页");
    expect(label).toContain("搜索网页");
  });

  it("renders MCP identifiers as a service and operation label", () => {
    expect(readableToolLabel("mcp__github__search_users")).toBe("github.search_users");
  });

  it("renders built-in tool identifiers as concise Chinese actions", () => {
    expect(readableToolLabel("run_command")).toBe("运行命令");
    expect(readableToolLabel("write_file")).toBe("写入文件");
    expect(readableToolLabel("read_file")).toBe("读取文件");
    expect(readableToolLabel("edit_file")).toBe("编辑文件");
    expect(readableToolLabel("update_plan")).toBe("更新计划");
  });

  it("leaves unknown tool identifiers untouched", () => {
    expect(readableToolLabel("todo_write")).toBe("todo_write");
  });
});
