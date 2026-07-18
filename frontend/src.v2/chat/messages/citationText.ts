import type { Citation } from "../../stores/types";

const sourceLabelPattern = "(?:\\*\\*|__)?(?:\u6765\u6e90|\u6570\u636e\u6765\u6e90|\u4fe1\u606f\u6765\u6e90|\u53c2\u8003\u6765\u6e90|\u8d44\u6599\u6765\u6e90|\u53c2\u8003|sources?|references?)(?:\\*\\*|__)?";
const sourceHeadingLabelPattern = "(?:\u6765\u6e90|\u6570\u636e\u6765\u6e90|\u4fe1\u606f\u6765\u6e90|\u53c2\u8003\u6765\u6e90|\u8d44\u6599\u6765\u6e90|\u53c2\u8003\u6765\u6e90\u5217\u8868|\u53c2\u8003\u8d44\u6599|\u53c2\u8003\u6587\u732e|\u53c2\u8003|sources?|references?)";
const splitHostPattern = "[\\w.-]+\\.[a-z](?:\\s+[a-z]){1,}(?:\\/\\S*)?";
const urlOrHostPattern = `(?:<?https?:\\/\\/\\S+>?|[\\w.-]+\\.[a-z]{2,}(?:\\/\\S*)?|${splitHostPattern})`;

const sourceLinePattern = new RegExp(
  `^\\s*${sourceLabelPattern}\\s*[:\\uFF1A]\\s*(?:\\[\\d+\\]\\s*)?.*${urlOrHostPattern}.*$`,
  "i",
);
const sourceListOnlyPattern = new RegExp(
  `^\\s*${sourceLabelPattern}\\s*[:\\uFF1A]\\s*(?:\\[\\d+\\](?:\\s*${urlOrHostPattern})?(?:\\s*[,;\\uFF0C\\u3001]\\s*)?)+\\s*$`,
  "i",
);
const sourceCitationSummaryPattern = new RegExp(
  `^\\s*${sourceLabelPattern}\\s*[:\\uFF1A]\\s*(?:\\[\\d+\\]\\s*[^\\[]+)+\\s*$`,
  "i",
);
const sourceIntroLinePattern = new RegExp(
  `^\\s*${sourceLabelPattern}\\s*[:\\uFF1A]\\s*[^\\n]{1,140}\\s*$`,
  "i",
);
const sourceHeadingPattern = new RegExp(
  `^\\s{0,3}(?:#{1,6}\\s*)?${sourceHeadingLabelPattern}\\s*[:\\uFF1A]?\\s*$`,
  "i",
);
const sourceItemPattern = new RegExp(
  `^\\s*(?:[-*]\\s*)?(?:\\[\\d+\\]|\\d+[.)])\\s*(?:[^\\n:\\uFF1A]{0,160}(?:[:\\uFF1A]|\\s+)\\s*)?${urlOrHostPattern}.*$`,
  "i",
);
const sourceTitlePattern = new RegExp("^\\s*(?:[-*]\\s*)?(?:\\[\\d+\\]|\\d+[.)])\\s+.+[:\\uFF1A]\\s*$");
const bareUrlPattern = /^\s*<?https?:\/\/\S+>?\s*$/i;
const sourceMarkdownLinkOnlyPattern = /^\s*(?:[-*]\s*)?\[[^\]]+\]\(\s*<?https?:\/\/[^)\s>]+>?\s*\)\s*[,;，。、]*\s*$/i;
const sourceNameCitationLinePattern = /^[\s,，、;；]*(?:[\w.\-\s\u3400-\u9fff（）()]+)?\[\d{1,3}\][\s。.,，、;；]*$/i;
const indexedCitationMarkerPattern = /(?:\[\d+\]|\[\[\\?\d+\\?\]\]\([^)]+\)|\[\\\[\d+\\\]\]\(<[^)]+>\))/g;
const urlOrHostGlobalPattern = new RegExp(urlOrHostPattern, "gi");
const inlineSourceLinkPattern = /\s*\[\s*(?:https?:\/\/[^\]\s]+|(?:www\.)?[\w.-]+\.[a-z]{2,}(?:\/[^\]\s]*)?)\s*\]\(\s*<?https?:\/\/[^)\s>]+>?\s*\)/gi;
const toolTruncationPlaceholderPattern = /(?:\.{3}|…)?\s*\[\s*(?:内容\s*(?:已)?截断|(?:已)?截断(?:内容)?|省略(?:内容)?|content\s+truncated|output\s+truncated|truncated(?:[^\]\r\n]{0,180})?)\s*\]\s*(?:\.{3}|…)?/gi;

