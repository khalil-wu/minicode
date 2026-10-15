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

    expect(screen.getByText("已在1s内运行")).toBeTruthy();
    expect(container.querySelector(".exec-cell-header-button")?.getAttribute("data-processing")).toBe("false");
    expect(screen.getByText(command)).toBeTruthy();
    expect(container.querySelector(".exec-cell-output-status")).toBeNull();
    expect(container.querySelector(".exec-cell-header-button")).toBeTruthy();
    expect(container.querySelector(".exec-cell-output-stack")).toBeNull();
    expect(container.querySelector(".exec-cell-collapsed-output")).toBeNull();

    const disclosure = screen.getByRole("button", { name: /展开命令详情/ });
    expect(disclosure.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(disclosure);

    expect(disclosure.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("region", { name: "命令输出" })).toBeTruthy();
    expect(container.querySelector(".exec-cell-expanded")?.previousElementSibling)
      .toBe(container.querySelector(".exec-cell-header-row"));
    // Output has its own copy action without another nested card.
    expect(container.querySelector(".exec-cell-shell-card")).toBeNull();
    expect(container.querySelector(".exec-cell-expanded-heading")).toBeNull();
    expect(screen.getByText("Shell")).toBeTruthy();
    expect(screen.getByRole("button", { name: "复制命令输出" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "复制命令" })).toBeTruthy();
    expect(screen.getByText("命令已在1s内运行完成")).toBeTruthy();
    expect(screen.queryByText(command)).toBeNull();
    expect(screen.getByText("成功")).toBeTruthy();
    expect(screen.getByText("$ " + command)).toBeTruthy();
    expect(screen.getByText("7 passed")).toBeTruthy();
    expect(container.querySelector(".exec-cell-output-toolbar")).toBeNull();
  });

  it("keeps a running command compact with interruption owned by the composer", () => {
    const { container } = render(
      <ExecCell
        isActive
        cell={{
          kind: "exec",
          id: "exec-running",
          command: "npm test -- --runInBand",
          status: "running",
          stdoutPreview: ["running"],
          stderrPreview: [],
          collapsed: true,
          createdAt: Date.now(),
        }}
      />,
    );

    expect(screen.getByText("正在运行")).toBeTruthy();
    expect(container.querySelector(".exec-cell-header-button")?.getAttribute("data-processing")).toBe("true");
    expect(screen.getByText("npm test -- --runInBand")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "停止命令" })).toBeNull();
    expect(container.querySelector(".exec-cell-expanded")).toBeNull();
    expect(screen.queryByText("running")).toBeNull();
  });

  it("does not open a collapsed output just because the command finishes", () => {
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

    expect(container.querySelector(".exec-cell-expanded")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /展开命令详情/ }));
    expect(screen.getByText("1:foo")).toBeTruthy();
  });

  it("preserves background startup and keeps partial results inside disclosure", () => {
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

    expect(screen.getAllByText("已在后台启动")).toHaveLength(2);
    expect(screen.getAllByText("已在后台启动").length).toBeGreaterThan(0);
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
    expect(screen.getByText("运行命令")).toBeTruthy();
    expect(screen.queryByText("退出码 未知")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /展开命令详情/ }));
    expect(screen.getByText("退出码 未知")).toBeTruthy();
    expect(screen.getByText("half of the suite ran")).toBeTruthy();
  });

  it("copies actual command and output independently without protocol decoration", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<ExecCell cell={{ kind: "exec", id: "copy", command: "npm test", status: "success", stdoutPreview: ["passed"], stderrPreview: ["warning"], collapsed: false, createdAt: 1 }} />);
    fireEvent.click(screen.getByRole("button", { name: "复制命令输出" }));
    expect(writeText).toHaveBeenCalledWith("passed\nwarning");
    expect(await screen.findByRole("button", { name: "已复制命令输出" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "复制命令" }));
    expect(writeText).toHaveBeenLastCalledWith("npm test");
    expect(await screen.findByRole("button", { name: "已复制命令" })).toBeTruthy();
  });

  it("expands the full original command while retaining literal output and a neutral exit footer", () => {
    const command = "python -c \"print('line 1'); print('line 2'); print('line 3')\"";
    const { container } = render(<ExecCell cell={{ kind: "exec", id: "original-failure", command: "python …", originalCommand: command,
      status: "failed", exitCode: 7, stdoutPreview: ["literal stdout"], stderrPreview: ["actual error"], collapsed: false, createdAt: 1 }} />);
    const fullCommand = screen.getByRole("button", { name: `展开完整命令：${command}` });
    expect(fullCommand.getAttribute("data-expanded")).toBe("false");
    fireEvent.click(fullCommand);
    expect(fullCommand.getAttribute("data-expanded")).toBe("true");
    expect(fullCommand.textContent).toBe(`$ ${command}`);
    expect(container.querySelector(".exec-cell-output-stdout")?.textContent).toBe("literal stdout");
    expect(container.querySelector(".exec-cell-output-stderr")?.textContent).toBe("actual error");
    expect(screen.getByText("退出码 7")).toBeTruthy();
    expect(screen.queryByText("失败")).toBeNull();
    expect(screen.queryByText("成功")).toBeNull();
  });

  it("does not add a running badge or placeholder to an empty live shell", () => {
    const { container } = render(<ExecCell cell={{ kind: "exec", id: "empty-live", command: "npm test", status: "running",
      stdoutPreview: [], stderrPreview: [], collapsed: false, createdAt: Date.now() }} />);
    expect(container.querySelector(".exec-cell-output-pre")?.textContent).toBe("");
    expect(container.querySelector(".exec-cell-output-status")).toBeNull();
    expect(screen.queryByText("运行中")).toBeNull();
    expect(screen.queryByText("等待输出…")).toBeNull();
    expect(screen.queryByText("无输出")).toBeNull();
  });

  it("shows a real stopped receipt inside disclosure", () => {
    render(<ExecCell cell={{ kind: "exec", id: "stopped", command: "npm test", status: "cancelled",
      stdoutPreview: ["partial stdout"], stderrPreview: [], collapsed: false, createdAt: 1 }} />);
    expect(screen.getByText("已停止")).toBeTruthy();
    expect(screen.getByText("partial stdout")).toBeTruthy();
    expect(screen.queryByText("失败")).toBeNull();
    expect(screen.queryByText("成功")).toBeNull();
  });
});


it.each([
  ["pending_approval", "等待批准运行命令"],
  ["failed", "运行命令"],
] as const)("does not claim a %s command already executed", (status, title) => {
  render(<ExecCell cell={{ kind: "exec", id: "exec-not-performed", command: "python -m pytest", status,
    stdoutPreview: [], stderrPreview: [], collapsed: true, createdAt: 1 }} />);
  expect(screen.getByText(title)).toBeTruthy();
  expect(screen.queryByText("已在 1.3s 内运行")).toBeNull();
  expect(screen.queryByText("正在运行")).toBeNull();
});
