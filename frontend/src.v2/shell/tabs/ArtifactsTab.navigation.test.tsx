/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../../stores";
import { ArtifactsTab } from "./ArtifactsTab";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
afterEach(cleanup);

describe("artifact search and return", () => {
  it("finds an older output beyond thirty entries and returns to its actual producing turn", () => {
    useAppStore.setState({ conversationId: "owner", messageRevealTarget: null, previewArtifact: null,
      messages: Array.from({ length: 35 }, (_, index) => ({ id: `producer-${index}`, turnId: `turn-${index}`,
        role: "assistant" as const, content: "created", timestamp: 1000 + index, artifacts: [{ artifactId: `file-${index}`, kind: "file" as const, summary: `report-${index}.txt`, mediaType: "text/plain" }] })),
    });
    render(<ArtifactsTab />);
    fireEvent.change(screen.getByRole("textbox", { name: "搜索文件、附件与执行结果" }), { target: { value: "report-0.txt" } });
    expect(screen.getByText("report-0.txt")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "回到对话" }));
    expect(useAppStore.getState().messageRevealTarget).toMatchObject({ conversationId: "owner", messageId: "producer-0" });
    expect(useAppStore.getState().editorOpenRequests.some((request) => request.path === "report-0.txt")).toBe(false);
  });
});
