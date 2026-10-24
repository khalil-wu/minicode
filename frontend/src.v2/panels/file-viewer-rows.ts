import { parsePatch } from "diff";

export interface FileViewerRow { text: string; kind: "context" | "add" | "del"; oldLine?: number; newLine?: number }

/** Expand the selected file's patch against its complete current contents. */
export function fileViewerRows(content: string, patch: string, showChanges: boolean): FileViewerRow[] {
  const lines = content.split(/\r?\n/);
  if (lines.at(-1) === "") lines.pop();
  if (!showChanges || !patch) return lines.map((text, index) => ({ text, kind: "context", oldLine: index + 1, newLine: index + 1 }));
  const hunks = parsePatch(patch)[0]?.hunks ?? [];
  const rows: FileViewerRow[] = [];
  let current = 1, previous = 1;
  for (const hunk of hunks) {
    while (current < hunk.newStart) rows.push({ text: lines[current++ - 1], kind: "context", oldLine: previous++, newLine: current - 1 });
    previous = hunk.oldStart;
    current = Math.max(1, hunk.newStart);
    for (const line of hunk.lines) {
      if (line[0] === "-") rows.push({ text: line.slice(1), kind: "del", oldLine: previous++ });
      else if (line[0] === "+") rows.push({ text: line.slice(1), kind: "add", newLine: current++ });
      else if (line[0] === " ") rows.push({ text: line.slice(1), kind: "context", oldLine: previous++, newLine: current++ });
    }
  }
  while (current <= lines.length) rows.push({ text: lines[current++ - 1], kind: "context", oldLine: previous++, newLine: current - 1 });
  return rows;
}
