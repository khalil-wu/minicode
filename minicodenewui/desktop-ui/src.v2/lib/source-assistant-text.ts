/** A whole assistant-message control marker, never a substring filter. */
export const projectSourceAssistantText = (text: string, isFinalPhase: boolean): string => {
  const envelope = isFinalPhase ? text.match(/^<!\[CDATA\[ ([\s\S]*) \]\]>$/) : null;
  return /^::\s*SKIP_COMPLETION\s*::$/.test((envelope?.[1] ?? text).trim()) ? "" : text;
};
