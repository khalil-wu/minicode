/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ExecCell } from "./ExecCell";

afterEach(cleanup);

describe("ExecCell", () => {
  it("renders a compact command row and opens one flat output panel on demand", () => {
    const command = "Get-ChildItem frontend/src.v2/chat/cells -Filter *.test.tsx | Select-String Activity";
    const { container } = render(
      <ExecCell
        cell={{
          kind: "exec",
          id: "exec-flat",
          command,
          status: "success",
          exitCode: 0,
          stdoutPreview: ["7 passed"],
          stderrPreview: [],
          stdoutFull: "7 passed",
          collapsed: true,
          createdAt: 1,
          durationMs: 1300,
        }}
      />,
    );

    expect(screen.getByText("已运行命令")).toBeTruthy();
    expect(screen.getByText(command)).toBeTruthy();
    expect(screen.getByText("exit 0 · 1.3s")).toBeTruthy();
    expect(container.querySelector(".exec-cell-header-button")).toBeTruthy();
    expect(container.querySelector(".exec-cell-output-stack")).toBeNull();
    expect(container.querySelector(".exec-cell-collapsed-output")).toBeNull();

    const disclosure = screen.getByRole("button", { name: "展开命令详情" });
    expect(disclosure.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(disclosure);

    expect(disclosure.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("region", { name: "命令输出" })).toBeTruthy();
    expect(container.querySelector(".exec-cell-expanded")?.previousElementSibling)
      .toBe(container.querySelector(".exec-cell-header-row"));
    // One frame only: the header row already carries the outcome and duration,
    // so the panel must not repeat them behind another nested card.
    expect(container.querySelector(".exec-cell-shell-card")).toBeNull();
    expect(container.querySelector(".exec-cell-expanded-heading")).toBeNull();
    expect(screen.queryByText("Shell")).toBeNull();
    expect(screen.queryByText("命令已在 1.3s 内运行完成")).toBeNull();
    expect(screen.getByText("$ " + command)).toBeTruthy();
    expect(screen.getByText("7 passed")).toBeTruthy();
  });

  it("keeps a running command stoppable without opening a result card", () => {
    const stop = vi.fn();
    const { container } = render(
      <ExecCell
        isActive
        onStop={stop}
        cell={{
          kind: "exec",
          id: "exec-running",
          command: "npm test -- --runInBand",
          status: "running",
          stdoutPreview: ["running"],
          stderrPreview: [],
          collapsed: true,
          createdAt: 1,
        }}
      />,
    );

    expect(screen.getByText("正在运行")).toBeTruthy();
    expect(screen.getByText("npm test -- --runInBand")).toBeTruthy();
    expect(screen.getByRole("button", { name: "停止命令" })).toBeTruthy();
    expect(container.querySelector(".exec-cell-expanded")).toBeNull();
    expect(screen.queryByText("running")).toBeNull();
  });

  it("opens the command output when the active command finishes", () => {
    const command = "grep -n foo 1.txt";
    const { container, rerender } = render(
      <ExecCell
        isActive
        cell={{
          kind: "exec",
          id: "exec-settles",
          command,
          status: "running",
          stdoutPreview: [],
          stderrPreview: [],
          collapsed: true,
          createdAt: 1,
        }}
      />,
    );

    expect(container.querySelector(".exec-cell-expanded")).toBeNull();
    rerender(
      <ExecCell
        cell={{
          kind: "exec",
          id: "exec-settles",
          command,
          status: "success",
          exitCode: 0,
          stdoutPreview: ["1:foo"],
          stderrPreview: [],
          stdoutFull: "1:foo",
          collapsed: true,
          createdAt: 1,
          durationMs: 42,
        }}
      />,
    );

    expect(container.querySelector(".exec-cell-expanded")).toBeTruthy();
    expect(screen.getByText("1:foo")).toBeTruthy();
  });

  it("preserves background and partial lifecycle labels in the flat row", () => {
    const { rerender } = render(
      <ExecCell
        cell={{
          kind: "exec",
          id: "exec-background",
          command: "npm run dev",
          background: true,
          status: "success",
          stdoutPreview: [],
          stderrPreview: [],
          durationMs: 33,
          collapsed: false,
          createdAt: 1,
        }}
      />,
    );

    expect(screen.getByText("已启动后台命令")).toBeTruthy();
    expect(screen.getByText("后台运行")).toBeTruthy();
    expect(screen.queryByText("后台命令已启动；状态和输出会保留在活动任务中。")).toBeNull();

    rerender(
      <ExecCell
        cell={{
          kind: "exec",
          id: "exec-partial",
          command: "npm test",
          status: "partial",
          stdoutPreview: ["half of the suite ran"],
          stderrPreview: [],
          collapsed: true,
          createdAt: 1,
        }}
      />,
    );
    expect(screen.getByText("命令未完整结束")).toBeTruthy();
    expect(screen.getByText("未完整结束")).toBeTruthy();
  });
});
