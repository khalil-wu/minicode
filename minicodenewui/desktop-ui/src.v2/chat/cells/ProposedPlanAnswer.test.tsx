/* @vitest-environment jsdom */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProposedPlanAnswer } from "./ProposedPlanAnswer";
import { proposedPlanParts } from "../../lib/proposed-plan";

vi.mock("../messages/MarkdownRenderer", () => ({ MarkdownRenderer: ({ content }: { content: string }) => <div>{content}</div> }));
const content = "先核对目标。\n<proposed_plan>\n## 修改计划\n\n1. 修正调用链。\n2. 验证真实任务。\n</proposed_plan>\n确认后可以实施。";
afterEach(cleanup);

describe("source proposed-plan answer projection", () => {
  it("renders the complete plan and surrounding text, with implementation owned by the bottom request surface", () => {
    render(<ProposedPlanAnswer content={content} conversationId="plan-owner" workspaceRoot="C:/project" />);
    expect(screen.queryByText(/<proposed_plan>/)).toBeNull();
    expect(screen.getByText("先核对目标。")).toBeTruthy();
    expect(screen.getByText("确认后可以实施。")).toBeTruthy();
    expect(screen.getByText(/修正调用链/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "执行计划" })).toBeNull();
  });

  it("retains the actual original output for streaming or interrupted plans", () => {
    const partial = "<proposed_plan>\n1. 未完成步骤";
    const view = render(<ProposedPlanAnswer content={partial} isStreaming conversationId="plan-owner" />);
    expect(screen.getByText("正在制定方案")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "执行计划" })).toBeNull();
    view.rerender(<ProposedPlanAnswer content={partial} failed conversationId="plan-owner" />);
    expect(screen.getByText("方案未完整生成")).toBeTruthy();
    expect(view.container.querySelector("pre")?.textContent).toBe(partial);
  });

  it("does not treat fenced examples or unrelated XML as a proposed plan", () => {
    expect(proposedPlanParts("```xml\n<proposed_plan>\nexample\n</proposed_plan>\n```")).toBeNull();
    expect(proposedPlanParts("<other>\nordinary assistant text\n</other>")).toBeNull();
  });
});
