/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../../stores";
import { AsyncUserMessage } from "./AsyncUserMessage";
import { sendChatMessage } from "../sendChatMessage";

vi.mock("../sendChatMessage", () => ({ sendChatMessage: vi.fn(async () => false) }));
beforeEach(() => { vi.clearAllMocks(); useAppStore.setState({ conversationId: "question-owner", messages: [
  { id: "new-answer", turnId: "new-unrelated-turn", role: "assistant", content: "new work", artifacts: [], timestamp: 2, isStreaming: true },
], conversationMessages: {}, sideChats: {} }); });
afterEach(cleanup);

it("answers the question's original owning turn even when another turn is now visible", async () => {
  render(<AsyncUserMessage conversationId="question-owner" turnId="question-original-turn" readOnly={false}
    message={{ type: "text", itemId: "original-question", content: "Select a choice", delivery: "async", questions: [{ title: "Choice", options: ["Keep original"] }] }} />);
  fireEvent.click(screen.getByRole("button", { name: "发送回答" }));
  await waitFor(() => expect(sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({ asyncRequestId: "original-question",
    busyBehavior: "steer", expectedTurnId: "question-original-turn" })));
  expect((screen.getByRole("radio", { name: "Keep original" }) as HTMLInputElement).checked).toBe(true);
});
