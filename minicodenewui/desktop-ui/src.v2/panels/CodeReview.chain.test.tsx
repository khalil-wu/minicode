/* @vitest-environment jsdom */
import React from "react";
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { codeReviewState, publishCodeReview } from "../chat/codeReview";
import { hydrateMessages } from "../chat/transcriptHydration";
import { DiffPanel } from "./DiffPanel";
import type { PreparedCodeReview } from "../protocol/code-review-types";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../lib/monaco-colorize", async load => ({ ...await load<typeof import("../lib/monaco-colorize")>(), useColorizedLines: () => null }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(), sendClientCommandAwaitResult: vi.fn(),
  sendPromptResponseCommand: vi.fn(), commandResultSucceeded: (event: { level?: string }) => event.level !== "error" }));
const prepared: PreparedCodeReview = { scope: { mode: "uncommitted", cwd: "C:/work", gitRoot: "C:/work" },
  prompt: "## Code review guidelines:\nFull source guidelines\n## My request:\nReview my changes", requestMessage: "Review my changes",
  diff: "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n" };
afterEach(cleanup);

it("projects two actual final directives to the same right-side diff line as distinct comments", async () => {
  useAppStore.setState({ conversationId: "owner", workingDirectory: "C:/work", requestGitChanges: vi.fn(), gitReviewRequest: null,
    turnDiffs: {}, diffReview: codeReviewState(prepared, "owner", "user"), messages: [{ id: "answer", role: "assistant", artifacts: [], timestamp: 1,
      content: 'Found issues.\n::code-comment{title="One" body="First reason" file="C:/work/app.py" start=1}\n::code-comment{title="Two" body="Second reason" file="C:/work/app.py" end=1}',
      terminalStatus: "completed", isStreaming: false }] });
  const view = render(<DiffPanel />);
  await waitFor(() => expect(view.container.querySelectorAll('[data-code-review-comment][data-code-review-file="app.py"][data-code-review-line="1"]')).toHaveLength(2));
  expect(view.container.textContent).toContain("First reason");
  expect(view.container.textContent).toContain("Second reason");
  expect(view.container.textContent).not.toContain("::code-comment");
});

it("cold transcript keeps the visible request, full model input and exact review scope together", () => {
  const [message] = hydrateMessages([{ id: "user", role: "user", content: prepared.prompt,
    display_content: prepared.requestMessage, code_review: prepared.scope }]);
  expect(message.content).toBe(prepared.requestMessage);
  expect(message.backendContent).toBe(prepared.prompt);
  expect(message.codeReview).toEqual(prepared.scope);
});

it("a late review receipt updates its real owner without overwriting the newly selected draft", () => {
  useAppStore.setState({ conversationId: "other", workingDirectory: "C:/elsewhere", draft: "unsaved other input", conversationWorkbenchStates: {} });
  publishCodeReview(prepared, "owner", "user");
  expect(useAppStore.getState().draft).toBe("unsaved other input");
  expect(useAppStore.getState().conversationWorkbenchStates.owner.diffReview?.conversationId).toBe("owner");
});
