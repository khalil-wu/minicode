export function smoothedLiveNarrationMarkdown(markdown: string, isStreaming = true): string {
  const value = markdown.trim();
  if (!value) return "";
  if (!isStreaming) return value;
  if (endsAtStableBoundary(value)) return value;

  const lastBreak = Math.max(
    value.lastIndexOf("\n"),
    value.lastIndexOf("。"),
    value.lastIndexOf("！"),
    value.lastIndexOf("？"),
    value.lastIndexOf("；"),
    value.lastIndexOf("，"),
    value.lastIndexOf("："),
    value.lastIndexOf(";"),
    value.lastIndexOf(","),
    value.lastIndexOf(":"),
    value.lastIndexOf("."),
    value.lastIndexOf("!"),
    value.lastIndexOf("?"),
  );
  if (lastBreak < 0) {
    // CJK streams can stop between any two characters. Showing a short
    // unpunctuated tail makes partial words such as “等待结” look final.
    return /[\u3400-\u9fff]/.test(value) && value.length <= 32 ? "" : value;
  }

  const tail = value.slice(lastBreak + 1).trim();
  if (!tail) return value;
  if (tail.length > 32) return value;

  const stable = value.slice(0, lastBreak + 1).trim();
  return stable || value;
}

function endsAtStableBoundary(value: string): boolean {
  return /[\n。！？；，：;,:.!?]$/.test(value.trim());
}
