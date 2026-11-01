export type DiffSide = "left" | "right";

export interface DiffCommentPosition {
  path: string;
  side: DiffSide;
  line: number;
  start_line?: number;
  start_side?: DiffSide;
}

export interface DiffComment {
  type: "comment";
  content: Array<{ content_type: "text"; text: string }>;
  position: DiffCommentPosition;
  localDiffHunk?: string;
  localWorkspaceRoot?: string;
}

/** Unsaved line text belongs to its mounted Diff reader, not durable comments. */
export interface DiffCommentDraft {
  key: string;
  ownerId: string;
  position: DiffCommentPosition;
  text: string;
  localDiffHunk?: string;
  localWorkspaceRoot?: string;
}

export const diffCommentText = (comment: DiffComment): string =>
  comment.content.map(block => block.text).join("");

export function formatDiffCommentLines(position: DiffCommentPosition): string {
  const startLine = position.start_line ?? position.line;
  const startSide = position.start_side ?? position.side;
  if (startSide !== position.side) {
    return `${startSide === "left" ? "L" : "R"}${startLine}-${position.side === "left" ? "L" : "R"}${position.line}`;
  }
  return startLine === position.line ? `${position.line}` : `${Math.min(startLine, position.line)}-${Math.max(startLine, position.line)}`;
}

export function diffCommentSideLabel(comment: DiffComment): string {
  const start = comment.position.start_side ?? comment.position.side;
  const end = comment.position.side;
  return start === end ? (end === "left" ? "L" : "R") : `${start === "left" ? "L" : "R"}-${end === "left" ? "L" : "R"}`;
}

export const formatDiffCommentLocation = (comment: DiffComment): string =>
  `${comment.position.path}:${formatDiffCommentLines(comment.position)}`;

/** Windows path aliases share identity; WSL file case is retained. */
export function commentWorkspaceKey(value: string): string {
  const slashed = value.replace(/^\\\\\?\\UNC\\(.*)$/i, "//$1")
    .replace(/^\\\\\?\\([a-z]:[\\/].*)$/i, "$1").replace(/\\/g, "/");
  const windows = /^\/[a-z]:\//i.test(slashed) ? slashed.slice(1) : slashed;
  const wsl = slashed.match(/^\/\/(?:wsl\$|wsl\.localhost)\/([^/]+)(?:\/(.*))?$/i);
  let key = slashed;
  if (wsl) {
    const path = `/${wsl[2] ?? ""}`;
    key = /^\/mnt\/[a-z](?:\/|$)/i.test(path)
      ? `win32:${path.toLowerCase()}` : `wsl:${wsl[1].toLowerCase()}:${path}`;
  } else if (/^[a-z]:\//i.test(windows) || /^\/\/[^/]+\/[^/]+/.test(windows)) {
    const drive = windows.match(/^([a-z]):(?:\/(.*))?$/i);
    key = drive ? `win32:/mnt/${drive[1].toLowerCase()}${drive[2] ? `/${drive[2].toLowerCase()}` : ""}` : `win32:${windows.toLowerCase()}`;
  }
  return key === "/" ? key : key.replace(/\/+$/, "");
}

/** Comment identity includes authored text and workspace, but not the hunk. */
export function diffCommentKey(comment: DiffComment): string {
  const workspace = comment.localWorkspaceRoot == null ? "" : `${commentWorkspaceKey(comment.localWorkspaceRoot)}|`;
  return `${workspace}${comment.position.path}|${comment.position.side}|${formatDiffCommentLines(comment.position)}|${diffCommentText(comment)}`;
}

export const cloneDiffComment = (comment: DiffComment): DiffComment => ({
  ...comment, content: comment.content.map(block => ({ ...block })), position: { ...comment.position },
});

export const cloneDiffComments = (comments: readonly DiffComment[]): DiffComment[] => comments.map(cloneDiffComment);

export function mergeDiffComments(current: readonly DiffComment[], incoming: readonly DiffComment[]): DiffComment[] {
  const seen = new Set<string>();
  return [...current, ...incoming].filter(comment => {
    const key = diffCommentKey(comment);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).map(cloneDiffComment);
}

/** Ordinary file-comment context for the prompt. */
export function formatDiffComment(comment: DiffComment, promptNumber: number): string {
  let text = `\n## User Comment ${promptNumber}\nFile: ${comment.position.path}\n`;
  if (comment.localWorkspaceRoot != null) text += `Workspace: ${comment.localWorkspaceRoot}\n`;
  text += `Side: ${diffCommentSideLabel(comment)}\nLines: ${formatDiffCommentLines(comment.position)}\n`;
  if (comment.localDiffHunk != null && comment.localDiffHunk.trim().length > 0) {
    text += `Diff hunk:\n\`\`\`diff\n${comment.localDiffHunk}\n\`\`\`\n`;
  }
  return `${text}Comment:\n${diffCommentText(comment)}\n`;
}

export function formatDiffCommentsPrompt(prompt: string, comments: readonly DiffComment[]): string {
  if (comments.length === 0) return prompt;
  return `\n# Diff comments:\n${comments.map((comment, index) => formatDiffComment(comment, index + 1)).join("")}\n## My request:\n${prompt}\n`;
}
