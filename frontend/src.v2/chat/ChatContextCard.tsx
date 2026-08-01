import {
  ChevronRight,
  FileImage,
  FileText,
  ListChecks,
  MonitorPlay,
  Network,
  Paperclip,
  PanelRightOpen,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { AgentAvatar } from "../components/AgentAvatar";
import { BrandIcon } from "../components/BrandIcon";
import { ImageLightbox } from "../components/ImageLightbox";
import {
  embeddedBrowserActivate,
  embeddedBrowserList,
  isDesktop,
  onEmbeddedBrowserEvent,
  type EmbeddedBrowserState,
} from "../desktop/runtime";
import { projectAgentViews } from "../lib/agent-view-model";
import { getWebSocket } from "../hooks/useWebSocket";
import { previewUrlForPath } from "../shell/fileTreeHelpers";
import { useAppStore } from "../stores";
import type { ChatMessage, RightStackTab } from "../stores/types";
import { openWebTarget } from "./openWebTarget";
import "./ChatContextCard.css";

interface ContextSource {
  id: string;
  label: string;
  url: string;
  messageId: string;
}

interface ContextAttachment {
  id: string;
  label: string;
  kind: "image" | "file" | "document";
  messageId: string;
  artifactId?: string;
  docId?: string;
  path?: string;
  mediaType?: string;
  relatedCount: number;
}

const CONTEXT_CARD_EXIT_MS = 190;
const CONTEXT_CARD_PREPARE_MS = 220;

const shortPath = (path: string): string => {
  const normalized = path.replace(/[\\/]+$/, "");
  return normalized.split(/[\\/]/).filter(Boolean).at(-1) || path || "未选择工作区";
};

const sourceLabel = (value: string): string => {
  try {
    const url = new URL(value);
    return url.hostname.replace(/^www\./, "") || value;
  } catch {
    return shortPath(value);
  }
};

const collectSources = (messages: ChatMessage[]): ContextSource[] => {
  const items: ContextSource[] = [];
  const seen = new Set<string>();
  const push = (item: ContextSource) => {
    if (!item.id || seen.has(item.id)) return;
    seen.add(item.id);
    items.push(item);
  };

  for (const message of messages) {
    for (const citation of message.citations ?? []) {
      const url = citation.url || citation.source;
      if (!url) continue;
      push({
        id: `citation:${url}`,
        label: citation.title || citation.label || sourceLabel(url),
        url,
        messageId: message.id,
      });
    }
  }

  return items.slice(-6).reverse();
};

const collectAttachments = (messages: ChatMessage[]): ContextAttachment[] => {
  const items: Omit<ContextAttachment, "relatedCount">[] = [];
  const seen = new Set<string>();
  for (const message of messages) {
    for (const attachment of message.attachmentRefs ?? []) {
      const id = attachment.artifactId || attachment.docId || attachment.id || `${message.id}:${attachment.name}`;
      if (!id || seen.has(id)) continue;
      seen.add(id);
      items.push({
        id,
        label: attachment.name || "附件",
        kind: attachment.kind,
        messageId: message.id,
        artifactId: attachment.artifactId,
        docId: attachment.docId,
        mediaType: attachment.mediaType,
      });
    }
    for (const attachment of message.replyAttachments ?? []) {
      const id = attachment.path || `${message.id}:reply:${items.length}`;
      if (seen.has(id)) continue;
      seen.add(id);
      items.push({
        id,
        label: shortPath(attachment.path),
        kind: attachment.isImage ? "image" : "file",
        messageId: message.id,
        path: attachment.path,
        mediaType: attachment.isImage ? "image/*" : undefined,
      });
    }
  }
  const groupCounts = new Map<string, number>();
  for (const item of items) groupCounts.set(item.messageId, (groupCounts.get(item.messageId) ?? 0) + 1);
  return items.slice(-8).reverse().map((item) => ({
    ...item,
    relatedCount: groupCounts.get(item.messageId) ?? 1,
  }));
};

export const ChatContextCard = () => {
  const messages = useAppStore((state) => state.messages);
  const subagents = useAppStore((state) => state.subagents);
  const backgroundTasks = useAppStore((state) => state.backgroundTasks);
  const conversationId = useAppStore((state) => state.conversationId);
  const rightPanelOpen = useAppStore((state) => state.rightPanelOpen);
  const setRightStackTab = useAppStore((state) => state.setRightStackTab);
  const setFocusedSubagentId = useAppStore((state) => state.setFocusedSubagentId);
  const [browserTargets, setBrowserTargets] = useState<EmbeddedBrowserState[]>([]);
  const [attachmentPreview, setAttachmentPreview] = useState<{ src: string; name: string } | null>(null);
  const [pendingAttachmentId, setPendingAttachmentId] = useState<string | null>(null);
  const [failedAttachmentId, setFailedAttachmentId] = useState<string | null>(null);
  const pendingAttachmentIdRef = useRef<string | null>(null);
  const [cardPresence, setCardPresence] = useState<"visible" | "exiting" | "preparing" | "hidden">(
    rightPanelOpen ? "hidden" : "visible",
  );
  const previousRightPanelOpenRef = useRef(rightPanelOpen);
  const agentViews = useMemo(() => projectAgentViews(subagents), [subagents]);
  const sources = useMemo(() => collectSources(messages), [messages]);
  const attachments = useMemo(() => collectAttachments(messages), [messages]);

  useEffect(() => {
    const wasOpen = previousRightPanelOpenRef.current;
    previousRightPanelOpenRef.current = rightPanelOpen;

    if (!rightPanelOpen) {
      if (!wasOpen) {
        setCardPresence("visible");
        return;
      }
      setCardPresence("preparing");
      const timeout = window.setTimeout(() => setCardPresence("visible"), CONTEXT_CARD_PREPARE_MS);
      return () => window.clearTimeout(timeout);
    }
    if (cardPresence === "hidden" || cardPresence === "preparing") {
      setCardPresence("hidden");
      return;
    }
    setCardPresence("exiting");
    const timeout = window.setTimeout(() => setCardPresence("hidden"), CONTEXT_CARD_EXIT_MS);
    return () => window.clearTimeout(timeout);
  }, [rightPanelOpen]);

  useEffect(() => {
    if (!isDesktop()) return;
    let cancelled = false;
    const refresh = () => {
      void Promise.resolve(embeddedBrowserList()).then((targets) => {
        if (!cancelled && Array.isArray(targets)) {
          setBrowserTargets(targets.filter((target) => target.url && target.url !== "about:blank"));
        }
      });
    };
    refresh();
    const unsubscribe = onEmbeddedBrowserEvent((event) => {
      if (!event.url || event.url === "about:blank" || event.type === "new-tab-request") return;
      setBrowserTargets((current) => {
        const nextTarget = { ...event, active: true };
        const existing = current.some((target) => target.id === event.id);
        return existing
          ? current.map((target) => target.id === event.id ? nextTarget : { ...target, active: false })
          : [nextTarget, ...current.map((target) => ({ ...target, active: false }))];
      });
    });
    return () => {
      cancelled = true;
      unsubscribe?.();
    };
  }, []);

  useEffect(() => {
    if (!rightPanelOpen && isDesktop()) {
      void Promise.resolve(embeddedBrowserList()).then((targets) => {
        if (Array.isArray(targets)) {
          setBrowserTargets(targets.filter((target) => target.url && target.url !== "about:blank"));
        }
      });
    }
  }, [rightPanelOpen]);

  useEffect(() => {
    const onArtifactImagePreview = (event: Event) => {
      const detail = (event as CustomEvent<{ artifactId?: string; url?: string }>).detail;
      const artifactId = pendingAttachmentIdRef.current;
      if (!artifactId || detail?.artifactId !== artifactId || !detail.url) return;
      const attachment = attachments.find((item) => item.artifactId === artifactId);
      setAttachmentPreview({ src: detail.url, name: attachment?.label || "image" });
      pendingAttachmentIdRef.current = null;
      setPendingAttachmentId(null);
      setFailedAttachmentId(null);
    };
    window.addEventListener("artifact:image-preview", onArtifactImagePreview);
    return () => window.removeEventListener("artifact:image-preview", onArtifactImagePreview);
  }, [attachments]);

  useEffect(() => {
    if (!pendingAttachmentId) return;
    const artifactId = pendingAttachmentId;
    const timeout = window.setTimeout(() => {
      if (pendingAttachmentIdRef.current !== artifactId) return;
      pendingAttachmentIdRef.current = null;
      setPendingAttachmentId(null);
      setFailedAttachmentId(artifactId);
    }, 10_000);
    return () => window.clearTimeout(timeout);
  }, [pendingAttachmentId]);

  const activeAgents = agentViews.filter((agent) => agent.status !== "completed");
  const completedAgents = agentViews.filter((agent) => agent.status === "completed");
  const scopedBackgroundTasks = backgroundTasks.filter((task) =>
    Boolean(conversationId) && task.conversationId === conversationId
  );
  const runningBackgroundTasks = scopedBackgroundTasks.filter((task) => task.status === "running").length;
  const failedBackgroundTasks = scopedBackgroundTasks.filter((task) => task.status === "failed").length;
  const cancelledBackgroundTasks = scopedBackgroundTasks.filter((task) => task.status === "cancelled").length;
  const backgroundTaskSummary = runningBackgroundTasks > 0
    ? `${runningBackgroundTasks} 个运行中`
    : failedBackgroundTasks > 0
      ? `${failedBackgroundTasks} 个失败`
      : cancelledBackgroundTasks > 0
        ? "已取消"
        : "已完成";
  const hasBackgroundTasks = scopedBackgroundTasks.length > 0;
  const contextCount = attachments.length + sources.length + browserTargets.length + agentViews.length + (hasBackgroundTasks ? 1 : 0);

  const openPanel = (tab: RightStackTab) => setRightStackTab(tab);
  const openAgent = (agentId?: string) => {
    setFocusedSubagentId(agentId ?? null);
    setRightStackTab("subagents");
  };
  const openBrowserTarget = (target?: EmbeddedBrowserState) => {
    if (target?.id) void embeddedBrowserActivate(target.id);
    setRightStackTab("browser");
  };
  const openAttachment = (attachment: ContextAttachment) => {
    const store = useAppStore.getState();
    const isImage = attachment.kind === "image" || attachment.mediaType?.startsWith("image/");
    if (isImage && attachment.path) {
      setAttachmentPreview({
        src: previewUrlForPath(attachment.path, store.workingDirectory || ""),
        name: attachment.label,
      });
      return;
    }
    if (isImage && attachment.artifactId) {
      setFailedAttachmentId(null);
      pendingAttachmentIdRef.current = attachment.artifactId;
      setPendingAttachmentId(attachment.artifactId);
      getWebSocket()?.send({
        type: "read_artifact",
        artifact_id: attachment.artifactId,
        purpose: "image_preview",
      });
      return;
    }
    if (attachment.path) {
      store.openEditorFile(attachment.path, attachment.label);
      return;
    }
    if (attachment.artifactId) {
      store.addPanel({
        id: `artifact-${attachment.artifactId}`,
        kind: "preview",
        label: attachment.label.slice(0, 24) || "Attachment",
      });
      store.setRightStackTab("preview");
      getWebSocket()?.send({ type: "read_artifact", artifact_id: attachment.artifactId, purpose: "attachment" });
      return;
    }
    store.setRightStackTab("artifacts");
  };
  const openBackgroundTasks = () => useAppStore.getState().setRightStackTab("tasks");

  if (cardPresence === "hidden") return null;

  return (
    <aside className="mc-chat-context-card" data-state={cardPresence} aria-label="工作区上下文摘要">
      <button
        type="button"
        className="mc-chat-context-card-compact"
        aria-label="打开上下文详情"
        title="打开上下文详情"
        onClick={() => openPanel("tasks")}
      >
        <PanelRightOpen size={18} strokeWidth={1.8} />
      </button>

      <header className="mc-chat-context-card-header">
        <span>上下文 <small>{contextCount}</small></span>
        <button type="button" aria-label="打开上下文详情" title="打开上下文详情" onClick={() => openPanel("tasks")}>
          <PanelRightOpen size={17} strokeWidth={1.8} />
        </button>
      </header>

      <div className="mc-chat-context-card-body">
        {contextCount === 0 && (
          <section className="mc-chat-context-card-section mc-chat-context-card-empty" aria-label="上下文入口">
            <button type="button" className="mc-chat-context-empty-row" onClick={() => openAgent()}>
              <Network size={16} />
              <span>子智能体</span>
              <small>0</small>
            </button>
            <button type="button" className="mc-chat-context-empty-row" onClick={() => openPanel("tasks")}>
              <Paperclip size={16} />
              <span>来源</span>
              <small>0</small>
            </button>
            <button type="button" className="mc-chat-context-empty-row" onClick={() => openBrowserTarget()}>
              <MonitorPlay size={16} />
              <span>浏览器</span>
              <small>0</small>
            </button>
            <button type="button" className="mc-chat-context-empty-row" onClick={openBackgroundTasks}>
              <ListChecks size={16} />
              <span>后台任务</span>
              <small>0</small>
            </button>
          </section>
        )}

        {agentViews.length > 0 && <section className="mc-chat-context-card-section" aria-label="子智能体摘要">
          <button type="button" className="mc-chat-context-section-title" onClick={() => openAgent()}>
            <span>子智能体</span>
            <small>{agentViews.length}</small>
          </button>
          <div className="mc-chat-context-agents" aria-label="最近的子智能体">
            {agentViews.slice(0, 5).map((agent) => (
              <button
                key={agent.id}
                type="button"
                className="mc-chat-context-agent"
                aria-label={`打开子智能体：${agent.title}`}
                title={agent.title}
                onClick={() => openAgent(agent.id)}
              >
                <AgentAvatar tone={agent.glyphTone} status={agent.status} size="small" />
              </button>
            ))}
            <button type="button" className="mc-chat-context-agent-summary" onClick={() => openAgent()}>
              {activeAgents.length > 0 ? `${activeAgents.length} 个运行中` : `${completedAgents.length} 个已完成`}
            </button>
          </div>
        </section>}

        {attachments.length > 0 && (
          <section className="mc-chat-context-card-section" aria-label="附件摘要">
            <button type="button" className="mc-chat-context-section-title" onClick={() => openPanel("artifacts")}>
              <span>附件</span>
              <small>{attachments.length}</small>
            </button>
            {attachments.slice(0, 3).map((attachment) => {
              const Icon = attachment.kind === "image" ? FileImage : FileText;
              return (
                <button
                  key={attachment.id}
                  type="button"
                  className="mc-chat-context-source"
                  aria-label={`查看附件：${attachment.label}`}
                  title={attachment.label}
                  onClick={() => openAttachment(attachment)}
                >
                  <span><Icon size={15} /></span>
                  <span className="mc-chat-context-source-content">
                    <span>{attachment.label}</span>
                    <small>
                      {pendingAttachmentId === attachment.artifactId
                        ? "加载中"
                        : failedAttachmentId === attachment.artifactId
                          ? "加载失败，点击重试"
                          : attachment.relatedCount > 1
                            ? `同组 ${attachment.relatedCount} 项`
                            : "来自当前对话"}
                    </small>
                  </span>
                </button>
              );
            })}
            {attachments.length > 3 && (
              <button type="button" className="mc-chat-context-more" onClick={() => openPanel("artifacts")}>
                还有 {attachments.length - 3} 项 <ChevronRight size={14} />
              </button>
            )}
          </section>
        )}

        {sources.length > 0 && <section className="mc-chat-context-card-section" aria-label="来源摘要">
          <button type="button" className="mc-chat-context-section-title" onClick={() => openPanel("tasks")}>
            <span>来源</span>
            <small>{sources.length}</small>
          </button>
          {sources.slice(0, 3).map((source) => (
            <button
              key={source.id}
              type="button"
              className="mc-chat-context-source"
              aria-label={`打开来源：${source.label}`}
              title={source.url}
              onClick={() => openWebTarget(source.url)}
            >
              <span>
                <BrandIcon
                  value={`${source.label} ${source.url}`}
                  websiteUrl={source.url}
                  fallback="web"
                  size={15}
                />
              </span>
              <span className="mc-chat-context-source-content">
                <span>{source.label}</span>
                <small>{sourceLabel(source.url)}</small>
              </span>
            </button>
          ))}
          {sources.length > 3 && (
            <button type="button" className="mc-chat-context-more" onClick={() => openPanel("tasks")}>
              还有 {sources.length - 3} 项 <ChevronRight size={14} />
            </button>
          )}
        </section>}

        {browserTargets.length > 0 && (
          <section className="mc-chat-context-card-section" aria-label="浏览器摘要">
            <button
              type="button"
              className="mc-chat-context-section-title"
              onClick={() => openBrowserTarget(browserTargets.find((target) => target.active) ?? browserTargets[0])}
            >
              <span>浏览器</span>
              <small>{browserTargets.length}</small>
            </button>
            {browserTargets.slice(0, 2).map((target) => (
              <button
                key={target.id}
                type="button"
                className="mc-chat-context-source"
                title={target.url}
                onClick={() => openBrowserTarget(target)}
              >
                <span>
                  <BrandIcon
                    value={`${target.title || ""} ${target.url}`}
                    iconUrl={target.faviconUrl}
                    websiteUrl={target.url}
                    fallback="web"
                    size={15}
                  />
                </span>
                <span className="mc-chat-context-source-content">
                  <span>{target.title || sourceLabel(target.url)}</span>
                  <small>{sourceLabel(target.url)}</small>
                </span>
              </button>
            ))}
          </section>
        )}

        {hasBackgroundTasks && (
          <section className="mc-chat-context-card-section" aria-label="后台任务摘要">
            <button type="button" className="mc-chat-context-section-title" onClick={openBackgroundTasks}>
              <span>后台任务</span>
              <small>{backgroundTaskSummary}</small>
            </button>
            <button type="button" className="mc-chat-context-empty-row" onClick={openBackgroundTasks}>
              <ListChecks size={16} /> 查看后台任务
            </button>
          </section>
        )}
      </div>
      {attachmentPreview ? (
        <ImageLightbox
          src={attachmentPreview.src}
          alt={attachmentPreview.name}
          title={attachmentPreview.name}
          onClose={() => setAttachmentPreview(null)}
        />
      ) : null}
    </aside>
  );
};
