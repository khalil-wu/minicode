export interface UnifiedDiffLine {
  kind: "context" | "add" | "del" | "hunk" | "meta" | "marker";
  text: string;
}

export function parseUnifiedDiffLines(patch: string): UnifiedDiffLine[] {
  if (!patch) return [];
  const lines = patch.split(/\r?\n/);
  const hasHunks = lines.some((line) => line.startsWith("@@"));
  let inHunk = false;
  return lines.map((text): UnifiedDiffLine => {
    if (text.startsWith("diff --git ") || text.startsWith("Index: ")) {
      inHunk = false;
      return { kind: "meta", text };
    }
    if (text.startsWith("@@")) {
      inHunk = true;
      return { kind: "hunk", text };
    }
    if (text.startsWith("\\ No newline at end of file")) return { kind: "marker", text };
    if (!inHunk && /^(?:---|\+\+\+|index |new file mode|deleted file mode|old mode|new mode|similarity index|rename (?:from|to)|Binary files |GIT binary patch)/.test(text)) {
      return { kind: "meta", text };
    }
    if (inHunk || !hasHunks) {
      if (text.startsWith("+")) return { kind: "add", text };
      if (text.startsWith("-")) return { kind: "del", text };
    }
    return { kind: text ? "context" : "meta", text };
  });
}

export function countUnifiedDiffLines(patch: string): { plus: number; minus: number } {
  let plus = 0;
  let minus = 0;
  for (const line of parseUnifiedDiffLines(patch)) {
    if (line.kind === "add") plus += 1;
    else if (line.kind === "del") minus += 1;
  }
  return { plus, minus };
}
