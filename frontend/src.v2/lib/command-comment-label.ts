/**
 * Extract a human-readable label from a multi-line shell command's leading
 * `# comment` line. The model often writes the first line as a comment meant
 * for the human to read (e.g. `# install deps\nnpm install`); surface that as
 * the tool label instead of the raw command. Ported from cc's commentLabel.ts.
 *
 * Returns undefined when there is no usable leading comment (including shebang
 * `#!` lines, which are not human notes).
 */
export function extractCommandCommentLabel(command: string | undefined): string | undefined {
  if (!command) return undefined;
  const newlineIndex = command.indexOf("\n");
  const firstLine = (newlineIndex === -1 ? command : command.slice(0, newlineIndex)).trim();
  if (!firstLine.startsWith("#") || firstLine.startsWith("#!")) return undefined;
  return firstLine.replace(/^#+\s*/, "") || undefined;
}
