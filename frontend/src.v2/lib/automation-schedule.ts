import { RRule, RRuleSet, rrulestr, type Weekday } from "rrule";

export const AUTOMATION_WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"] as const;
export type AutomationWeekday = typeof AUTOMATION_WEEKDAYS[number];
export type AutomationScheduleMode = "hourly" | "daily" | "weekdays" | "weekly" | "custom";
export type CustomFrequency = "hourly" | "daily" | "weekly" | "monthly" | "yearly";
export interface AutomationScheduleDraft {
  mode: AutomationScheduleMode;
  intervalMinutes: string;
  time: string;
  weekdays: AutomationWeekday[];
  customRrule: string;
}
export interface CustomScheduleDraft {
  frequency: CustomFrequency;
  interval: string;
  time: string;
  weekdays: AutomationWeekday[];
  month: number;
  monthDays: number[];
  minute: string;
}

/** The same RFC parser used by Source, at the user-edited schedule boundary. */
export function parseAutomationRRule(value: string): RRuleSet | null {
  try {
    const rules = rrulestr(value.replace(/TZID="([^"\r\n]+)"/gi, "TZID=$1"), { forceset: true }) as RRuleSet;
    return rules.rrules().length ? rules : null;
  } catch { return null; }
}
const single = (value: number | number[] | null | undefined): number | null =>
  typeof value === "number" ? value : value?.length === 1 ? value[0] : null;
const weekdaysOf = (value: number | Weekday | string | (number | Weekday | string)[] | null | undefined): AutomationWeekday[] => {
  const values = value == null ? [] : Array.isArray(value) ? value : [value];
  return AUTOMATION_WEEKDAYS.filter((code, index) => values.some(day => typeof day === "string" ? day === code : (typeof day === "number" ? day : day.weekday) === index));
};
const plainWeekdays = (value: RRule["origOptions"]["byweekday"]) =>
  (value == null ? [] : Array.isArray(value) ? value : [value]).every(day => typeof day !== "object" || day.n == null);
