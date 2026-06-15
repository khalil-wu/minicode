import { Copy, RotateCcw, Trash2, RotateCw, Quote } from "lucide-react";  // 🔧 添加 Quote
import { useCallback, useEffect, useRef, useState } from "react";
import type React from "react";
import type { AssistantMarkdownCellState } from "./cellTypes";
import type { Citation } from "../../stores/types";
import { MarkdownRenderer } from "../messages/MarkdownRenderer";
import { useAppStore } from "../../stores";
import "./cells.css";

export function AssistantMarkdownCell({
  cell,
}: {
  cell: AssistantMarkdownCellState;
}) {
  const [copied, setCopied] = useState(false);
  // ✅ P1-2: 打字机效果状态
  const [displayedContent, setDisplayedContent] = useState(
    cell.isStreaming ? "" : cell.markdownSource
  );
  const displayedLenRef = useRef(displayedContent.length);
  displayedLenRef.current = displayedContent.length;

  // ✅ P3-4: 优化打字机效果 - rAF + ref追踪 + 追赶阈值
  useEffect(() => {
    if (!cell.isStreaming) {
      setDisplayedContent(cell.markdownSource);
      return;
    }

    const targetContent = cell.markdownSource;
    const targetLen = targetContent.length;

    // 追赶阈值：如果积压超过150字符，跳过动画直接全量更新
    if (targetLen - displayedLenRef.current > 150) {
      setDisplayedContent(targetContent);
      return;
    }

    if (displayedLenRef.current >= targetLen) {
      return;
    }

    let animationFrameId: number;
    let lastUpdate = performance.now();
    const charsPerSecond = 200;
    const msPerChar = 1000 / charsPerSecond;

    const animate = (timestamp: number) => {
      const elapsed = timestamp - lastUpdate;
      if (elapsed >= msPerChar) {
        const charsToAdd = Math.max(1, Math.floor(elapsed / msPerChar));
        const nextLength = Math.min(displayedLenRef.current + charsToAdd, targetLen);
        setDisplayedContent(targetContent.slice(0, nextLength));
        lastUpdate = timestamp;
      }

      if (displayedLenRef.current < targetLen) {
        animationFrameId = requestAnimationFrame(animate);
      }
    };

    animationFrameId = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(animationFrameId);
  }, [cell.markdownSource, cell.isStreaming]);

  const recallMessage = useAppStore((s) => s.recallMessage);
  const deleteMessage = useAppStore((s) => s.deleteMessage);
  const sources = uniqueCitationSources(displayedContent, cell.citations);
  const displayMarkdown = sources.length
    ? stripInlineCitationMarkers(displayedContent, cell.citations)
    : displayedContent;

  const copy = useCallback(() => {
    navigator.clipboard.writeText(cell.markdownSource).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    });
  }, [cell.markdownSource]);

  const recall = useCallback(async () => {
    if (!cell.messageId) return;
    const state = useAppStore.getState();
    const index = state.messages.findIndex((item) => item.id === cell.messageId);
    const userIndex = (() => {
      for (let i = index; i >= 0; i -= 1) {
        if (state.messages[i]?.role === "user") return i;
      }
      return index;
    })();
    const removeCount = userIndex >= 0 ? state.messages.length - userIndex : 1;
    const { showConfirm } = await import("../../overlays/DialogService");
    const ok = await showConfirm({
      title: "召回消息",
      message: `将对应提示召回到输入框？这会从当前视图移除后续 ${removeCount} 条消息。`,
      confirmLabel: "召回",
      danger: removeCount > 1 || state.isStreaming,
    });
    if (!ok) return;
    if (useAppStore.getState().isStreaming) {
      useAppStore.getState().interrupt();
    }
    recallMessage(cell.messageId);
  }, [cell.messageId, recallMessage]);

  const deleteWithConfirm = useCallback(async () => {
    if (!cell.messageId) return;
    const { showConfirm } = await import("../../overlays/DialogService");
    const ok = await showConfirm({
      title: "删除回复",
      message: "从当前对话视图中删除这条助手回复？",
      confirmLabel: "删除",
      danger: true,
    });
    if (!ok) return;
    deleteMessage(cell.messageId);
  }, [cell.messageId, deleteMessage]);

  // 🔧 新增：引用回复功能
  const quoteReply = useCallback(() => {
    if (!cell.markdownSource) return;
    const composerTextarea = document.querySelector('[data-composer-input]') as HTMLTextAreaElement;
    if (composerTextarea) {
      const quotedText = `> ${cell.markdownSource.split('\n').join('\n> ')}\n\n`;
      composerTextarea.value = quotedText + composerTextarea.value;
      composerTextarea.focus();
      composerTextarea.setSelectionRange(quotedText.length, quotedText.length);
    }
  }, [cell.markdownSource]);

  const regenerate = useCallback(async () => {
    if (!cell.messageId) return;
    const state = useAppStore.getState();

    // Find the user message that triggered this response
    const index = state.messages.findIndex((item) => item.id === cell.messageId);
    if (index < 0) return;

    // Find the preceding user message
    let userIndex = -1;
    for (let i = index - 1; i >= 0; i--) {
      if (state.messages[i]?.role === "user") {
        userIndex = i;
        break;
      }
    }

    if (userIndex < 0) return;

    const { showConfirm } = await import("../../overlays/DialogService");
    const ok = await showConfirm({
      title: "重新生成",
      message: "将删除当前回复并重新生成。继续吗？",
      confirmLabel: "重新生成",
      danger: false,
    });
    if (!ok) return;

    // Delete this assistant message and all following messages
    const removeCount = state.messages.length - index;
    for (let i = 0; i < removeCount; i++) {
      const msg = state.messages[index];
      if (msg) {
        deleteMessage(msg.id);
      }
    }

    // Re-send the user message
    const userMessage = state.messages[userIndex];
    if (userMessage && userMessage.role === "user") {
      recallMessage(userMessage.id);
      // Trigger send after a short delay
      setTimeout(() => {
        const sendBtn = document.querySelector('[data-send-button]') as HTMLButtonElement;
        if (sendBtn) sendBtn.click();
      }, 100);
    }
  }, [cell.messageId, deleteMessage, recallMessage]);

  return (
    <div className="assistant-cell-wrap">
      <div className="assistant-cell-content md-prose">
        <MarkdownRenderer
          content={displayMarkdown}
          isStreaming={cell.isStreaming || false}
        />
        {sources.length > 0 && (
          <div className="assistant-cell-sources">
            <div className="assistant-cell-sources-title">来源</div>
            <div className="assistant-cell-sources-list">
              {sources.map((source) => (
                <a
                  key={source.url}
                  href={source.url}
                  target="_blank"
                  rel="noreferrer"
                  title={source.title || source.url}
                  className="assistant-cell-source-link"
                >
                  {source.label}
                </a>
              ))}
            </div>
          </div>
        )}
      </div>
      <div className="assistant-cell-actions">
        {cell.copyable && (
          <button
            type="button"
            onClick={copy}
            title={copied ? "已复制" : "复制回复"}
            aria-label={copied ? "已复制" : "复制回复"}
            className="cell-action-btn"
          >
            <Copy size={12} />
          </button>
        )}
        {cell.messageId && (
          <>
            <button
              type="button"
              onClick={quoteReply}
              title="引用回复"
              aria-label="引用回复"
              className="cell-action-btn"
            >
              <Quote size={12} />
            </button>
            <button
              type="button"
              onClick={regenerate}
              title="重新生成"
              aria-label="重新生成"
              className="cell-action-btn"
            >
              <RotateCw size={12} />
            </button>
            <button
              type="button"
              onClick={recall}
              title="召回到输入框"
              aria-label="召回到输入框"
              className="cell-action-btn"
            >
              <RotateCcw size={12} />
            </button>
            <button
              type="button"
              onClick={deleteWithConfirm}
              title="删除回复"
              aria-label="删除回复"
              className="cell-action-btn"
            >
              <Trash2 size={12} />
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function citationHref(citation: Citation | undefined): string {
  const candidate = String(citation?.url || citation?.source || "").trim();
  return /^https?:\/\//i.test(candidate) ? candidate : "";
}

function sourceLabel(url: string): string {
  try {
    const parsed = new URL(url);
    return parsed.hostname.replace(/^www\./i, "");
  } catch {
    return url;
  }
}

function uniqueCitationSources(content: string, citations: AssistantMarkdownCellState["citations"] = []): Array<{ url: string; label: string; title?: string }> {
  const citedIndexes = extractInlineCitationIndexes(content);
  if (citedIndexes.size === 0) return [];
  const seen = new Set<string>();
  const sources: Array<{ url: string; label: string; title?: string }> = [];
  for (const [index, citation] of citations.entries()) {
    if (!citedIndexes.has(index + 1)) continue;
    const url = citationHref(citation);
    if (!url || seen.has(url)) continue;
    seen.add(url);
    sources.push({
      url,
      label: citation.label || sourceLabel(url),
      title: citation.title,
    });
  }
  return sources;
}

function extractInlineCitationIndexes(content: string): Set<number> {
  const indexes = new Set<number>();
  for (const match of content.matchAll(/\[(\d+)\]/g)) {
    const index = Number(match[1]);
    if (Number.isFinite(index) && index > 0) indexes.add(index);
  }
  return indexes;
}

function stripInlineCitationMarkers(content: string, citations: AssistantMarkdownCellState["citations"] = []): string {
  const linkedIndexes = new Set(
    citations
      .map((citation, index) => citationHref(citation) ? index + 1 : 0)
      .filter((index) => index > 0),
  );
  if (linkedIndexes.size === 0) return content;
  return content
    .replace(/\[(\d+)\]/g, (match, rawIndex) =>
      linkedIndexes.has(Number(rawIndex)) ? "" : match,
    )
    .replace(/\s+([,.;:!?，。；：！？])/g, "$1")
    .replace(/[ \t]{2,}/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
