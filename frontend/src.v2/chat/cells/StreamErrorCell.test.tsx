// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { LanguageProvider } from "../../i18n";
import { hydrateMessages } from "../transcriptHydration";
import { projectMessagesToTurns } from "../chatSurfaceState";
import type { ActivityCellState } from "./cellTypes";
import { StreamErrorCell } from "./StreamErrorCell";

beforeEach(() => vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener() {}, removeEventListener() {} })));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const retry = (attempt: number, evidence: Record<string, unknown> = {}) => ({
  type: "progress", id: `provider:connection:turn:iteration:error:request-${attempt}:reconnecting`,
  stage: "status", phase: "recover", status: "info", visibility: "timeline", provider_state: "reconnecting",
  message: `Reconnecting ${attempt}/5`, retry_attempt: attempt, max_retries: 5,
  error_message: "Original provider failure", detail: "Original HTTP 429 detail https://example.com/request",
  timestamp: attempt * 1000, ...evidence,
});
const projected = (blocks: ReturnType<typeof retry>[]) => {
  const messages = hydrateMessages([{ id: "answer", role: "assistant", content: "", timestamp: 1000, blocks }]);
  const turn = projectMessagesToTurns(messages, true)[0];
  const cell = [...turn.committedCells, turn.activeCell].find((value): value is ActivityCellState => value?.kind === "activity");
  expect(cell).toBeTruthy();
  return cell!;
};
const view = (cell: ActivityCellState) => <LanguageProvider locale="en-US"><StreamErrorCell cell={cell} /></LanguageProvider>;

it("restores and coalesces 429, ordinary 503 and absent HTTP evidence without retaining a busy label", () => {
  const busy = retry(1, { status_code: 429, provider_error_code: "controlled_busy" });
  const rendered = render(view(projected([busy])));
  const summary = screen.getByRole("button", { name: /Server is busy, reconnecting/ });
  fireEvent.click(summary);
  expect(screen.getByText("Original HTTP 429 detail https://example.com/request")).toBeTruthy();
  expect(rendered.container.querySelector("a")).toBeNull();
  expect(rendered.container.querySelector('[data-source-icon="stream-error-chevron"]')?.getAttribute("width")).toBe("12");
  const ordinary = retry(2, { status_code: 503, provider_error_code: "controlled_retry" });
  rendered.rerender(view(projected([busy, ordinary])));
  expect(screen.getByRole("button", { name: /Reconnecting 2\/5/ })).toBe(summary);
  expect(summary.getAttribute("aria-expanded")).toBe("true");
  const noHttp = retry(3);
  const latest = projected([busy, ordinary, noHttp]);
  expect(latest.progress?.statusCode).toBeUndefined();
  expect(latest.progress?.providerErrorCode).toBeUndefined();
  rendered.rerender(view(latest));
  expect(screen.getByRole("button", { name: /Reconnecting 3\/5/ })).toBe(summary);
  expect(summary.textContent).not.toContain("Server is busy");
});

it("keeps each actual retry failure in the shared expanded detail after history hydration", () => {
  const first = retry(1, { detail: "First gateway failure", error_message: "First gateway failure" });
  const second = retry(2, { detail: "Second gateway failure", error_message: "Second gateway failure" });
  const cell = projected([first, second]);
  const result = render(view(cell));
  fireEvent.click(screen.getByRole("button", { name: /Reconnecting 2\/5/ }));
  expect(result.container.querySelectorAll(".stream-error-details")).toHaveLength(2);
  expect(result.container.textContent).toContain("First gateway failure");
  expect(result.container.textContent).toContain("Second gateway failure");
});

it("keeps failures from different provider operations in separate rows", () => {
  const first = retry(1, { operation_id: "request-first", detail: "First operation failed" });
  const second = retry(1, { operation_id: "request-second", detail: "Second operation failed" });
  const messages = hydrateMessages([{ id: "answer", role: "assistant", content: "", timestamp: 1000, blocks: [first, second] }]);
  const activities = projectMessagesToTurns(messages, true)[0].committedCells.filter(cell => cell.kind === "activity");
  expect(activities).toHaveLength(2);
});

it.each([
  [{ status_code: 503, provider_error_code: "server_is_overloaded" }, "Server is busy, reconnecting"],
  [{ provider_error_code: "rate_limit", message: "server overloaded HTTP 429" }, "Reconnecting"],
])("uses structured overload evidence rather than provider prose (%j)", (evidence, expected) => {
  render(view(projected([retry(1, evidence as Record<string, unknown>)])));
  const summary = screen.getByRole("button");
  expect(summary.textContent).toContain(expected);
  if (expected === "Reconnecting") expect(summary.textContent).not.toContain("Server is busy");
});
