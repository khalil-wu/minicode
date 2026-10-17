/** Source ads: cap the review as a whole, rather than adding file/line page buttons. */
export function reviewNeedsSingleFile(fileCount: number, changedLines: number, changedBytes: number): boolean {
  return fileCount > 128 || changedLines > 9000 || changedBytes > 12 * 1024 * 1024;
}
