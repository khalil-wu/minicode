/** Source _Dc/TDc/DDc: compact age uses calendar days after the first 24 hours. */
export function compactCommitTime(dateString: string, now: Date, locale: "zh-CN" | "en-US"): string {
  const date = new Date(dateString);
  const elapsed = now.getTime() - date.getTime();
  if (elapsed < 60_000) return locale === "zh-CN" ? "刚刚" : "just now";
  const minutes = Math.max(1, Math.floor(elapsed / 60_000));
  const hours = Math.floor(minutes / 60);
  const day = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const previousDay = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const days = Math.max(1, Math.round((day.getTime() - previousDay.getTime()) / 86_400_000));
  const [value, unit] = minutes < 60 ? [minutes, 0] : hours < 24 ? [hours, 1]
    : days < 7 ? [days, 2] : days < 30 ? [Math.floor(days / 7), 3]
      : days < 365 ? [Math.floor(days / 30), 4] : [Math.floor(days / 365), 5];
  return locale === "zh-CN" ? `${value} ${["分", "小时", "天", "周", "个月", "年"][unit]}`
    : `${value}${["m", "h", "d", "w", "mo", "y"][unit]}`;
}
