import { describe, expect, it } from "vitest";
import {
  mergeTerminalOutputByCursor,
  mergeTerminalOutputSnapshot,
  terminalExitCodeLabel,
  terminalSessionLabel,
} from "./TerminalPanel";

describe("terminal scrollback hydration", () => {
  it("appends only live output beyond the snapshot cursor", () => {
    const snapshot = "0123456789";
    const live = "6789abc";

    expect(mergeTerminalOutputByCursor(snapshot, 0, 10, live, 13)).toEqual({
      output: "0123456789abc",
      endCursor: 13,
    });
  });

  it("uses the snapshot when it already covers the observed live output", () => {
    expect(mergeTerminalOutputByCursor("complete", 12, 20, "plete", 20)).toEqual({
      output: "complete",
      endCursor: 20,
    });
  });

  it("handles overlap larger than the legacy string fallback window when cursors are present", () => {
    const overlap = "x".repeat(5_000);
    const snapshot = `before-${overlap}`;
    const live = `${overlap}-after`;

    expect(mergeTerminalOutputByCursor(snapshot, 0, snapshot.length, live, snapshot.length + 6).output)
      .toBe(`${snapshot}-after`);
  });

  it("keeps legacy metadata-only runtimes compatible", () => {
    expect(mergeTerminalOutputSnapshot("hello ", "world")).toBe("hello world");
  });
});

describe("terminal session labels", () => {
  it("distinguishes parallel sessions without adding noise to a single terminal", () => {
    const session = {
      id: "term-1",
      conversationId: "conv-1",
      shell: "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
      cwd: "C:\\workspace",
      status: "running" as const,
      terminalMode: "pty" as const,
    };

    expect(terminalSessionLabel(session, 0, 1)).toBe("powershell");
    expect(terminalSessionLabel(session, 1, 2)).toBe("powershell 2");
    expect(terminalSessionLabel({ ...session, terminalMode: "pipe" }, 0, 2)).toBe("powershell 1（基础）");
  });

  it("does not misreport an unknown process status as a successful zero exit", () => {
    expect(terminalExitCodeLabel(0)).toBe("0");
    expect(terminalExitCodeLabel(-1)).toBe("-1");
    expect(terminalExitCodeLabel(null)).toBe("unknown");
    expect(terminalExitCodeLabel(undefined)).toBe("unknown");
    expect(terminalExitCodeLabel("0")).toBe("unknown");
  });
});
