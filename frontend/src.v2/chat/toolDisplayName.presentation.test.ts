import { describe, expect, it } from "vitest";
import { readableToolLabel } from "./toolDisplayName";

describe("Codex-style presentation keeps execution evidence intact", () => {
  it("changes only the leading action, never a filename", () => {
    expect(readableToolLabel("read_file src/read_file.ts")).toBe("读取 src/read_file.ts");
    expect(readableToolLabel("读取文件 C:\\work\\web_search.ts")).toBe("读取 C:\\work\\web_search.ts");
  });
  it("keeps the complete command after its action", () => {
    const command = 'echo web_fetch mcp__github__search_users && node "read_file.ts"';
    expect(readableToolLabel(`run_command ${command}`)).toBe(`运行 ${command}`);
    expect(readableToolLabel("web_fetch https://example.com/Read/Search?tool=ask_user"))
      .toBe("读取网页 https://example.com/Read/Search?tool=ask_user");
  });
  it("uses a qualified MCP operation without changing the target", () => {
    expect(readableToolLabel("mcp__github__search_users web_search/read_file.ts"))
      .toBe("github.search_users web_search/read_file.ts");
  });

  it("keeps double separators inside an MCP tool's fallback display name", () => {
    expect(readableToolLabel("mcp__github__foo__bar src/read_file.ts")).toBe("github.foo__bar src/read_file.ts");
  });
  it("keeps internal execution wrappers on evidence labels instead of exposing runtime phases", () => {
    expect(readableToolLabel("tool_exec")).toBe("操作结果");
    expect(readableToolLabel("tool_wait")).toBe("操作结果");
    expect(readableToolLabel("tool_wait", true)).toBe("操作结果");
    expect(readableToolLabel("正在执行代码")).toBe("操作结果");
  });
  it("retains an unknown provider's supplied title", () => {
    expect(readableToolLabel("custom_indexer workspace/read_file.ts"))
      .toBe("custom_indexer workspace/read_file.ts");
  });
});
