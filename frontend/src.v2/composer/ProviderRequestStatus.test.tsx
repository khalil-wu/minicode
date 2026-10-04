// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentProgressEntry } from "../stores/types";
import { useAppStore } from "../stores";
import { ProviderRequestStatus } from "./ProviderRequestStatus";

vi.mock("../stores", async () => {
  const { create } = await import("zustand");
  return { useAppStore: create(() => ({ isStreaming: true, conversationId: "A", agentProgress: [] })) };
});

const request = (overrides: Partial<AgentProgressEntry> = {}): AgentProgressEntry => ({
  type: "progress", id: "provider:connection:run:iter:2", conversationId: "A",
  stage: "status", status: "running", providerState: "responding",
  visibility: "debug", message: "Provider connection established", timestamp: Date.now(),
  ...overrides,
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(200_000);
  useAppStore.setState({ isStreaming: true, conversationId: "A", agentProgress: [] });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("live provider request status", () => {
  it("shows debug request state outside the transcript", () => {
    useAppStore.setState({ agentProgress: [request()] });
    render(<ProviderRequestStatus />);
    expect(screen.getByRole("status").textContent).toContain("Waiting for model");
    expect(screen.getByRole("status").getAttribute("data-provider-request-state")).toBe("responding");
  });

  it("does not show another conversation's request", () => {
    useAppStore.setState({ agentProgress: [request({ conversationId: "B" })] });
    render(<ProviderRequestStatus />);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("does not revive an earlier request after the latest request completes", () => {
    useAppStore.setState({ agentProgress: [request({ id: "provider:connection:old" }), request({ status: "completed" })] });
    render(<ProviderRequestStatus />);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows elapsed time without announcing every clock tick", () => {
    useAppStore.setState({ agentProgress: [request()] });
    render(<ProviderRequestStatus />);
    act(() => vi.advanceTimersByTime(65_000));
    const elapsed = screen.getByText("1m 5s");
    expect(elapsed.getAttribute("aria-hidden")).toBe("true");
  });

  it("stops its timer when the run is cancelled", () => {
    useAppStore.setState({ agentProgress: [request()] });
    render(<ProviderRequestStatus />);
    expect(vi.getTimerCount()).toBe(1);
    act(() => useAppStore.setState({ isStreaming: false }));
    expect(screen.queryByRole("status")).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("keeps typed reconnect counters and clears on unmount", () => {
    useAppStore.setState({ agentProgress: [request({ providerState: "reconnecting", retryAttempt: 2, maxRetries: 10 })] });
    const view = render(<ProviderRequestStatus />);
    expect(screen.getByRole("status").textContent).toContain("Reconnecting 2/10");
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});
