/* @vitest-environment jsdom */

import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ActivityCell } from "./cells/ActivityCell";
import type { ActivityCellState } from "./cells/cellTypes";
import { ToolCallCard } from "./tool-calls/ToolCallCard";
import { getRecordOutputText } from "./cells/activityCellHelpers";
import type { ToolCallRecord } from "../lib/tool-call-reducer";

const { openArtifactMock } = vi.hoisted(() => ({ openArtifactMock: vi.fn() }));
vi.mock("./openAttachmentPreview", () => ({ openArtifactPreview: openArtifactMock, openWorkspaceFilePreview: vi.fn() }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ send: vi.fn(), sessionId: "session" }) }));

afterEach(() => { cleanup(); vi.clearAllMocks(); });

const expandedCell = (record: ToolCallRecord): ActivityCellState => ({
  kind: "activity", id: `activity-${record.id}`, activityKind: "webSearch", title: "Search · Fetch",
  status: "done", collapsed: false, startedAt: 1, toolCallRecords: [record],
});

describe("tool result disclosure", () => {
  it("shows the complete canonical result past the short content preview", () => {
    const record: ToolCallRecord = {
      id: "full-result", name: "read_artifact", args: {}, status: "success",
      summary: "Header\n" + "Detailed result\n".repeat(80) + "Final diagnostic line",
      contentPreview: "Header", startedAt: 1,
    };
    render(<ActivityCell cell={{ ...expandedCell(record), activityKind: "fileRead" }} />);
    expect(document.body.textContent).toContain("Final diagnostic line");
  });

  it.each(["monitor", "task", "task_status", "task_get"])("keeps %s typed presentation free of internal receipt ids", (name) => {
    const record: ToolCallRecord = {
      id: "receipt", name, args: {}, status: "partial",
      summary: "subagent_id: internal-private-receipt\nActual result",
      contentPreview: "Partial\nActual result",
    };
    expect(getRecordOutputText(record)).toBe("Partial\nActual result");
    expect(getRecordOutputText(record)).not.toContain("internal-private-receipt");
  });

  it("keeps the Fetch extraction separate from its raw page artifact and owner", () => {
    const record: ToolCallRecord = {
      id: "fetch", name: "web_fetch", args: { url: "https://example.test/jobs" }, status: "success",
      summary: "Extracted job requirements: Python and SQL",
      contentPreview: "Navigation and raw page text", extractionStatus: "ok", evidenceType: "fetched",
      artifactId: "art_raw_page", artifactMediaType: "text/plain",
    };
    render(<ActivityCell cell={expandedCell(record)} conversationId="fetch-owner" />);
    expect(document.body.textContent).toContain("Extracted job requirements: Python and SQL");
    expect(document.body.textContent).not.toContain("Navigation and raw page text");
    expect(screen.getByText("已获取正文")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看页面正文" }));
    expect(openArtifactMock).toHaveBeenCalledWith({
      artifactId: "art_raw_page", name: "页面正文", kind: "text", mediaType: "text/plain", conversationId: "fetch-owner",
    });
  });

  it.each([
    ["failed", "未获取有效内容"], ["partial", "内容不完整"], ["ok", "已获取正文"],
  ])("uses extraction status %s for a legacy success card", (extractionStatus, label) => {
    render(<ToolCallCard record={{
      id: "legacy-fetch", name: "web_fetch", args: { url: "https://example.test/jobs" }, status: "success",
      extractionStatus, evidenceType: "fetched", summary: "Actual extraction outcome",
    }} viewMode="verbose" conversationId="fetch-owner" />);
    expect(screen.getByText(label)).toBeTruthy();
    expect(document.body.textContent).not.toContain("已获取证据");
  });
});
