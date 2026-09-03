import { describe, expect, it, vi } from "vitest";
import type { CommandResultEvent } from "../protocol/events";
import { reportCommandFailure } from "./commandFeedback";
import { pushToast } from "./ToastContainer";

vi.mock("./ToastContainer", () => ({
  pushToast: vi.fn(),
}));

vi.mock("../protocol/ws-outbox", () => ({
  commandResultSucceeded: (event: { level?: string }) => event.level !== "error" && event.level !== "failed",
}));

const result = (level: string, message = ""): CommandResultEvent => ({
  type: "command.result",
  command: "test",
  level,
  message,
  data: {},
});

describe("reportCommandFailure", () => {
  it("returns false without emitting a toast for successful results", () => {
    expect(reportCommandFailure(result("info"), "刷新")).toBe(false);
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("uses the default fallback for failures without a message", () => {
    expect(reportCommandFailure(result("error"), "刷新")).toBe(true);
    expect(pushToast).toHaveBeenCalledWith("刷新失败：后端未返回具体原因", "error");
  });

  it("preserves a caller-specific fallback", () => {
    vi.mocked(pushToast).mockClear();
    expect(reportCommandFailure(result("failed"), "连接", "服务未返回具体原因")).toBe(true);
    expect(pushToast).toHaveBeenCalledWith("连接失败：服务未返回具体原因", "error");
  });
});
