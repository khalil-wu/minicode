// Conversational-plan delimiters emitted by the model. Fenced examples remain
// ordinary text, and the original assistant item is never rewritten.
export function proposedPlanParts(content: string) {
  let offset = 0;
  let opening: { start: number; end: number } | null = null;
  let fence: { character: string; length: number } | null = null;
  for (const rawLine of content.split("\n")) {
    const line = rawLine.replace(/\r$/, "");
    const codeFence = /^[ \t]*(`{3,}|~{3,})/.exec(line);
    if (codeFence) {
      if (!fence) fence = { character: codeFence[1][0], length: codeFence[1].length };
      else if (codeFence[1][0] === fence.character && codeFence[1].length >= fence.length) fence = null;
    } else if (!fence && line.trimEnd() === "<proposed_plan>" && !opening) {
      opening = { start: offset, end: offset + rawLine.length + 1 };
    } else if (!fence && line.trimEnd() === "</proposed_plan>" && opening) {
      return { before: content.slice(0, opening.start), plan: content.slice(opening.end, offset).trimEnd(),
        after: content.slice(offset + rawLine.length + 1), complete: true };
    }
    offset += rawLine.length + 1;
  }
  return opening ? { before: content.slice(0, opening.start), plan: content.slice(opening.end), after: "", complete: false } : null;
}
