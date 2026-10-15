/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GuardianAssessment } from "../protocol/guardian-types";
import type { CommandResultEvent } from "../protocol/events";
import type { ChatMessage } from "../stores/types";
import { useAppStore } from "../stores";
import * as outbox from "../protocol/ws-outbox";
import { GuardianManualApproval } from "./cells/GuardianManualApproval";
import { GuardianApprovalMenu } from "../composer/GuardianApprovalMenu";
import { approveGuardianDeniedAction, guardianApprovalState, recentGuardianDenials } from "./guardianApprovalState";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const review = (id: string): GuardianAssessment => ({ id, status: "denied", turn_id: "turn", started_at_ms: 1000,
  action: { type: "command", source: "unified_exec", command: `command-${id}`, cwd: "C:/original" }, rationale: `reason-${id}` });
const result = (owner: string, id: string): CommandResultEvent => ({ type: "command.result", command: "thread/approveGuardianDeniedAction", message: "", level: "success",
  data: { conversation_id: owner, review_id: id, approval_recorded: true } });
const message = (reviews: GuardianAssessment[]): ChatMessage => ({ id: "answer", role: "assistant", turnId: "turn", timestamp: 1000, content: "",
  blocks: reviews.map(item => ({ type: "guardian_assessment", review: item })) });

describe("Source manual denied-action approval", () => {
  it("disables all approval buttons in the thread until the exact request is recorded; never sends a retry turn", async () => {
    const owner = "manual-button-owner";
    const first = review("one");
    let resolve!: (event: CommandResultEvent) => void;
    const send = vi.spyOn(outbox, "sendClientCommandAwaitResult").mockImplementation(() => new Promise(done => { resolve = done; }));
    const { container } = render(<><GuardianManualApproval conversationId={owner} review={first} /><GuardianManualApproval conversationId={owner} review={review("two")} /></>);
    fireEvent.click(screen.getAllByRole("button", { name: "批准" })[0]);
    expect(screen.getByRole("button", { name: "正在批准…" })).toBeTruthy();
    expect([...container.querySelectorAll("button")].every(button => button.disabled)).toBe(true);
    expect(send).toHaveBeenCalledWith({ type: "thread/approveGuardianDeniedAction", threadId: owner, conversation_id: owner, event: first }, "thread/approveGuardianDeniedAction", { silent: true });
    await act(async () => { resolve(result(owner, first.id)); });
    expect(screen.getAllByRole("button", { name: "批准" })).toHaveLength(1);
    expect(guardianApprovalState(owner).approved.has(first.id)).toBe(true);
    expect(guardianApprovalState(owner).pending).toBeNull();
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("restores pending controls after a failed save and keeps the selected denial available", async () => {
    const owner = "manual-failed-owner";
    const item = review("failed-save");
    vi.spyOn(outbox, "sendClientCommandAwaitResult").mockRejectedValueOnce(new Error("disk unavailable"));
    render(<GuardianManualApproval conversationId={owner} review={item} />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "批准" })); });
    expect((screen.getByRole("button", { name: "批准" }) as HTMLButtonElement).disabled).toBe(false);
    expect(guardianApprovalState(owner).approved.has(item.id)).toBe(false);
    expect(guardianApprovalState(owner).pending).toBeNull();
  });

  it("does not accept an unrelated command result as authorization", async () => {
    const owner = "manual-wrong-result-owner";
    vi.spyOn(outbox, "sendClientCommandAwaitResult").mockResolvedValue(result("other-conversation", "selected"));
    await expect(approveGuardianDeniedAction(owner, review("selected"))).rejects.toThrow();
    expect(guardianApprovalState(owner).approved.size).toBe(0);
    expect(guardianApprovalState(owner).pending).toBeNull();
  });

  it("shows the newest ten real denied events and preserves the composer draft while recording approval", async () => {
    const owner = "manual-menu-owner";
    const reviews = Array.from({ length: 12 }, (_, index) => review(String(index)));
    useAppStore.setState({ conversationId: owner, messages: [message([...reviews, { ...review("approved"), status: "approved" }])], draft: "/approve", attachments: [] });
    expect(recentGuardianDenials(owner).map(item => item.id)).toEqual(["11", "10", "9", "8", "7", "6", "5", "4", "3", "2"]);
    const send = vi.spyOn(outbox, "sendClientCommandAwaitResult").mockResolvedValue(result(owner, "11"));
    const approved = vi.fn();
    render(<GuardianApprovalMenu conversationId={owner} onClose={vi.fn()} onApproved={approved} />);
    expect(screen.getAllByRole("menuitem")).toHaveLength(10);
    expect(screen.queryByText("command-approved")).toBeNull();
    await act(async () => { fireEvent.click(screen.getByRole("menuitem", { name: /^command-11\s*reason-11$/ })); });
    expect(approved).toHaveBeenCalledOnce();
    expect(useAppStore.getState().draft).toBe("/approve");
    expect(send).toHaveBeenCalledTimes(1);
    expect(recentGuardianDenials(owner).some(item => item.id === "11")).toBe(false);
  });
});
