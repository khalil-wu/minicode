import { describe, expect, it } from "vitest";
import { RRule } from "rrule";
import { AUTOMATION_WEEKDAYS, buildAutomationSchedule, buildCustomSchedule, parseAutomationRRule, readAutomationSchedule, readCustomSchedule } from "./automation-schedule";

describe("automation schedules", () => {
  it("builds the local interval and weekday frequency contracts without a Cron conversion", () => {
    const initial = readAutomationSchedule();
    expect(parseAutomationRRule(buildAutomationSchedule(initial)!)!.rrules()[0].origOptions).toMatchObject({ freq: RRule.MINUTELY, interval: 30 });
    const weekdays = buildAutomationSchedule({ ...initial, mode: "weekdays", time: "08:20" })!;
    expect(parseAutomationRRule(weekdays)!.rrules()[0].origOptions).toMatchObject({ freq: RRule.WEEKLY, byhour: 8, byminute: 20 });
    expect(readAutomationSchedule(weekdays)).toMatchObject({ mode: "weekdays", time: "08:20", weekdays: AUTOMATION_WEEKDAYS.slice(0, 5) });
    expect(buildAutomationSchedule({ ...initial, intervalMinutes: "" })).toBeNull();
    expect(buildAutomationSchedule({ ...initial, intervalMinutes: "1.5" })).toBeNull();
    expect(buildAutomationSchedule({ ...initial, intervalMinutes: "1000" })).toBeNull();
  });

  it("recognizes legacy Cron for presentation without mutating the wire schedule", () => {
    expect(readAutomationSchedule("0 * * * *")).toMatchObject({ mode: "hourly", intervalMinutes: "60" });
    expect(readAutomationSchedule("20 8 * * 1-5")).toMatchObject({ mode: "weekdays", time: "08:20" });
    expect(readAutomationSchedule("10 9 * * 0")).toMatchObject({ mode: "weekly", weekdays: ["SU"] });
    const annual = readAutomationSchedule("10 9 1 2 *");
    expect(readCustomSchedule(annual.customRrule)).toMatchObject({ frequency: "yearly", month: 2, monthDays: [1], time: "09:10" });
    expect(readAutomationSchedule("*/7 4-9 * 2,8 1,3")).toMatchObject({ mode: "custom", customRrule: "" });
  });

  it("validates and preserves complete RFC rules with dates, zones and exclusions", () => {
    const raw = "DTSTART;TZID=America/New_York:20261101T013000\nRRULE:FREQ=DAILY;COUNT=3\nEXDATE;TZID=America/New_York:20261102T013000";
    expect(parseAutomationRRule(raw)).not.toBeNull();
    expect(readAutomationSchedule(raw)).toMatchObject({ mode: "custom", customRrule: raw });
    expect(readCustomSchedule(raw)).toBeNull();
    expect(buildAutomationSchedule(readAutomationSchedule(raw))).toBe(raw);
    const mixed = "DTSTART:20270101T000000Z\nRRULE:FREQ=YEARLY;COUNT=3\nRDATE:20270301T000000Z\nEXDATE:20280101T000000Z";
    const parsed = parseAutomationRRule(mixed)!;
    expect(parsed.rrules()[0].origOptions.count).toBe(3);
    expect(parsed.rdates()).toHaveLength(1);
    expect(parsed.exdates()).toHaveLength(1);
    expect(readCustomSchedule(mixed)).toBeNull();
    expect(parseAutomationRRule("RRULE:FREQ=UNKNOWN")).toBeNull();
    expect(parseAutomationRRule("")).toBeNull();
  });

  it("uses structured custom controls only for schedules they can represent completely", () => {
    const simple = "RRULE:FREQ=YEARLY;INTERVAL=2;BYMONTH=2;BYMONTHDAY=1,14;BYHOUR=9;BYMINUTE=15";
    const draft = readCustomSchedule(simple)!;
    expect(draft).toMatchObject({ frequency: "yearly", interval: "2", month: 2, monthDays: [1, 14], time: "09:15" });
    expect(parseAutomationRRule(buildCustomSchedule({ ...draft, month: 3 })!)!.rrules()[0].origOptions.bymonth).toBe(3);
    expect(readCustomSchedule("RRULE:FREQ=HOURLY;BYHOUR=9;BYMINUTE=15")).toBeNull();
    expect(readCustomSchedule("RRULE:FREQ=HOURLY;BYDAY=MO;BYMINUTE=15")).toBeNull();
    expect(readAutomationSchedule("RRULE:FREQ=HOURLY;BYHOUR=9;BYMINUTE=0").mode).toBe("custom");
    expect(readCustomSchedule("RRULE:FREQ=MONTHLY;BYDAY=1MO;BYHOUR=9;BYMINUTE=0")).toBeNull();
    expect(readCustomSchedule("RRULE:FREQ=DAILY;UNTIL=20270101T000000Z;BYHOUR=9;BYMINUTE=0")).toBeNull();
    expect(buildCustomSchedule({ ...draft, monthDays: [] })).toBeNull();
  });
});
