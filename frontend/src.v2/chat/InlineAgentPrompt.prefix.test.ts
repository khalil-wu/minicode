import { describe, expect, it } from "vitest";
import { deriveCommandPrefix } from "./commandPrefix";

describe("deriveCommandPrefix", () => {
  it("uses a two-token prefix for package managers / git", () => {
    expect(deriveCommandPrefix("npm run build")).toBe("npm run");
    expect(deriveCommandPrefix("git status")).toBe("git status");
    expect(deriveCommandPrefix("cargo test --release")).toBe("cargo test");
    expect(deriveCommandPrefix("pnpm lint")).toBe("pnpm lint");
  });

  it("falls back to the binary for unknown tools", () => {
    expect(deriveCommandPrefix("echo hello")).toBe("echo");
    expect(deriveCommandPrefix("./bin/serve --port 3000")).toBe("./bin/serve");
  });

  it("strips a leading $ prompt and trims whitespace", () => {
    expect(deriveCommandPrefix("$ npm install")).toBe("npm install");
    expect(deriveCommandPrefix("   git   push  ")).toBe("git push");
  });

  it("returns empty for blank input", () => {
    expect(deriveCommandPrefix("")).toBe("");
    expect(deriveCommandPrefix("   ")).toBe("");
  });

  it("does not create permanent rules for wrappers or compound commands", () => {
    expect(deriveCommandPrefix("bash -c 'echo ok'")).toBe("");
    expect(deriveCommandPrefix("C:\\Windows\\System32\\cmd.exe /c echo ok")).toBe("");
    expect(deriveCommandPrefix("git status && rm -rf build")).toBe("");
    expect(deriveCommandPrefix('git status --pathspec-from-file="a&&b"')).toBe("git status");
  });
});
