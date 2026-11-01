import { normalizeWorkspacePath } from "./workspace-path";

export interface CodeReviewComment {
  file: string;
  title: string;
  body: string;
  start: number;
  end: number;
  priority?: number;
}

const directiveLine = /^\s*:{1,3}code-comment(?:\[[^\]\r\n]*\])?\{(.*)\}\s*$/;
const attribute = /([A-Za-z][\w-]*)\s*=\s*(?:"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)'|([^\s}]+))/g;
const numberAttribute = (value: string | undefined): number | undefined => value?.trim() && Number.isFinite(Number(value)) ? Math.trunc(Number(value)) : undefined;

function commentPath(file: string, workspaceRoot: string): string {
  const path = normalizeWorkspacePath(file);
  const root = normalizeWorkspacePath(workspaceRoot).replace(/\/$/, "");
  const windows = /^[A-Za-z]:/.test(root) || root.startsWith("//");
  const comparedPath = windows ? path.toLowerCase() : path;
  const comparedRoot = windows ? root.toLowerCase() : root;
  if (root && comparedPath.startsWith(`${comparedRoot}/`)) return path.slice(root.length + 1);
  const folder = `${comparedRoot.split("/").at(-1)}/`;
  const index = root ? comparedPath.indexOf(folder) : -1;
  return index >= 0 && (index === 0 || comparedPath[index - 1] === "/") ? path.slice(index + folder.length) : path;
}

/** Line-start directives with required text; the end line defaults to the start line. */
export function parseCodeComments(markdown: string, workspaceRoot: string): CodeReviewComment[] {
  const comments: CodeReviewComment[] = [];
  let fence: string | undefined;
  for (const line of markdown.split(/\r?\n/)) {
    // A directive quoted inside a code fence stays literal text in the rendered
    // message, so it is not a finding either.
    const marker = /^\s*(`{3,}|~{3,})/.exec(line)?.[1];
    if (marker) {
      if (fence && marker[0] === fence[0] && marker.length >= fence.length) fence = undefined;
      else if (!fence) fence = marker;
      continue;
    }
    if (fence) continue;
    const directive = directiveLine.exec(line);
    if (!directive) continue;
    const attrs: Record<string, string> = {};
    for (const match of directive[1].matchAll(attribute)) attrs[match[1]] = (match[2] ?? match[3] ?? match[4]).replace(/\\(["'\\])/g, "$1").trim();
    if (!attrs.title || !attrs.body || !attrs.file) continue;
    const priority = numberAttribute(attrs.priority);
    const title = priority !== undefined && !/^(?:<sub>\s*)*\[p\d\]/i.test(attrs.title) ? `[P${priority}] ${attrs.title}` : attrs.title;
    const start = Math.max(1, numberAttribute(attrs.start) ?? 1);
    comments.push({ file: commentPath(attrs.file, workspaceRoot), title, body: attrs.body, start,
      end: Math.max(start, numberAttribute(attrs.end) ?? start), ...(priority === undefined ? {} : { priority }) });
  }
  return comments;
}

/** Directive render handlers return null; original markdown stays in the transcript. */
export function withoutCodeCommentDirectives(markdown: string): string {
  if (!markdown.includes(":code-comment")) return markdown;
  let fence: string | undefined;
  return markdown.split(/(?<=\n)/).filter(raw => {
    const line = raw.replace(/\r?\n$/, "");
    const marker = /^\s*(`{3,}|~{3,})/.exec(line)?.[1];
    if (marker) {
      if (fence && marker[0] === fence[0] && marker.length >= fence.length) fence = undefined;
      else if (!fence) fence = marker;
      return true;
    }
    return Boolean(fence) || !directiveLine.test(line);
  }).join("");
}
