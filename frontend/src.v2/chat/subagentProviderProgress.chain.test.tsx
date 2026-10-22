// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { SubagentProgressEvent, SubagentTranscriptEventsDelta } from "../protocol/streaming-types";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { LanguageProvider } from "../i18n";
import { applySubagentTranscriptEvents } from "./subagentTranscript";
import { hydrateMessages } from "./transcriptHydration";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { HistoryCellRenderer } from "./components/ChatTurn";

afterEach(cleanup);

const operation = "provider:connection:child:iteration-1";
const fact = (id: string, payload: Record<string, unknown>) => ({ event_id: id, seq: 2, ts_ms: 1100,
  event_type: "system" as const, payload: { lifecycle: "provider_progress", operation_id: operation,
    provider_span_id: "original-span", provider_state: "reconnecting", retry_attempt: 1, max_retries: 2,
    message: "Reconnecting 1/2", error_message: "Actual stream interruption", visibility: "timeline", ...payload } });
const decode = (events: SubagentTranscriptEventsDelta["events"], base = 1): SubagentTranscriptEventsDelta => {
  const event = normalizeInboundServerEvent(JSON.parse(JSON.stringify({ type: "subagent.progress", conversation_id: "parent", subagent_id: "child",
    transcript_delta: { kind: "events", base_seq: base, seq: base + events.length,
      events: events.map((event, index) => ({ ...event, seq: base + index + 1 })) } }))) as SubagentProgressEvent;
  expect(event).toBeTruthy();
  return event.transcript_delta as SubagentTranscriptEventsDelta;
};
const initial = () => applySubagentTranscriptEvents([], decode([{ event_id: "start", seq: 1, ts_ms: 1000,
  event_type: "system", payload: { lifecycle: "turn_started", turn_id: "child" } }], 0))!;

it("keeps actual public retry evidence when debug lifecycle and context recovery arrive in the child delta", () => {
  const first = applySubagentTranscriptEvents(initial(), decode([fact("retry", { status_code: 429, provider_error_code: "controlled_busy" })]))!;
  const delta = decode([
    fact("context", { visibility: "debug", error_message: "Private request context" }),
    fact("lifecycle", { visibility: "debug", provider_state: "failed", error_message: "Private terminal lifecycle" }),
    fact("missing-visibility", { visibility: undefined, error_message: "Unpublished detail" }),
    fact("liveness", { provider_state: "failed", error_message: "" }),
    { event_id: "actual-failure", seq: 9, ts_ms: 1200, event_type: "system", payload: {
      lifecycle: "error", message: "The real terminal failure",
    } },
  ], 2);
  const next = applySubagentTranscriptEvents(first, delta)!;
  expect(next[0].failureMessage).toBe("The real terminal failure");
  expect(next[0].blocks).toEqual(first[0].blocks);
  expect(next[0].blocks?.[0]).toMatchObject({ id: `${operation}:error:original-span:reconnecting:1`,
    visibility: "timeline", status: "info", errorMessage: "Actual stream interruption", statusCode: 429, providerErrorCode: "controlled_busy" });
  const recovered = applySubagentTranscriptEvents(next, decode([fact("ordinary", { provider_span_id: "second-span", retry_attempt: 2,
    status_code: 503, provider_error_code: "controlled_retry" }), fact("unclassified", { provider_span_id: "third-span", retry_attempt: 3 })], 7))!;
  const restored = hydrateMessages([{ id: "restored-child", role: "assistant", content: "", timestamp: 1000,
    blocks: JSON.parse(JSON.stringify(recovered[0].blocks)) }]);
  const progress = restored[0].blocks?.filter(block => block.type === "progress");
  expect(progress?.[0]).toMatchObject({ statusCode: 429, providerErrorCode: "controlled_busy" });
  expect(progress?.[1]).toMatchObject({ statusCode: 503, providerErrorCode: "controlled_retry" });
  expect(progress?.[2]?.statusCode).toBeUndefined();
  expect(progress?.[2]?.providerErrorCode).toBeUndefined();
});

it("projects an explicit public child terminal failure into the existing neutral ErrorCell once", () => {
  const messages = applySubagentTranscriptEvents(initial(), decode([fact("failed", { provider_state: "failed", visibility: "compact",
    message: "Request failed", error_message: "The real terminal failure", detail: "HTTP=503 · code=public_failure" })]))!;
  expect(projectMessagesToTurns(messages, true)[0].committedCells[0]).toMatchObject({ kind: "error", recoverable: false });
  messages[0] = { ...messages[0], isStreaming: false, terminalStatus: "failed", failureMessage: "The real terminal failure",
    failureRecoverable: true, completedAt: 1200 };
  const cells = projectMessagesToTurns(messages, false)[0].committedCells;
  expect(cells).toHaveLength(1);
  expect(cells[0]).toMatchObject({ kind: "error", source: "agent", message: "The real terminal failure", recoverable: true,
    rawError: "HTTP=503 · code=public_failure" });
  const view = render(<LanguageProvider locale="en-US"><HistoryCellRenderer cell={cells[0]} conversationId="child" /></LanguageProvider>);
  expect(screen.getByRole("alert").textContent).toContain("The real terminal failure");
  expect(screen.getByRole("alert").textContent).toContain("HTTP=503 · code=public_failure");
  expect(view.container.querySelector(".error-cell-notice")).toBeTruthy();
  expect(view.container.querySelector(".activity-cell")).toBeNull();

  const restored = hydrateMessages([{ id: "restored-child", role: "assistant", content: "", timestamp: 1000,
    terminal_status: "failed", failure_message: "The real terminal failure", completed_at: 1200,
    blocks: JSON.parse(JSON.stringify(messages[0].blocks)) }]);
  expect(projectMessagesToTurns(restored, false)[0].committedCells.map(cell => cell.kind)).toEqual(["error"]);
});
