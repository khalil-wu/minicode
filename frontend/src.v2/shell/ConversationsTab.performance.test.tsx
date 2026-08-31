/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { sessionRowRenderMock } = vi.hoisted(() => ({
  sessionRowRenderMock: vi.fn(),
}));

vi.mock("./SessionRow", async () => {
  const React = await import("react");
  const MockSessionRow = React.memo((props: Record<string, any>) => {
    sessionRowRenderMock(props.conversation.id);
    return React.createElement(
      "div",
      { "data-testid": `mock-session-row-${props.conversation.id}` },
      React.createElement(
        "button",
        {
          type: "button",
          "aria-label": `打开菜单 ${props.conversation.title}`,
          onClick: () => props.onSetMenuFor(props.menuOpen ? null : props.conversation.id),
        },
        props.conversation.title,
      ),
      props.selectionMode
        ? React.createElement("input", {
            type: "checkbox",
            "aria-label": `Select ${props.conversation.title}`,
            checked: props.selected,
            onChange: () => props.onToggleSelected(props.conversation.id),
          })
        : null,
    );
  });
  return { SessionRow: MockSessionRow };
});

vi.mock("../desktop/runtime", () => ({
  isDesktop: () => false,
  revealPath: vi.fn(),
}));

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: vi.fn(),
}));

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(() => true),
  sendClientCommandAwaitResult: vi.fn(async (command: { type: string }) => ({
    type: "command_result",
    command: command.type,
    level: "success",
    message: "",
    data: {},
  })),
  sendConversationDeleteCommand: vi.fn(async () => true),
  commandResultSucceeded: () => true,
}));

import { useAppStore } from "../stores";
import { ConversationsTab } from "./ConversationsTab";

function ParentWithUnrelatedUpdates() {
  const [, setTick] = useState(0);
  const [activeConversationId, setActiveConversationId] = useState("conv-a");
  return (
    <>
      <button type="button" onClick={() => setTick((value) => value + 1)}>父级无关更新</button>
      <button type="button" onClick={() => setActiveConversationId("conv-b")}>切换活动会话</button>
      <ConversationsTab
        conversationId={activeConversationId}
        onSetConfirmDialog={(dialog) => void dialog}
      />
    </>
  );
}

describe("ConversationsTab row rendering performance", () => {
  beforeEach(() => {
    sessionRowRenderMock.mockClear();
    useAppStore.setState({
      conversationId: "conv-a",
      conversations: [
        { id: "conv-a", title: "Task A", updatedAt: "2026-08-15T00:00:00.000Z" },
        { id: "conv-b", title: "Task B", updatedAt: "2026-08-15T00:00:01.000Z" },
        { id: "conv-c", title: "Task C", updatedAt: "2026-08-15T00:00:02.000Z" },
      ],
      conversationMessages: {},
      conversationStreaming: {},
      conversationHydration: {},
      recentWorkspaces: [],
      isConnected: false,
      isStreaming: false,
      pendingApproval: null,
      approvalQueue: [],
      pendingDiffReview: null,
      diffReviewQueue: [],
      pendingAskUser: null,
      askUserQueue: [],
      runtimeSession: null,
      workingDirectory: "",
      workspaceGit: null,
    });
  });

  afterEach(() => cleanup());

  it("re-renders only the affected row when a menu opens or selection changes", () => {
    render(<ParentWithUnrelatedUpdates />);
    expect(sessionRowRenderMock).toHaveBeenCalledTimes(3);

    sessionRowRenderMock.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "父级无关更新" }));
    expect(sessionRowRenderMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "切换活动会话" }));
    expect(sessionRowRenderMock.mock.calls.map(([id]) => id)).toEqual(["conv-a", "conv-b"]);

    sessionRowRenderMock.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "打开菜单 Task A" }));
    expect(sessionRowRenderMock.mock.calls.map(([id]) => id)).toEqual(["conv-a"]);

    fireEvent.click(screen.getByRole("button", { name: "选择会话" }));
    sessionRowRenderMock.mockClear();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Task B" }));
    expect(sessionRowRenderMock.mock.calls.map(([id]) => id)).toEqual(["conv-b"]);
  });
});
