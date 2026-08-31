import { describe, expect, it } from "vitest";
import { workspaceRelativeDiffPath } from "./diffPaths";

describe("workspaceRelativeDiffPath", () => {
  it("strips the active workspace folder prefix from relative diff paths", () => {
    expect(workspaceRelativeDiffPath("temp/greeting.py", "C:\\Desktop\\temp")).toBe("greeting.py");
  });

  it("keeps nested paths when the prefix is not the active workspace folder", () => {
    expect(workspaceRelativeDiffPath("temp/greeting.py", "C:\\Desktop")).toBe("temp/greeting.py");
  });

  it("converts absolute paths inside the workspace to workspace-relative paths", () => {
    expect(workspaceRelativeDiffPath("C:\\Desktop\\temp\\weather.json", "C:\\Desktop\\temp")).toBe("weather.json");
  });

  it("does not strip a differently-cased POSIX workspace prefix", () => {
    expect(workspaceRelativeDiffPath("/tmp/project/weather.json", "/tmp/Project")).toBe("/tmp/project/weather.json");
  });

  it("keeps Windows drive paths case-insensitive", () => {
    expect(workspaceRelativeDiffPath("c:\\desktop\\temp\\weather.json", "C:\\Desktop\\Temp")).toBe("weather.json");
  });
});
