import { formatBytes } from "./format-bytes";

/** File reads and project indexing share the editor's existing admission limit. */
export function editorFileLimitReason(content: string, sizeBytes?: number): string | null {
  const maxBytes = 2 * 1024 * 1024;
  if (sizeBytes !== undefined && sizeBytes > maxBytes) {
    return `该文件大小为 ${formatBytes(sizeBytes)}，超过编辑器 ${formatBytes(maxBytes)} 的限制。`;
  }
  if (content.length > 1_000_000) {
    return `该文件包含 ${content.length.toLocaleString()} 个字符，超过编辑器限制。`;
  }
  const lines = content ? content.split(/\r\n|\r|\n/).length : 0;
  return lines > 20_000 ? `该文件包含 ${lines.toLocaleString()} 行，超过编辑器限制。` : null;
}
