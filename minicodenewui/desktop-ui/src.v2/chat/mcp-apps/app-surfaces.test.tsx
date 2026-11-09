/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { McpAppReceipt } from "../../protocol/mcp-app-types";
import { useAppStore } from "../../stores";
import { closeMcpAppPanel, McpAppInlineSlot, McpAppPanelSlot, McpAppSurfaces, useMcpAppSurfaces } from "./app-surfaces";
import { ChatTurn } from "../components/ChatTurn";
import { projectMessagesToTurns } from "../chatSurfaceState";
import type { ChatMessage } from "../../stores/types";
import { conversationReadingState, releaseConversationReadingState } from "../transcriptReadingState";

const calls = vi.hoisted(() => ({ mounted: vi.fn(), unmounted: vi.fn(), send: vi.fn(async () => true) }));
vi.mock("../sendChatMessage", () => ({ sendChatMessage: calls.send }));
vi.mock("./McpAppRenderer", async () => {
  const { useLayoutEffect, useRef, useState } = await import("react");
  return { McpAppRenderer: (props: import("./McpAppRenderer").McpAppRendererProps) => {
    const inline = useRef<HTMLDivElement>(null);
    const host = useRef<HTMLDivElement>();
    const [error, setError] = useState(false);
    useLayoutEffect(() => {
      calls.mounted(props.app.id);
      host.current = document.createElement("div");
      host.current.dataset.mcpAppId = props.app.id;
      host.current.appendChild(document.createElement("iframe"));
      inline.current!.appendChild(host.current);
      return () => { calls.unmounted(props.app.id); host.current!.remove(); };
    }, [props.app.id]);
    useLayoutEffect(() => {
      (props.fullscreenContainer || inline.current!).appendChild(host.current!);
    }, [props.fullscreenContainer]);
    return <div className="mc-mcp-app">
      <button onClick={() => props.onDisplayModeChange?.("fullscreen", props.app)}>展开应用</button>
      <button disabled={!props.onFollowUpMessage} onClick={() => props.onFollowUpMessage?.("应用后续消息")}>应用后续</button>
      <button onClick={() => setError(true)}>模拟资源错误</button>
      {error && props.fallback}
      <div ref={inline} className="mc-mcp-app-inline" hidden={error} />
    </div>;
  } };
});

const app: McpAppReceipt = { id: "a".repeat(32), server: "registered", tool: "chart", resourceUri: "ui://chart",
  displayMode: "inline", title: "真实工具应用", readOnly: false };
const frame = () => document.querySelector<HTMLIFrameElement>(`[data-mcp-app-id="${app.id}"] iframe`)!;

beforeEach(() => {
  vi.clearAllMocks();
  releaseConversationReadingState("owner");
  useMcpAppSurfaces.setState({ entries: [] });
  useAppStore.setState({ conversationId: "owner", rightPanelOpen: false, rightStackTab: "tasks", followUpBehavior: "queue", draft: "未提交草稿" });
  // jsdom checks React/DOM ownership only. The real browser suite verifies
  // moveBefore preserves the document, initialization and MessagePort.
  Object.defineProperty(HTMLElement.prototype, "moveBefore", { configurable: true,
    value(this: HTMLElement, node: Node, child: Node | null) { this.insertBefore(node, child); } });
});
afterEach(() => { cleanup(); useMcpAppSurfaces.setState({ entries: [] }); });

it("keeps one App instance through live→history, complete-final folding and virtual-row remount", async () => {
  const messages: ChatMessage[] = [{ id: "u", role: "user", content: "绘制图表", timestamp: 1 },
    { id: "a", role: "assistant", content: "", timestamp: 2, isStreaming: true, blocks: [{ type: "tool_call", record: {
      id: "chart-call", name: "chart", args: {}, activityKind: "mcpToolCall", resultKind: "mcp", status: "success",
      startedAt: 2, finishedAt: 3, mcpApp: app, outputPreview: "原始 MCP 输出",
    } }] }];
  const active = projectMessagesToTurns(messages, true)[0];
  const completed = projectMessagesToTurns([messages[0], { ...messages[1], isStreaming: false, content: "图表已完成。",
    blocks: [...messages[1].blocks!, { type: "text", itemId: "final", source: "model_final", status: "completed", content: "图表已完成。", isStreaming: false }] }], false)[0];
  const view = (phase: "live" | "history" | "offscreen") => <>
    {phase === "live" && <div key="live"><ChatTurn turn={active} conversationId="owner" /></div>}
    {phase === "history" && <section key="history"><ChatTurn turn={completed} conversationId="owner" /></section>}
    <McpAppSurfaces />
  </>;
  const { rerender, container } = render(view("live"));
  await waitFor(() => expect(frame()).toBeTruthy());
  const original = frame();
  expect(container.querySelectorAll(".activity-cell-main-button")).toHaveLength(0);
  rerender(view("history"));
  expect(container.querySelector('[data-zone="work"]')?.getAttribute("data-collapsed")).toBe("true");
  expect(container.querySelector(`[data-mcp-app-inline-slot] iframe`)).toBe(original);
  fireEvent.click(screen.getByRole("button", { name: "展开处理步骤" }));
  expect(container.querySelectorAll("[data-mcp-app-inline-slot]")).toHaveLength(1);
  expect(container.querySelectorAll(".activity-cell-main-button")).toHaveLength(0);
  expect(frame()).toBe(original);
  fireEvent.click(screen.getByRole("button", { name: "收起处理步骤" }));
  expect(frame()).toBe(original);
  rerender(view("offscreen"));
  expect(original.isConnected).toBe(true);
  expect(document.querySelector('[data-mcp-app-parking] iframe')).toBe(original);
  rerender(view("history"));
  expect(container.querySelector(`[data-mcp-app-inline-slot] iframe`)).toBe(original);
  expect(calls.mounted).toHaveBeenCalledTimes(1);
  expect(calls.unmounted).not.toHaveBeenCalled();
});