const timeOf = (hour: number, minute: number) => `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
const timeParts = (time: string): [number, number] | null => {
  const match = /^(\d{2}):(\d{2})$/.exec(time);
  return match && Number(match[1]) < 24 && Number(match[2]) < 60 ? [Number(match[1]), Number(match[2])] : null;
};
const ruleDays = (days: AutomationWeekday[]) => days.map(day => RRule[day]);
const whole = (value: string, minimum = 1, maximum = 999): number | null => {
  const number = Number(value);
  return value.trim() && Number.isInteger(number) && number >= minimum && number <= maximum ? number : null;
};

export function readAutomationSchedule(value?: string): AutomationScheduleDraft {
  const initial: AutomationScheduleDraft = { mode: "hourly", intervalMinutes: "30", time: "09:00", weekdays: [...AUTOMATION_WEEKDAYS], customRrule: "" };
  if (!value) return initial;
  const cron = value.trim().split(/\s+/);
  if (cron.length === 5) {
    const [minute, hour, day, month, weekday] = cron;
    if (minute === "0" && hour === "*" && day === "*" && month === "*" && weekday === "*") return { ...initial, intervalMinutes: "60" };
    if (/^\d+$/.test(minute) && /^\d+$/.test(hour) && Number(minute) < 60 && Number(hour) < 24) {
      if (day === "*" && month === "*" && ["*", "1-5", "0", "1", "2", "3", "4", "5", "6", "7"].includes(weekday)) {
        return { ...initial, mode: weekday === "*" ? "daily" : weekday === "1-5" ? "weekdays" : "weekly", time: timeOf(Number(hour), Number(minute)),
          weekdays: weekday === "*" ? [...AUTOMATION_WEEKDAYS] : weekday === "1-5" ? AUTOMATION_WEEKDAYS.slice(0, 5) : [AUTOMATION_WEEKDAYS[(Number(weekday) + 6) % 7]] };
      }
      if (weekday === "*" && /^\d+$/.test(day) && /^\d+$/.test(month) && Number(day) >= 1 && Number(day) <= 31 && Number(month) >= 1 && Number(month) <= 12) {
        return { ...initial, mode: "custom", customRrule: new RRule({ freq: RRule.YEARLY, bymonth: Number(month), bymonthday: Number(day), byhour: Number(hour), byminute: Number(minute) }).toString() };
      }
    }
    return { ...initial, mode: "custom" };
  }
  const set = parseAutomationRRule(value);
  const rule = set?.rrules()[0];
  const custom = { ...initial, mode: "custom" as const, customRrule: value };
  if (!set || !rule || set.rrules().length !== 1 || set.rdates().length || set.exdates().length || set.exrules().length) return custom;
  const options = rule.origOptions;
  const keys = Object.entries(options).filter(([, entry]) => entry != null).map(([key]) => key);
  const allowed = new Set(["freq", "interval", "byhour", "byminute", "byweekday"]);
  if (keys.some(key => !allowed.has(key)) || !plainWeekdays(options.byweekday)) return custom;
  const interval = options.interval ?? 1;
  const days = weekdaysOf(options.byweekday);
  if (options.freq === RRule.MINUTELY && keys.every(key => key === "freq" || key === "interval")) return { ...initial, intervalMinutes: String(interval) };
  if (options.freq === RRule.HOURLY && options.byhour == null && single(options.byminute) === 0 && (!days.length || days.length === 7)) return { ...initial, intervalMinutes: String(interval * 60) };
  const hour = single(options.byhour), minute = single(options.byminute);
  if (interval !== 1 || hour == null || minute == null || ![RRule.WEEKLY, RRule.DAILY].includes(options.freq!)) return custom;
  const selectedDays = days.length ? days : [...AUTOMATION_WEEKDAYS];
  const mode = selectedDays.length === 7 ? "daily" : selectedDays.join() === "MO,TU,WE,TH,FR" ? "weekdays" : selectedDays.length === 1 ? "weekly" : "custom";
  return mode === "custom" ? custom : { ...initial, mode, time: timeOf(hour, minute), weekdays: selectedDays };
}

export function buildAutomationSchedule(draft: AutomationScheduleDraft): string | null {
  if (draft.mode === "custom") return parseAutomationRRule(draft.customRrule) ? draft.customRrule : null;
  if (draft.mode === "hourly") {
    const interval = whole(draft.intervalMinutes);
    return interval == null ? null : new RRule({ freq: RRule.MINUTELY, interval }).toString();
  }
  const time = timeParts(draft.time);
  const days = draft.mode === "daily" ? [...AUTOMATION_WEEKDAYS] : draft.mode === "weekdays" ? AUTOMATION_WEEKDAYS.slice(0, 5) : draft.weekdays;
  return time && days.length ? new RRule({ freq: RRule.WEEKLY, byhour: time[0], byminute: time[1], byweekday: ruleDays(days) }).toString() : null;
}

export function readCustomSchedule(value: string): CustomScheduleDraft | null {
  const set = parseAutomationRRule(value), rule = set?.rrules()[0];
  if (!set || !rule || set.rrules().length !== 1 || set.rdates().length || set.exdates().length || set.exrules().length) return null;
  const options = rule.origOptions;
  const frequencies: Partial<Record<number, CustomFrequency>> = { [RRule.HOURLY]: "hourly", [RRule.DAILY]: "daily", [RRule.WEEKLY]: "weekly", [RRule.MONTHLY]: "monthly", [RRule.YEARLY]: "yearly" };
  const frequency = frequencies[options.freq!], hour = single(options.byhour), minute = single(options.byminute);
  const allowed = new Set(["freq", "interval", "byhour", "byminute", ...(frequency === "weekly" || frequency === "hourly" ? ["byweekday"] : []),
    ...(frequency === "monthly" || frequency === "yearly" ? ["bymonthday"] : []), ...(frequency === "yearly" ? ["bymonth"] : [])]);
  if (Object.entries(options).some(([key, entry]) => entry != null && !allowed.has(key)) || !plainWeekdays(options.byweekday)
    || frequency === "hourly" && (options.byhour != null || options.byweekday != null && weekdaysOf(options.byweekday).length !== 7)
    || frequency === "yearly" && single(options.bymonth) == null) return null;
  const rawMonthDays = options.bymonthday == null ? [1] : Array.isArray(options.bymonthday) ? options.bymonthday : [options.bymonthday];
  if (!frequency || minute == null || frequency !== "hourly" && hour == null || rawMonthDays.some(day => day < 1 || day > 31)) return null;
  return { frequency, interval: String(options.interval ?? 1), time: timeOf(hour ?? 9, minute), minute: String(minute),
    weekdays: weekdaysOf(options.byweekday), month: single(options.bymonth) ?? 1, monthDays: rawMonthDays };
}

export function buildCustomSchedule(draft: CustomScheduleDraft): string | null {
  const interval = whole(draft.interval);
  if (interval == null) return null;
  if (draft.frequency === "hourly") {
    const minute = whole(draft.minute, 0, 59);
    return minute == null ? null : new RRule({ freq: RRule.HOURLY, interval, byminute: minute }).toString();
  }
  const time = timeParts(draft.time);
  if (!time || draft.frequency === "weekly" && !draft.weekdays.length || ["monthly", "yearly"].includes(draft.frequency) && !draft.monthDays.length) return null;
  return new RRule({ freq: RRule[draft.frequency.toUpperCase() as "DAILY" | "WEEKLY" | "MONTHLY" | "YEARLY"], interval, byhour: time[0], byminute: time[1],
    ...(draft.frequency === "weekly" ? { byweekday: ruleDays(draft.weekdays) } : {}),
    ...(["monthly", "yearly"].includes(draft.frequency) ? { bymonthday: draft.monthDays } : {}),
    ...(draft.frequency === "yearly" ? { bymonth: draft.month } : {}) }).toString();
}
