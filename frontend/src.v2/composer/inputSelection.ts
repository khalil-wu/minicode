export interface ComposerSelection { start: number; end: number; composing?: boolean; }
export interface ComposerToken { kind: "mention" | "skill"; value: string; start: number; end: number; }

/** Only the token immediately before a collapsed caret belongs to a picker. */
export function composerTokenAtSelection(value: string, selection: ComposerSelection): ComposerToken | null {
  if (selection.composing || selection.start !== selection.end) return null;
  const before = value.slice(0, selection.start);
  const line = before.slice(before.lastIndexOf("\n") + 1);
  const match = line.match(/(?:^|\s)(@[^\s@]*|\$[A-Za-z0-9_.:/\\-]*)$/u);
  if (!match) return null;
  const token = match[1];
  return { kind: token.startsWith("@") ? "mention" : "skill", value: token, start: selection.start - token.length, end: selection.end };
}

export const removeComposerToken = (value: string, token: ComposerToken): string => value.slice(0, token.start) + value.slice(token.end);

export function appendComposerTokenAnchor(path: string, token: string): string {
  if (path.includes("#")) return path;
  const anchor = token.match(/#L?(\d+)(?:-L?(\d+))?$/i);
  return anchor ? `${path}#${anchor[1]}${anchor[2] ? `-${anchor[2]}` : ""}` : path;
}
