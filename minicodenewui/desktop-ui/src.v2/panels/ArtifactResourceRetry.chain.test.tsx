// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { ToolCallCard } from "../chat/tool-calls/ToolCallCard";
import { ChatContextCard } from "../chat/ChatContextCard";
import { LiveArtifacts } from "../overlays/LiveArtifacts";
import type { ToolCallRecord } from "../lib/tool-call-reducer";

vi.hoisted(() => {
  (globalThis as unknown as { __MINICODE_RUNTIME__: object }).__MINICODE_RUNTIME__ = { runtimeToken: "test-resource-runtime-token" };
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }) });
});
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "current-session" }) }));

const artifact = { artifactId: "signed-resource", kind: "image" as const, mediaType: "image/png", summary: "Signed image" };
beforeEach(() => useAppStore.setState({
  conversationId: "resource-owner", isConnected: true, workingDirectory: "C:/owner", liveArtifactsOpen: true,
  contextCardCollapsed: false, rightPanelOpen: false,
  messages: [{ id: "assistant-image", role: "assistant", content: "", timestamp: 1, artifacts: [artifact] }],
  subagents: [], backgroundTasks: [], terminalSessions: [], turnDiffs: {},
  gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
}));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("real artifact image consumers after signed resource expiry", () => {
  it.each(["tool", "context", "gallery"])("re-signs %s resources on retry without resetting the new request", (surface) => {
    const clock = vi.spyOn(Date, "now").mockReturnValue(1_000_000);
    if (surface === "tool") {
      const record = { id: "image-call", name: "image_generation", args: {}, status: "success", artifactId: artifact.artifactId,
        artifactKind: "image", artifactMediaType: "image/png", summary: artifact.summary, startedAt: 1, finishedAt: 2 } as ToolCallRecord;
      render(<ToolCallCard record={record} conversationId="resource-owner" />);
    } else if (surface === "context") render(<ChatContextCard />);
    else render(<LiveArtifacts />);
    const resource = () => new URL(screen.getByRole("img").getAttribute("src")!);
    expect(resource().searchParams.get("asset_token")!.split(".")[0]).toBe("1300");
    clock.mockReturnValue(1_301_000);
    fireEvent.error(screen.getByRole("img"));
    if (surface === "gallery") fireEvent.error(screen.getByRole("img"));
    fireEvent.click(screen.getByRole("button", { name: /重试/ }));
    expect(resource().searchParams.get("asset_token")!.split(".")[0]).toBe("1601");
    expect(resource().searchParams.get("conversation_id")).toBe("resource-owner");
    expect(resource().searchParams.get("session_id")).toBe("current-session");
    expect(resource().searchParams.get("preview_retry")).toBe(surface === "gallery" ? "2" : "1");
  });
});