it("shows the original record on App failure and retains its conversation reading owner", async () => {
  const messages: ChatMessage[] = [{ id: "u", role: "user", content: "查看图表", timestamp: 1 },
    { id: "a", role: "assistant", content: "", timestamp: 2, isStreaming: true, blocks: [{ type: "tool_call", record: {
      id: "chart-call", name: "chart", args: {}, activityKind: "mcpToolCall", resultKind: "mcp", status: "success",
      startedAt: 2, finishedAt: 3, mcpApp: app, outputPreview: "原始 MCP 记录仍可阅读",
    } }] }];
  const turn = projectMessagesToTurns(messages, true)[0];
  const { container } = render(<><ChatTurn turn={turn} conversationId="owner" /><McpAppSurfaces /></>);
  await waitFor(() => expect(frame()).toBeTruthy());
  expect(container.querySelector(".activity-cell-main-button")).toBeNull();
  fireEvent.click(screen.getByText("模拟资源错误"));
  fireEvent.click(container.querySelector<HTMLButtonElement>(".activity-cell-main-button")!);
  expect(screen.getByText("原始 MCP 记录仍可阅读")).toBeTruthy();
  expect(conversationReadingState("owner").lastInteractedTurn).toBe(turn.id);
});

it("returns the original frame before its fullscreen panel is removed", async () => {
  const view = (panel: boolean) => <><McpAppInlineSlot app={app} conversationId="owner" />
    {panel && <McpAppPanelSlot id={app.id} />}<McpAppSurfaces /></>;
  const { rerender } = render(view(false));
  await waitFor(() => expect(frame()).toBeTruthy());
  const original = frame();
  fireEvent.click(screen.getByText("展开应用"));
  expect(useAppStore.getState()).toMatchObject({ rightPanelOpen: true, rightStackTab: `mcp-app:${app.id}` });
  rerender(view(true));
  expect(document.querySelector('[data-mcp-app-side-panel-frame-container] iframe')).toBe(original);
  act(() => closeMcpAppPanel(app.id));
  rerender(view(false));
  expect(document.querySelector('[data-mcp-app-inline-slot] iframe')).toBe(original);
  expect(original.isConnected).toBe(true);
  expect(calls.mounted).toHaveBeenCalledTimes(1);
});

it("uses the admitted conversation for confirmed follow-ups and keeps the composer draft", async () => {
  render(<><McpAppInlineSlot app={app} conversationId="owner" /><McpAppSurfaces /></>);
  await waitFor(() => expect(frame()).toBeTruthy());
  fireEvent.click(screen.getByText("应用后续"));
  expect(calls.send).toHaveBeenCalledWith({ displayContent: "应用后续消息", conversationId: "owner",
    allowWhileStreaming: true, awaitAdmission: true, busyBehavior: "queue" });
  expect(useAppStore.getState().draft).toBe("未提交草稿");
});

it("keeps a child work record read-only and closes its surface on host conversation switch", async () => {
  render(<><McpAppInlineSlot app={app} conversationId="child" readOnly /><McpAppSurfaces /></>);
  await waitFor(() => expect(frame()).toBeTruthy());
  expect((screen.getByText("应用后续") as HTMLButtonElement).disabled).toBe(true);
  act(() => useAppStore.setState({ conversationId: "other" }));
  await waitFor(() => expect(calls.unmounted).toHaveBeenCalledTimes(1));
  expect(calls.send).not.toHaveBeenCalled();
});
