import { apiBase, authHeaders, fetchWithTimeout, errorMessageFromResponseText } from "./api";
import type { WorkspaceBufferEdit } from "../panels/applyWorkspaceBufferEdits";
import { applyOffsetEdits } from "../panels/applyWorkspaceBufferEdits";

export interface WorkspaceSearchOptions { query: string; regex: boolean; caseSensitive: boolean; wholeWord: boolean; include: string; exclude: string; }
export interface WorkspaceSearchMatch {
  id: string; offset: number; length: number; line: number; column: number; end_line: number; end_column: number;
  text: string; snippet: string; groups: Array<string | null>; named_groups: Record<string, string | null>;
}
export interface WorkspaceSearchFile {
  path: string; content: string; original: string; content_hash: string; size_bytes: number; read_only: boolean; from_buffer: boolean; matches: WorkspaceSearchMatch[];
}
export interface WorkspaceSearchResult { workspace_root: string; files: WorkspaceSearchFile[]; match_count: number; truncated: boolean; issues: Array<{path: string; message: string}>; }
export interface WorkspaceSearchBuffer { path: string; content: string; original: string; content_hash: string; read_only: boolean; }

export function splitSearchGlobs(value: string): string[] {
  const patterns: string[] = [];
  let depth = 0, current = "";
  for (const char of value) {
    if (char === "{") depth++;
    if (char === "}") depth--;
    if ((char === "," || char === "\n") && depth === 0) { if (current.trim()) patterns.push(current.trim()); current = ""; }
    else current += char;
  }
  if (current.trim()) patterns.push(current.trim());
  return patterns;
}

export async function searchWorkspaceText(workspaceRoot: string, options: WorkspaceSearchOptions, buffers: WorkspaceSearchBuffer[], signal?: AbortSignal): Promise<WorkspaceSearchResult> {
  const url = new URL(`${apiBase()}/api/workspace/search-content`);
  url.searchParams.set("workspace_root", workspaceRoot);
  const response = await fetchWithTimeout(url, { method: "POST", headers: authHeaders({ "content-type": "application/json" }), signal,
    body: JSON.stringify({ query: options.query, regex: options.regex, case_sensitive: options.caseSensitive, whole_word: options.wholeWord,
      include: splitSearchGlobs(options.include), exclude: splitSearchGlobs(options.exclude), buffers }) });
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  return response.json();
}

export function replacementText(template: string, match: WorkspaceSearchMatch, regex: boolean): string {
  if (!regex) return template;
  return template.replace(/\\([nrt])|\$(\$|&|0|[1-9]\d?|<[^>]+>)/g, (token, escape: string, capture: string) => {
    if (escape) return ({ n: "\n", r: "\r", t: "\t" } as Record<string, string>)[escape];
    if (capture === "$") return "$";
    if (capture === "&" || capture === "0") return match.text;
    if (capture.startsWith("<")) return match.named_groups[capture.slice(1, -1)] ?? "";
    return Number(capture) <= match.groups.length ? match.groups[Number(capture) - 1] ?? "" : token;
  });
}

export function selectedReplacementFiles(files: WorkspaceSearchFile[], selected: Set<string>, replacement: string, regex: boolean): Array<WorkspaceBufferEdit & { after: string }> {
  return files.filter((file) => !file.read_only).flatMap((file) => {
    const eol = file.content.match(/\r\n|\r|\n/)?.[0] ?? "\n";
    const edits = file.matches.filter((match) => selected.has(match.id)).map((match) => ({ offset: match.offset, length: match.length,
      text: replacementText(replacement, match, regex).replace(/\r\n|\r|\n/g, eol) }));
    if (!edits.length) return [];
    return [{ path: file.path, before: file.content, original: file.original, contentHash: file.content_hash, sizeBytes: file.size_bytes, edits, after: applyOffsetEdits(file.content, edits) }];
  });
}
