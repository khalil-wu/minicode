import type { DiffLine } from "../../lib/diff-comment-lines";
import type { FullDiffContext, FullDiffLine } from "./full-content-metadata";

export type ContextExpansion = Record<string, { fromStart: number; fromEnd: number }>;
export interface DiffContextGap { key: string; remaining: number; allowUp: boolean; allowDown: boolean }
export type ContextDirection = "up" | "down" | "both" | "all";

/** Source expandHunk: 20 from each selected edge; Shift/Expand all uses Infinity. */
export function expandDiffContext(state: ContextExpansion, key: string, direction: ContextDirection): ContextExpansion {
  const previous = state[key] ?? { fromStart: 0, fromEnd: 0 };
  const amount = direction === "all" ? Infinity : 20;
  return { ...state, [key]: {
    fromStart: previous.fromStart + (direction === "down" ? 0 : amount),
    fromEnd: previous.fromEnd + (direction === "up" ? 0 : amount),
  } };
}

export function displayFullDiffContext(context: FullDiffContext, expansion: ContextExpansion): {
  lines: FullDiffLine[]; gaps: Map<DiffLine, DiffContextGap>;
} {
  if (context.isPartial) return { lines: context.lines, gaps: new Map() };
  const lines: FullDiffLine[] = [], gaps = new Map<DiffLine, DiffContextGap>();
  const appendGap = (key: string, contents: FullDiffLine[], header: FullDiffLine, allowUp: boolean, allowDown: boolean) => {
    const expanded = expansion[key] ?? { fromStart: 0, fromEnd: 0 };
    const fromStart = Math.min(expanded.fromStart, contents.length);
    const fromEnd = Math.min(expanded.fromEnd, contents.length - fromStart);
    const remaining = contents.length - fromStart - fromEnd;
    lines.push(...contents.slice(0, fromStart));
    const divider = { ...header, oldGap: remaining, newGap: remaining };
    lines.push(divider);
    gaps.set(divider, { key, remaining, allowUp, allowDown });
    if (fromEnd > 0) lines.push(...contents.slice(contents.length - fromEnd));
  };
  for (let index = 0; index < context.hunks.length; index++) {
    const hunk = context.hunks[index];
    appendGap(`before:${hunk.index}`, hunk.before, hunk.header, index > 0, true);
    lines.push(...hunk.body);
  }
  if (context.trailing.length > 0) appendGap("trailing", context.trailing, { kind: "hunk", text: "" }, true, false);
  return { lines, gaps };
}
