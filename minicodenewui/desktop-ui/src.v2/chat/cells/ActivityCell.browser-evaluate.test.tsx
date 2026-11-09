// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "../../stores";
import { ActivityCell } from "./ActivityCell";
import type { ActivityCellState } from "./cellTypes";

const expression = "JSON.stringify({viewport:[innerWidth,innerHeight],buttons:[...document.querySelectorAll('button')].map(button=>button.getBoundingClientRect())})";
const output = JSON.stringify({ viewport: [1024, 768], buttons: Array.from({ length: 40 }, (_, index) => ({ x: index * 30, y: 700, width: 26, height: 26 })) });
const cell: ActivityCellState = {
  kind: "activity", id: "browser-evaluate-detail", activityKind: "browser", title: "Evaluate JavaScript",
  status: "done", collapsed: false, startedAt: 1000, completedAt: 3100,
  toolCallRecords: [{
    id: "evaluate-call", name: "browser_control", args: { action: "evaluate", expression },
    status: "success", outputPreview: output, startedAt: 1000, finishedAt: 3100,
  }],
};

beforeEach(() => useAppStore.setState({ conversationId: "evaluate-owner", workingDirectory: "C:/projects/demo" }));
afterEach(cleanup);

describe("browser evaluate disclosure", () => {
  it("keeps the script and its complete result in one labelled tool detail", () => {
    const view = render(<ActivityCell cell={cell} conversationId="evaluate-owner" />);
    const details = view.container.querySelectorAll(".activity-cell-browser-detail-card");
    expect(details).toHaveLength(1);
    expect(view.container.querySelectorAll(".activity-cell-expanded")).toHaveLength(1);
    expect([...details[0].querySelectorAll(".activity-cell-browser-detail-label")].map(label => label.textContent))
      .toEqual(["JavaScript", "操作结果"]);
    expect(view.getByLabelText("JavaScript").textContent).toBe(expression);
    expect(view.getByLabelText("操作结果").textContent).toBe(output);
    expect(view.getByLabelText("JavaScript").tagName).toBe("PRE");
    expect(view.getByLabelText("操作结果").tagName).toBe("PRE");
    expect(view.container.querySelector(".activity-cell-inline-output, .activity-cell-output-pre")).toBeNull();
    expect(view.container.querySelector(".activity-cell-detail-duration")?.textContent).toBe("2s");
  });

  it("keeps the original script and precise evaluation error in that same detail", () => {
    const error = "TypeError: Cannot read properties of null (reading 'getBoundingClientRect')";
    const failed: ActivityCellState = {
      ...cell, id: "browser-evaluate-error", status: "failed",
      toolCallRecords: [{ ...cell.toolCallRecords![0], status: "failed", outputPreview: "工具执行失败。", developerDetail: error }],
    };
    const view = render(<ActivityCell cell={failed} conversationId="evaluate-owner" />);
    expect(view.container.querySelectorAll(".activity-cell-browser-detail-card")).toHaveLength(1);
    expect(view.getByLabelText("JavaScript").textContent).toBe(expression);
    expect(view.getByLabelText("错误详情").textContent).toBe(error);
    expect(view.getByLabelText("错误详情").tagName).toBe("PRE");
    expect(view.container.querySelector('[data-error="true"] .activity-cell-browser-detail-label')?.textContent).toBe("错误详情");
    expect(view.container.querySelector(".activity-cell-failed")).toBeTruthy();
  });
});
