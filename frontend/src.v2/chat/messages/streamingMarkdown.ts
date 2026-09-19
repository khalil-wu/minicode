export interface MarkdownPart { start: number; content: string }

/** Only completed lines advance the scan. Lists and fences retain their own
 * context; reference definitions/HTML require a document-wide Markdown parse. */
export class StreamingMarkdownPartition {
  source = "";
  parts: MarkdownPart[] = [];
  scannedCharacters = 0;
  private cursor = 0;
  private start = 0;
  private fence: { marker: string; length: number; start: number } | null = null;
  private math: "$$" | "\\]" | null = null;
  private list = false;
  private blankEnd = 0;
  private wholeDocument = false;
  private comment = false;

  private commit(end: number): void {
    const content = this.source.slice(this.start, end);
    if (content.trim()) this.parts.push({ start: this.start, content });
    this.start = end;
    this.blankEnd = 0;
    this.list = false;
  }

  push(content: string): { parts: MarkdownPart[]; tail: MarkdownPart; openFence: number; wholeDocument: boolean } {
    if (!content.startsWith(this.source)) {
      this.parts = [];
      this.cursor = this.start = this.blankEnd = this.scannedCharacters = 0;
      this.fence = this.math = null;
      this.list = this.wholeDocument = false;
      this.comment = false;
    }
    this.source = content;
    let newline: number;
    while ((newline = content.indexOf("\n", this.cursor)) >= 0) {
      const lineStart = this.cursor;
      const line = content.slice(lineStart, newline).replace(/\r$/, "");
      this.cursor = newline + 1;
      this.scannedCharacters += this.cursor - lineStart;
      if (this.fence) {
        const close = /^ {0,3}(`+|~+)[\t ]*$/.exec(line);
        if (close && close[1][0] === this.fence.marker && close[1].length >= this.fence.length) {
          this.fence = null;
          if (!this.list) this.commit(this.cursor);
        }
        continue;
      }
      if (this.math) {
        if (line.includes(this.math)) this.math = null;
        continue;
      }
      if (this.comment) {
        if (line.includes("-->")) this.comment = false;
        continue;
      }
      if (line.includes("<!--") && !line.includes("-->")) { this.comment = true; continue; }
      if (/^ {0,3}(?:\[[^\]]+\]:|<[a-zA-Z])/.test(line)) this.wholeDocument = true;
      const listLine = /^ {0,3}(?:[-+*]|\d+[.)])\s/.test(line) || /^ {0,3}>/.test(line);
      if (this.blankEnd && (!this.list || (!listLine && !/^\s+\S/.test(line))) && line.trim()) this.commit(this.blankEnd);
      if (listLine) this.list = true;
      const opening = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
      if (opening) {
        if (!this.list && lineStart > this.start) this.commit(lineStart);
        this.fence = { marker: opening[1][0], length: opening[1].length, start: lineStart };
        continue;
      }
      const dollars = line.match(/(?<!\\)\$\$/g)?.length ?? 0;
      if (dollars % 2) this.math = "$$";
      if (line.includes("\\[") && !line.includes("\\]")) this.math = "\\]";
      if (!line.trim()) {
        this.blankEnd = this.cursor;
        if (!this.list && !this.math) this.commit(this.cursor);
      }
    }
    const unfinished = content.slice(this.cursor);
    if (!this.fence && !this.comment && /^ {0,3}(?:\[[^\]]+\]:|<[a-zA-Z])/.test(unfinished)) this.wholeDocument = true;
    let openFence = this.fence?.start ?? -1;
    const close = /^ {0,3}(`+|~+)[\t ]*$/.exec(unfinished);
    if (this.fence && close && close[1][0] === this.fence.marker && close[1].length >= this.fence.length) openFence = -1;
    if (!this.fence && /^ {0,3}(`{3,}|~{3,})/.test(unfinished)) openFence = this.cursor;
    const start = this.wholeDocument ? 0 : this.start;
    return {
      parts: this.parts,
      tail: { start, content: content.slice(start) },
      openFence: this.list || this.wholeDocument ? -1 : openFence < 0 ? -1 : openFence - start,
      wholeDocument: this.wholeDocument,
    };
  }
}