const isInlineIndexedSourceList = (line: string): boolean => {
  const trimmed = line.trim();
  if (!trimmed.startsWith("[")) return false;
  const markers = [...trimmed.matchAll(indexedCitationMarkerPattern)];
  if (markers.length < 2 || markers[0].index !== 0) return false;
  return [...trimmed.matchAll(urlOrHostGlobalPattern)].length >= 2;
};

const stripInlineSourceLinks = (line: string): string => (
  line
    .replace(inlineSourceLinkPattern, "")
    .replace(/\s+([。！？!?；;，,])/g, "$1")
);

const stripToolTruncationPlaceholders = (line: string): string => (
  line
    .replace(toolTruncationPlaceholderPattern, "")
    .replace(/\s+([。！？!?；;，,])/g, "$1")
    .replace(/[ \t]{2,}/g, " ")
);

const isToolTruncationPlaceholderLine = (line: string): boolean => {
  const stripped = stripToolTruncationPlaceholders(line);
  return stripped !== line && stripped.replace(/[.\s…。、，,;；:：!?！？\-—_]+/g, "").length === 0;
};

const isSourceSectionContinuation = (line: string): boolean => (
  line.trim() === "" ||
  sourceItemPattern.test(line) ||
  sourceTitlePattern.test(line) ||
  bareUrlPattern.test(line) ||
  sourceMarkdownLinkOnlyPattern.test(line) ||
  sourceNameCitationLinePattern.test(line)
);

export const stripModelAuthoredSources = (content: string): string => {
  const lines = content.split(/\r?\n/);
  const kept: string[] = [];
  let inSourceSection = false;
  let inFence = false;

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index] ?? "";
    if (/^\s*(```|~~~)/.test(line)) {
      inFence = !inFence;
      kept.push(line);
      continue;
    }
    if (inFence) {
      kept.push(line);
      continue;
    }
    if (isToolTruncationPlaceholderLine(line)) {
      continue;
    }
    const visibleLine = stripToolTruncationPlaceholders(line);
    if (
      sourceLinePattern.test(visibleLine) ||
      sourceListOnlyPattern.test(visibleLine) ||
      sourceCitationSummaryPattern.test(visibleLine) ||
      isInlineIndexedSourceList(visibleLine)
    ) {
      continue;
    }
    if (sourceIntroLinePattern.test(visibleLine)) {
      const hasPriorAnswerText = kept.some((keptLine) => keptLine.trim().length > 0);
      const startsSourceBlock = hasPriorAnswerText || isSourceSectionContinuation(lines[index + 1] ?? "");
      if (startsSourceBlock) {
        inSourceSection = true;
        continue;
      }
    }
    if (sourceHeadingPattern.test(visibleLine)) {
      inSourceSection = true;
      continue;
    }
    if (inSourceSection) {
      if (isSourceSectionContinuation(visibleLine)) {
        continue;
      }
      inSourceSection = false;
    }
    kept.push(stripInlineSourceLinks(visibleLine));
  }

  return kept.join("\n").trim();
};

const standaloneCitationMarkerPattern = /(?<![A-Za-z0-9_])\[\d{1,3}\](?=([\s，。！？；：、,.!?;:)）\[]|$))/g;

const removeModelAuthoredCitationMarkers = (content: string): string => (
  content
    .replace(standaloneCitationMarkerPattern, "")
    .replace(/[ \t]+([，。！？；：、,.!?;:])/g, "$1")
    .replace(/([（(【「『])\s+/g, "$1")
    .replace(/\s+([）)】」』])/g, "$1")
    .replace(/[ \t]{2,}/g, " ")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n[ \t]+/g, "\n")
    .trim()
);

export const normalizeCitationText = (content: string, citations: Citation[] = []): string => {
  const original = content.trim();
  let next = stripModelAuthoredSources(content);
  if (!citations.length) {
    return next !== original ? removeModelAuthoredCitationMarkers(next) : next;
  }
  return removeModelAuthoredCitationMarkers(next);
};
