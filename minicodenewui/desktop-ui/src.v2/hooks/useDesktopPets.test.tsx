/* @vitest-environment jsdom */
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { useDesktopPets } from "./useDesktopPets";
import { LS } from "../stores/shared-helpers";
import * as petActivities from "../pets/petActivities";

const mocks = vi.hoisted(() => ({
  publish: vi.fn(), update: vi.fn(async () => {}), toast: vi.fn(),
  onAction: vi.fn(() => () => {}), complete: vi.fn(async () => {}),
}));
vi.mock("../desktop/runtime", async load => ({ ...await load<Record<string, unknown>>(), isDesktop: () => true,
  desktop: () => ({ pets: { publishActivity: mocks.publish, update: mocks.update, onActionRequested: mocks.onAction, completeAction: mocks.complete } }) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: (...args: unknown[]) => mocks.toast(...args) }));
const Harness = () => { useDesktopPets(); return null; };
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.clearAllMocks(); });

it("keeps a failed initialization visible without retrying on draft edits, and resends real activity after explicit Refresh", async () => {
  mocks.publish.mockRejectedValueOnce(new Error("Pet preferences could not be read")).mockResolvedValue(undefined);
  useAppStore.setState({ conversationId: "actual", conversations: [{ id: "actual", title: "Actual task", updatedAt: "2026-10-20", sessionStatus: "running" }],
    messages: [], conversationStreaming: {}, isStreaming: true, isConnected: true, reducedMotion: false,
    pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [] });
  render(<Harness />);
  await waitFor(() => expect(mocks.toast).toHaveBeenCalledTimes(1));
  act(() => useAppStore.setState({ draft: "Unrelated unsent text" }));
  expect(mocks.publish).toHaveBeenCalledTimes(1);
  act(() => window.dispatchEvent(new Event("pets:refreshed")));
  await waitFor(() => expect(mocks.publish).toHaveBeenCalledTimes(2));
  expect(mocks.publish.mock.calls[1][0].tasks[0]).toMatchObject({ id: "actual", title: "Actual task", status: "running" });
  act(() => useAppStore.setState({ draft: "Keep editing" }));
  expect(mocks.publish).toHaveBeenCalledTimes(2);
  expect(useAppStore.getState().draft).toBe("Keep editing");
});

it("sends the resolved application motion preference to the Mini owner", async () => {
  const initial = useAppStore.getState();
  const saved = localStorage.getItem(LS.reducedMotion);
  const attribute = document.documentElement.getAttribute("data-reduced-motion");
  mocks.publish.mockResolvedValue(undefined);
  vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener() {}, removeEventListener() {} }));
  try {
    useAppStore.getState().setReducedMotionPreference("off");
    render(<Harness />);
    await waitFor(() => expect(mocks.update).toHaveBeenLastCalledWith({ reducedMotion: false }));
    vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
    act(() => useAppStore.getState().setReducedMotionPreference("on"));
    await waitFor(() => expect(mocks.update).toHaveBeenLastCalledWith({ reducedMotion: true }));
  } finally {
    cleanup(); vi.unstubAllGlobals();
    if (saved === null) localStorage.removeItem(LS.reducedMotion); else localStorage.setItem(LS.reducedMotion, saved);
    if (attribute === null) document.documentElement.removeAttribute("data-reduced-motion"); else document.documentElement.setAttribute("data-reduced-motion", attribute);
    useAppStore.setState({ reducedMotion: initial.reducedMotion, reducedMotionPreference: initial.reducedMotionPreference });
  }
});

it("does not rebuild pet tasks for streamed text or draft edits but tracks new turns, terminal results and prompts", () => {
  mocks.publish.mockResolvedValue(undefined);
  useAppStore.setState({ conversationId: "pet-stream", conversations: [{ id: "pet-stream", title: "Actual turn", updatedAt: "2026-11-08" }],
    messages: [{ id: "reply", turnId: "turn-a", role: "assistant", content: "", timestamp: 1, artifacts: [], isStreaming: true }],
    conversationMessages: {}, sideChats: {}, conversationStreaming: { "pet-stream": true }, isStreaming: true, isConnected: true,
    pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [] });
  const project = vi.spyOn(petActivities, "desktopPetActivity");
  render(<Harness />);
  project.mockClear();
  for (let index = 0; index < 20; index++) act(() => useAppStore.setState(state => ({ draft: `Draft ${index}`,
    messages: [{ ...state.messages[0], content: `Streamed text ${index}` }] })));
  expect(project).not.toHaveBeenCalled();
  act(() => useAppStore.setState(state => ({ messages: [{ ...state.messages[0], turnId: "turn-b" }] })));
  expect(project).toHaveBeenCalledTimes(1);
  expect(mocks.publish.mock.calls.at(-1)![0].tasks[0].turnKey).toBe("turn-b");
  act(() => useAppStore.setState(state => ({ messages: [{ ...state.messages[0], isStreaming: false, terminalStatus: "failed" }],
    isStreaming: false, conversationStreaming: {} })));
  expect(mocks.publish.mock.calls.at(-1)![0].tasks[0].status).toBe("failed");
  act(() => useAppStore.setState({ pendingAskUser: { requestId: "question", conversationId: "pet-stream", turnId: "turn-b", question: "Choose", options: [{ label: "One", value: "one" }] } }));
  expect(mocks.publish.mock.calls.at(-1)![0].tasks[0].status).toBe("waiting");
  expect(mocks.publish.mock.calls.at(-1)![0].tasks[0].actions[0].label).toBe("One");
});
