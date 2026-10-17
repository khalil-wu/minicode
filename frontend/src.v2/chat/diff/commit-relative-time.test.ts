import { expect, it } from "vitest";
import { compactCommitTime } from "./commit-relative-time";

it("keeps Source's minute/hour boundary and actual compact locale labels", () => {
  const now = new Date("2026-10-17T12:00:00Z");
  expect(compactCommitTime("2026-10-17T11:59:01Z", now, "en-US")).toBe("just now");
  expect(compactCommitTime("2026-10-17T11:59:00Z", now, "zh-CN")).toBe("1 分");
  expect(compactCommitTime("2026-10-17T11:00:00Z", now, "zh-CN")).toBe("1 小时");
  expect(compactCommitTime("2026-10-10T12:00:00Z", now, "en-US")).toBe("1w");
  expect(compactCommitTime("2025-10-17T12:00:00Z", now, "zh-CN")).toBe("1 年");
});

it("uses calendar dates after twenty-four hours rather than rounding an elapsed day", () => {
  const now = new Date(2026, 9, 17, 23, 0);
  expect(compactCommitTime(new Date(2026, 9, 15, 23, 30).toISOString(), now, "en-US")).toBe("2d");
  expect(compactCommitTime(new Date(2026, 9, 16, 23, 30).toISOString(), now, "en-US")).toBe("23h");
});
