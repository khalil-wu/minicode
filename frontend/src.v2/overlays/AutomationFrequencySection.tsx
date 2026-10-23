import { useId, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useI18n, type Translate } from "../i18n";
import { SelectMenu } from "../components/SelectMenu";
import { useFocusTrap } from "../hooks/useFocusTrap";
import {
  AUTOMATION_WEEKDAYS, parseAutomationRRule,
  type AutomationScheduleDraft, type AutomationScheduleMode, type AutomationWeekday,
  type CustomFrequency, type CustomScheduleDraft,
} from "../lib/automation-schedule";

const weekdayLabels = (tr: Translate) => [tr("星期一"), tr("星期二"), tr("星期三"), tr("星期四"), tr("星期五"), tr("星期六"), tr("星期日")];
const frequencyOptions = (tr: Translate) => [
  ["hourly", tr("每小时")], ["daily", tr("每天")], ["weekly", tr("每周")],
  ["monthly", tr("每月")], ["yearly", tr("每年")],
];

function TimePicker({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const { t: tr } = useI18n();
  const times = Array.from({ length: 96 }, (_, index) => `${String(Math.floor(index / 4)).padStart(2, "0")}:${String(index % 4 * 15).padStart(2, "0")}`);
  if (value && !times.includes(value)) times.push(value);
  return <SelectMenu className="automation-select" value={value} onValueChange={onChange} ariaLabel={tr("运行时间")} align="end">
    {times.sort().map(time => <option key={time} value={time}>{time}</option>)}
  </SelectMenu>;
}

function WeekdayPicker({ value, onChange }: { value: AutomationWeekday[]; onChange: (days: AutomationWeekday[]) => void }) {
  const { t: tr, locale } = useI18n();
  const labels = weekdayLabels(tr);
  return <div className="automation-weekdays" role="group" aria-label={tr("重复日期")}>
    {AUTOMATION_WEEKDAYS.map((day, index) => <button key={day} type="button" aria-label={labels[index]} aria-pressed={value.includes(day)}
      onClick={() => onChange(AUTOMATION_WEEKDAYS.filter(item => item === day ? !value.includes(item) : value.includes(item)))}>{locale === "zh-CN" ? labels[index].slice(-1) : labels[index].slice(0, 2)}</button>)}
  </div>;
}

export function AutomationFrequencySection({ draft, custom, onChange, onCustomChange, onRuleSave, afterScheduleRows }: {
  draft: AutomationScheduleDraft;
  custom: CustomScheduleDraft | null;
  onChange: (draft: AutomationScheduleDraft) => void;
  onCustomChange: (draft: CustomScheduleDraft) => void;
  onRuleSave: (value: string) => void;
  afterScheduleRows?: ReactNode;
}) {
  const { t: tr } = useI18n();
  const [unit, setUnit] = useState<"minutes" | "hours">(() => Number(draft.intervalMinutes) % 60 === 0 ? "hours" : "minutes");
  const [ruleOpen, setRuleOpen] = useState(false);
  const labels = weekdayLabels(tr);
  const monthLabels = [tr("一月"), tr("二月"), tr("三月"), tr("四月"), tr("五月"), tr("六月"), tr("七月"), tr("八月"), tr("九月"), tr("十月"), tr("十一月"), tr("十二月")];
  const update = (patch: Partial<AutomationScheduleDraft>) => onChange({ ...draft, ...patch });
  const updateCustom = (patch: Partial<CustomScheduleDraft>) => onCustomChange({ ...custom!, ...patch });
  return <section className="automation-section">
    <h3>{tr("频率")}</h3>
    <div className="automation-detail-row"><span>{tr("重复")}</span>
      <SelectMenu className="automation-select" value={draft.mode} onValueChange={mode => update({ mode: mode as AutomationScheduleMode })} ariaLabel={tr("重复计划")} align="end">
        <option value="hourly">{tr("间隔")}</option><option value="daily">{tr("每天")}</option><option value="weekdays">{tr("工作日")}</option>
        <option value="weekly">{tr("每周")}</option><option value="custom">{tr("自定义")}</option>
      </SelectMenu>
    </div>
    {draft.mode === "hourly" && <div className="automation-detail-row"><span>{tr("每隔")}</span><div className="automation-interval">
      <input type="number" min={unit === "hours" ? 1 / 60 : 1} max={unit === "hours" ? 999 / 60 : 999} step="any" aria-label={tr("重复间隔")}
        value={unit === "hours" && draft.intervalMinutes !== "" ? String(Number(draft.intervalMinutes) / 60) : draft.intervalMinutes}
        onChange={event => update({ intervalMinutes: event.target.value === "" ? "" : String(Number(event.target.value) * (unit === "hours" ? 60 : 1)) })} />
      <SelectMenu className="automation-select" value={unit} onValueChange={value => setUnit(value as "minutes" | "hours")} ariaLabel={tr("间隔单位")} align="end">
        <option value="minutes">{tr("分钟")}</option><option value="hours">{tr("小时")}</option>
      </SelectMenu>
    </div></div>}
    {draft.mode === "weekly" && <div className="automation-detail-row"><span>{tr("日期")}</span>
      <SelectMenu className="automation-select" value={draft.weekdays[0]} onValueChange={day => update({ weekdays: [day as AutomationWeekday] })} ariaLabel={tr("重复日期")} align="end">
        {AUTOMATION_WEEKDAYS.map((day, index) => <option key={day} value={day}>{labels[index]}</option>)}
      </SelectMenu>
    </div>}
    {["daily", "weekdays", "weekly"].includes(draft.mode) && <div className="automation-detail-row"><span>{tr("时间")}</span><TimePicker value={draft.time} onChange={time => update({ time })} /></div>}
    {draft.mode === "custom" && <>
      {custom && <>
        <div className="automation-detail-row"><span>{tr("重复单位")}</span>
          <SelectMenu className="automation-select" value={custom.frequency} onValueChange={frequency => updateCustom({ frequency: frequency as CustomFrequency })} ariaLabel={tr("自定义重复单位")} align="end">
            {frequencyOptions(tr).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </SelectMenu>
        </div>
        <div className="automation-detail-row"><span>{tr("每隔")}</span><input className="automation-number" type="number" min="1" max="999" step="1" value={custom.interval}
          aria-label={tr("自定义重复间隔")} onChange={event => updateCustom({ interval: event.target.value })} /></div>
        {custom.frequency === "weekly" && <div className="automation-detail-row automation-detail-row-wrap"><span>{tr("日期")}</span><WeekdayPicker value={custom.weekdays} onChange={weekdays => updateCustom({ weekdays })} /></div>}
        {custom.frequency === "yearly" && <div className="automation-detail-row"><span>{tr("月份")}</span>
          <SelectMenu className="automation-select" value={String(custom.month)} onValueChange={month => updateCustom({ month: Number(month) })} ariaLabel={tr("月份")} align="end">
            {monthLabels.map((label, index) => <option key={index} value={index + 1}>{label}</option>)}
          </SelectMenu>
        </div>}
        {["monthly", "yearly"].includes(custom.frequency) && <div className="automation-month-days" role="group" aria-label={tr("月中日期")}>
          {Array.from({ length: 31 }, (_, index) => index + 1).map(day => <button key={day} type="button" aria-label={tr("每月 {day} 日", { day })} aria-pressed={custom.monthDays.includes(day)}
            onClick={() => updateCustom({ monthDays: custom.monthDays.includes(day) ? custom.monthDays.filter(value => value !== day) : [...custom.monthDays, day].sort((a, b) => a - b) })}>{day}</button>)}
        </div>}
        {custom.frequency === "hourly" ? <div className="automation-detail-row"><span>{tr("整点后的分钟")}</span><input className="automation-number" type="number" min="0" max="59" step="1"
          value={custom.minute} aria-label={tr("整点后的分钟")} onChange={event => updateCustom({ minute: event.target.value })} /></div>
          : <div className="automation-detail-row"><span>{tr("开始时间")}</span><TimePicker value={custom.time} onChange={time => updateCustom({ time })} /></div>}
      </>}
      <div className="automation-rule-row"><button type="button" className="automation-rule-button" onClick={() => setRuleOpen(true)} aria-label={tr("编辑计划规则")}>
        <span className="automation-source-icon" aria-hidden="true" style={{ maskImage: `url(${import.meta.env.BASE_URL}apps/automation-edit-rule.svg)` }} />{tr("编辑计划规则")}
      </button></div>
    </>}
    {afterScheduleRows}
    {ruleOpen && <AutomationRuleDialog initialValue={draft.customRrule} onClose={() => setRuleOpen(false)} onSave={value => { onRuleSave(value); setRuleOpen(false); }} />}
  </section>;
}

function AutomationRuleDialog({ initialValue, onClose, onSave }: { initialValue: string; onClose: () => void; onSave: (value: string) => void }) {
  const { t: tr } = useI18n();
  const id = useId();
  const ref = useFocusTrap(true);
  const [value, setValue] = useState(initialValue);
  const valid = Boolean(parseAutomationRRule(value));
  return createPortal(<div className="mc-dialog-backdrop automation-dialog-backdrop" role="presentation" onClick={onClose}>
    <div ref={ref} className="mc-dialog-panel automation-dialog" role="dialog" aria-modal="true" aria-labelledby={id} tabIndex={-1}
      onClick={event => event.stopPropagation()} onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); } }}>
      <h2 id={id}>{tr("编辑计划规则")}</h2>
      <textarea aria-label="RRULE" className="automation-rrule" value={value} onChange={event => setValue(event.target.value)} rows={5}
        placeholder="RRULE:FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=9;BYMINUTE=0" aria-invalid={!valid} aria-describedby={!valid ? `${id}-error` : undefined} />
      {!valid && <p id={`${id}-error`} className="automation-field-error" role="alert">{tr("请输入有效的 RRULE")}</p>}
      <div className="automation-dialog-actions"><button type="button" onClick={onClose}>{tr("取消")}</button><button type="button" className="automation-primary-button" disabled={!valid} onClick={() => onSave(value)}>{tr("保存")}</button></div>
    </div>
  </div>, document.body);
}
