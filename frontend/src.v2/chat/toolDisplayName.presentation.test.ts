import { describe, expect, it } from "vitest";
import { readableToolLabel } from "./toolDisplayName";

describe("Codex-style presentation keeps execution evidence intact", () => {
  it("changes only the leading action, never a filename", () => {
    expect(readableToolLabel("read_file src/read_file.ts")).toBe("Read src/read_file.ts");
    expect(readableToolLabel("读取文件 C:\\work\\web_search.ts")).toBe("Read C:\\work\\web_search.ts");
  });
  it("keeps the complete command after its action", () => {
    const command = 'echo web_fetch mcp__github__search_users && node "read_file.ts"';
    expect(readableToolLabel(`run_command ${command}`)).toBe(`Run ${command}`);
  });
  it("uses a qualified MCP operation without changing the target", () => {
    expect(readableToolLabel("mcp__github__search_users web_search/read_file.ts"))
      .toBe("github.search_users web_search/read_file.ts");
  });

  it("keeps double separators inside an MCP tool's fallback display name", () => {
    expect(readableToolLabel("mcp__github__foo__bar src/read_file.ts")).toBe("github.foo__bar src/read_file.ts");
  });
  it("distinguishes waiting from starting a code execution", () => {
    expect(readableToolLabel("tool_exec")).toBe("Run code");
    expect(readableToolLabel("tool_wait")).toBe("Wait");
    expect(readableToolLabel("tool_wait", true)).toBe("Waiting");
  });
  it("retains an unknown provider's supplied title", () => {
    expect(readableToolLabel("custom_indexer workspace/read_file.ts"))
      .toBe("custom_indexer workspace/read_file.ts");
  });
});
