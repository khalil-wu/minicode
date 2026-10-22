// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { LanguageProvider } from "../i18n";
import { projectTurn } from "../lib/turn-projection";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { handleRuntimeEvent } from "./runtimeEvents";
import { hydrateMessages } from "./transcriptHydration";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { HistoryCellRenderer } from "./components/ChatTurn";

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  useAppStore.setState({ conversationId: "A", draft: "unsent A", messages: [], conversationMessages: {},
    conversationStreaming: {}, pendingProviderProgress: {}, agentProgress: [], conversationAgentProgress: {}, sideChats: {} });
  useAppStore.getState().resumeStreaming("A", [], "answer-A", "turn-A");
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("keeps actual debug context rebuilding out of the user-facing reconnect projection", () => {
  const projection = projectTurn([{ type: "progress", id: "provider:connection:turn-A:iteration-1", stage: "status",
    phase: "recover", status: "running", message: "正在调整请求上下文后重试", providerState: "reconnecting",
    retryAttempt: 1, maxRetries: 5, errorMessage: "context rebuild required", visibility: "debug", timestamp: 100 }], { isStreaming: true });
  expect(projection.activityItems).toEqual([]);
});

it("projects real retry wire fields into one stable Wi-Fi disclosure through live delivery and restored history", () => {
  const retry = (attempt: number) => normalizeInboundServerEvent({ type: "agent.progress", conversation_id: "A",
    message_id: "answer-A", turn_id: "turn-A", id: `provider:connection:turn-A:iteration-1:error:span-${attempt}:reconnecting`,
    operation_id: "provider:connection:turn-A:iteration-1", stage: "status", phase: "recover", status: "info",
    message: `连接中断，正在重连（第 ${attempt}/5 次）`, provider_state: "reconnecting", retry_attempt: attempt,
    max_retries: 5, retry_after_ms: 200, error_message: `stream closed ${attempt}`,
    detail: `stream closed ${attempt} · 0.2 秒后重试`, visibility: "timeline" })!;
  handleRuntimeEvent(retry(1));
  const first = projectMessagesToTurns(useAppStore.getState().messages, true)[0].committedCells[0];
  handleRuntimeEvent(retry(2));
  const state = useAppStore.getState();
  const cells = projectMessagesToTurns(state.messages, true)[0].committedCells;
  expect(cells).toHaveLength(1);
  expect(cells[0].id).toBe(first.id);
  expect(cells[0]).toMatchObject({ kind: "activity", status: "done", progress: { retryAttempt: 2, maxRetries: 5 } });
  const view = render(<LanguageProvider locale="en-US"><HistoryCellRenderer cell={cells[0]} conversationId="A" /></LanguageProvider>);
  expect(view.container.querySelector('[data-source-icon="wifi-light-16"]')).toBeTruthy();
  expect(view.container.textContent).toContain("Reconnecting 2/5");
  expect(view.container.textContent).not.toContain("stream closed");
  fireEvent.click(screen.getByRole("button", { name: "Reconnecting 2/5" }));
  expect(view.container.textContent).toContain("stream closed 2");
  expect(view.container.textContent).not.toContain("stream closed 1");
  expect(state.isStreaming).toBe(true);
  expect(state.draft).toBe("unsent A");

  const restored = hydrateMessages([{ ...state.messages[0], blocks: JSON.parse(JSON.stringify(state.messages[0].blocks)) }]);
  const restoredCells = projectMessagesToTurns(restored, true)[0].committedCells;
  expect(restoredCells).toHaveLength(1);
  expect(restoredCells[0]).toMatchObject({ id: first.id, progress: { retryAttempt: 2, errorMessage: "stream closed 2" } });
});
