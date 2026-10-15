/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../../stores";
import { ArtifactsTab } from "./ArtifactsTab";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
afterEach(cleanup);

describe("artifact inventory and return", () => {
  it("retains an older output beyond thirty entries and returns to its actual producing turn", () => {
    useAppStore.setState({ conversationId: "owner", messageRevealTarget: null, previewArtifact: null,
      messages: Array.from({ length: 35 }, (_, index) => ({ id: `producer-${index}`, turnId: `turn-${index}`,
        role: "assistant" as const, content: "created", timestamp: 1000 + index, artifacts: [{ artifactId: `file-${index}`, kind: "file" as const, summary: `report-${index}.txt`, mediaType: "text/plain" }] })),
    });
    render(<ArtifactsTab />);
    fireEvent.click(screen.getByRole("button", { name: "再显示 27 项" }));
    expect(screen.getByText("report-0.txt")).toBeTruthy();
    fireEvent.click(within(screen.getByText("report-0.txt").closest("button")!.parentElement!).getByRole("button", { name: "回到对话" }));
    expect(useAppStore.getState().messageRevealTarget).toMatchObject({ conversationId: "owner", messageId: "producer-0" });
    expect(useAppStore.getState().editorOpenRequests.some((request) => request.path === "report-0.txt")).toBe(false);
  });
});
