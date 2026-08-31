import { beforeEach, describe, expect, it } from "vitest";

import { useAppStore } from "./index";

describe("background task retention", () => {
  beforeEach(() => {
    useAppStore.setState({ backgroundTasks: [] });
  });

  it("never evicts running tasks when settled history exceeds its bound", () => {
    useAppStore.getState().addBackgroundTask({
      id: "long-running",
      command: "serve",
      status: "running",
      timestamp: 0,
      conversationId: "conv-owner",
    });
    for (let index = 0; index < 40; index += 1) {
      useAppStore.getState().addBackgroundTask({
        id: `settled-${index}`,
        command: "check",
        status: "completed",
        timestamp: index + 1,
        conversationId: "conv-owner",
      });
    }

    const tasks = useAppStore.getState().backgroundTasks;
    expect(tasks.some((task) => task.id === "long-running" && task.status === "running")).toBe(true);
    expect(tasks.filter((task) => task.status !== "running")).toHaveLength(30);
  });

  it("keeps stalled commands active and isolates identical ids by conversation owner", () => {
    useAppStore.getState().addBackgroundTask({
      id: "shared-id",
      command: "npm create vite",
      status: "stalled",
      timestamp: 1,
      conversationId: "conv-first",
      stalledTail: "Overwrite? [y/N]",
    });
    useAppStore.getState().addBackgroundTask({
      id: "shared-id",
      command: "pytest",
      status: "running",
      timestamp: 2,
      conversationId: "conv-second",
    });
    useAppStore.getState().addBackgroundTask({
      id: "shared-id",
      command: "npm create vite",
      status: "running",
      timestamp: 0,
      conversationId: "conv-first",
    });

    expect(useAppStore.getState().backgroundTasks).toMatchObject([
      { id: "shared-id", conversationId: "conv-first", status: "stalled" },
      { id: "shared-id", conversationId: "conv-second", status: "running" },
    ]);
  });
});
