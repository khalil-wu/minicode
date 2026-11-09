export interface UnifiedDiffLine {
  kind: "context" | "add" | "del" | "hunk" | "meta" | "marker";
  text: string;
}

/** Display/navigation identity belongs to the destination, or the removed
 * source for a deletion. Rename-only patches carry that identity in metadata. */
/** Git quotes a header name containing quotes, tabs, newlines or backslashes
 * (and non-ASCII bytes when core.quotePath is on); decode it to the real name. */
export function unquoteGitPath(token: string): string {
  const text = token.trim();
  if (text.length < 2 || !text.startsWith('"') || !text.endsWith('"')) return token;
  const inner = text.slice(1, -1);
  const bytes: number[] = [];
  const encoder = new TextEncoder();
  const simple: Record<string, number> = { n: 10, t: 9, r: 13, a: 7, b: 8, f: 12, v: 11, '"': 34, "\\": 92 };
  for (let index = 0; index < inner.length; index += 1) {
    const char = inner[index];
    if (char !== "\\") { bytes.push(...encoder.encode(char)); continue; }
    const octal = /^[0-7]{1,3}/.exec(inner.slice(index + 1, index + 4))?.[0];
    if (octal) { bytes.push(parseInt(octal, 8)); index += octal.length; continue; }
    const next = inner[index + 1] ?? "";
    bytes.push(simple[next] ?? (next ? next.charCodeAt(0) : 92));
    index += 1;
  }
  return new TextDecoder().decode(new Uint8Array(bytes));
}

const quotedToken = /^"(?:[^"\\]|\\.)*"/;
const headerPath = (raw: string, prefix: RegExp): string => {
  const trimmed = raw.trim();
  const token = trimmed.startsWith('"') ? quotedToken.exec(trimmed)?.[0] ?? trimmed : trimmed.split("\t", 1)[0];
  return unquoteGitPath(token).replace(prefix, "");
};

/** The `diff --git a/old b/new` header with Git-quoted names decoded. */
export function parseDiffGitHeader(line: string): { oldPath: string; newPath: string } | null {
  if (!line.startsWith("diff --git ")) return null;
  const rest = line.slice("diff --git ".length);
  let first: string;
  let remainder: string;
  if (rest.startsWith('"')) {
    const match = quotedToken.exec(rest);
    if (!match) return null;
    first = match[0];
    remainder = rest.slice(match[0].length).replace(/^ /, "");
  } else {
    const quotedAt = rest.indexOf(' "b/');
    const at = quotedAt >= 0 ? quotedAt : rest.lastIndexOf(" b/");
    if (at < 0) return null;
    first = rest.slice(0, at);
    remainder = rest.slice(at + 1);
  }
  const second = remainder.startsWith('"') ? quotedToken.exec(remainder)?.[0] : remainder;
  if (!second) return null;
  const oldPath = unquoteGitPath(first);
  const newPath = unquoteGitPath(second);
  if (!oldPath.startsWith("a/") || !newPath.startsWith("b/")) return null;
  return { oldPath: oldPath.slice(2), newPath: newPath.slice(2) };
}

export function unifiedDiffFilePaths(lines: { kind: string; text: string }[]): { oldPath: string; newPath: string } {
  let oldPath = "";
  let newPath = "";
  for (const line of lines) {
    if (line.kind !== "meta") continue;
    if (line.text.startsWith("--- ")) oldPath = headerPath(line.text.slice(4), /^a\//);
    else if (line.text.startsWith("+++ ")) newPath = headerPath(line.text.slice(4), /^b\//);
    else if (line.text.startsWith("rename from ")) oldPath = unquoteGitPath(line.text.slice(12));
    else if (line.text.startsWith("rename to ")) newPath = unquoteGitPath(line.text.slice(10));
  }
  return { oldPath, newPath };
}

export function extractFilePathFromDiff(lines: { kind: string; text: string }[]): string {
  const { oldPath, newPath } = unifiedDiffFilePaths(lines);
  return newPath && newPath !== "/dev/null" ? newPath : oldPath === "/dev/null" ? "" : oldPath;
}

export function parseUnifiedDiffLines(patch: string): UnifiedDiffLine[] {
  if (!patch) return [];
  const lines = patch.split(/\r?\n/);
  const hasHunks = lines.some((line) => line.startsWith("@@"));
  let inHunk = false;
  let counted = false;
  let oldRemaining = 0;
  let newRemaining = 0;
  return lines.map((text): UnifiedDiffLine => {
    if (text.startsWith("diff --git ") || text.startsWith("Index: ")) {
      inHunk = false;
      return { kind: "meta", text };
    }
    if (text.startsWith("@@")) {
      inHunk = true;
      const header = /^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@/.exec(text);
      counted = header !== null;
      oldRemaining = header ? Number(header[1] ?? 1) : 0;
      newRemaining = header ? Number(header[2] ?? 1) : 0;
      return { kind: "hunk", text };
    }
    if (text.startsWith("\\ No newline at end of file")) return { kind: "marker", text };
    // Once the hunk's declared line counts are spent the hunk has ended, so the
    // next file's ---/+++ headers in a concatenated patch are metadata rather
    // than removed and added lines.
    if (inHunk && counted && oldRemaining <= 0 && newRemaining <= 0) inHunk = false;
    if (!inHunk && /^(?:---|\+\+\+|index |new file mode|deleted file mode|old mode|new mode|similarity index|rename (?:from|to)|Binary files |GIT binary patch)/.test(text)) {
      return { kind: "meta", text };
    }
    if (inHunk || !hasHunks) {
      if (text.startsWith("+")) { newRemaining -= 1; return { kind: "add", text }; }
      if (text.startsWith("-")) { oldRemaining -= 1; return { kind: "del", text }; }
      if (inHunk && text.startsWith(" ")) { oldRemaining -= 1; newRemaining -= 1; }
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
