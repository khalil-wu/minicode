import { useEffect, useState } from "react";
import type { InputHTMLAttributes } from "react";

/** Keep partially typed numbers editable; publish only values in the control's range. */
export function NumberInput({ value, onCommit, ...props }: Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange"> & {
  value: number; onCommit: (value: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  return <input {...props} type="number" value={draft} onChange={(event) => {
    setDraft(event.target.value);
    if (event.currentTarget.value !== "" && event.currentTarget.validity.valid) onCommit(event.currentTarget.valueAsNumber);
  }} onBlur={(event) => { setDraft(String(value)); props.onBlur?.(event); }} />;
}
