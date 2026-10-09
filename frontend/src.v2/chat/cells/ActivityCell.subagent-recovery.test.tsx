// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "../../stores";
import { hydrateMessages } from "../transcriptHydration";
import { projectMessagesToTurns } from "../chatSurfaceState";
import { ActivityCell } from "./ActivityCell";

beforeEach(() => useAppStore.setState({ conversationId: "child-parent", workingDirectory: "C:/workspace" }));
afterEach(cleanup);

describe("child provider failure evidence", () => {
  it("hydrates and renders the actual retry error while excluding normal provider handshakes", () => {
    const error = "stream closed before response.completed (request_timeout)";
    const retryMessage = "连接中断，正在重连（第 1/2 次）";
    const messages = hydrateMessages([{
      id: "child-turn", role: "assistant", content: "", timestamp: 1000, is_streaming: true,
      blocks: [
        { type: "progress", id: "provider:child:iteration-2", stage: "status", phase: "model",
          status: "running", providerState: "responding", message: "已连接，模型正在响应", visibility: "debug" },
        { type: "progress", id: "provider:child:iteration-2:error:reconnecting:1", stage: "status", phase: "recover",
          status: "info", providerState: "reconnecting", message: retryMessage, visibility: "timeline",
          retryAttempt: 1, maxRetries: 2, retryAfterMs: 200, errorMessage: error },
      ],
    }]);
    const turn = projectMessagesToTurns(messages, true)[0];
    const activities = turn.committedCells.filter(cell => cell.kind === "activity");
    expect(activities).toHaveLength(1);
    const activity = activities[0];
    expect(activity.title).toBe(retryMessage);
    expect(activity.progress).toMatchObject({ phase: "recover", retryAttempt: 1, maxRetries: 2, errorMessage: error });
    const view = render(<ActivityCell cell={{ ...activity, collapsed: false }} conversationId="child-parent" />);
    expect(view.container.textContent).toContain(retryMessage);
    expect(view.container.textContent).toContain("stream closed before response.completed");
    expect(view.container.textContent).not.toContain("已连接，模型正在响应");
  });
});
