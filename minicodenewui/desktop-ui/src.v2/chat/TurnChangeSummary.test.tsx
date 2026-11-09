/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { TurnChangeSummary } from "./TurnChangeSummary";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
});

const diff = "diff --git a/src/a.ts b/src/a.ts\n--- a/src/a.ts\n+++ b/src/a.ts\n@@ -1 +1 @@\n-old\n+new";

beforeEach(() => useAppStore.setState({
  conversationId: "owner",
  messages: [{ id: "answer", role: "assistant", content: "Done", turnId: "turn-one", artifacts: [], timestamp: 1 }],
  turnDiffs: { owner: { threadId: "owner", turnId: "turn-one", messageId: "answer", diff, updatedAt: 1 } },
  diffReview: null,
  rightPanelOpen: false,
}));
afterEach(cleanup);

describe("TurnChangeSummary", () => {
  it("opens exactly the displayed turn changes in view-only review", () => {
    render(<TurnChangeSummary />);
    expect(screen.getByText("1 个文件已更改")).toBeTruthy();
    expect(screen.getByText("+1")).toBeTruthy();
    expect(screen.getByText("-1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "审阅本轮文件更改" }));
    expect(useAppStore.getState().diffReview).toMatchObject({ conversationId: "owner", turnId: "turn-one", diff, mode: "view", status: "viewing", selectedPath: "src/a.ts" });
    expect(useAppStore.getState().rightPanelOpen).toBe(true);
    expect(useAppStore.getState().rightStackTab).toBe("diff");
  });

  it("removes a retracted diff instead of keeping stale totals", () => {
    const { container } = render(<TurnChangeSummary />);
    act(() => useAppStore.setState({ turnDiffs: { owner: { ...useAppStore.getState().turnDiffs.owner, diff: "" } } }));
    expect(container.firstChild).toBeNull();
  });

  it("never shows another conversation or unmatched turn", () => {
    const { container } = render(<TurnChangeSummary />);
    act(() => useAppStore.setState({ conversationId: "other" }));
    expect(container.firstChild).toBeNull();
    act(() => useAppStore.setState({ conversationId: "owner", messages: [] }));
    expect(container.firstChild).toBeNull();
  });
});
