import {
  ChevronRight,
  FileDiff,
  FileImage,
  FileText,
  Folder,
  GitBranch,
  ListChecks,
  Monitor,
  PanelRightOpen,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent } from "react";
import { useShallow } from "zustand/react/shallow";
import { AgentAvatar } from "../components/AgentAvatar";
import { BrandIcon } from "../components/BrandIcon";
import {
  embeddedBrowserActivate,
  embeddedBrowserList,
  isDesktop,
  onEmbeddedBrowserEvent,
  type EmbeddedBrowserState,
} from "../desktop/runtime";
import { projectAgentViews } from "../lib/agent-view-model";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import type { ToolCallRecord } from "../lib/tool-call-reducer";
import {
  artifactMediaTypeForProjection,
  artifactSummaryForRecord,
  canonicalArtifactKind,
  cleanArtifactLabel,
  normalizeArtifactPreview,
} from "../lib/artifact-projection";
import {
  artifactImageResourceUrl,
  inlineImageResourceUrl,
  withPreviewCacheBust,
} from "../lib/artifact-resource";
import { useAppStore } from "../stores";
import type { ChatMessage, RightStackTab } from "../stores/types";
import { getWebSocket } from "../hooks/useWebSocket";
import { openArtifactPreview, openAttachmentPreview, openWorkspaceFilePreview } from "./openAttachmentPreview";
import { openWebTarget } from "./openWebTarget";
import { useTurnChanges } from "./useTurnChanges";
import "./ChatContextCard.css";

interface ContextSource {
  id: string;
  label: string;
  url?: string;
  detail: string;
  messageId: string;
}

interface ContextAttachment {
  id: string;
  label: string;
  kind: "image" | "file" | "document";
  source: "artifact" | "attachment" | "workspace";
  messageId: string;
  artifactId?: string;
  docId?: string;
  path?: string;
  mediaType?: string;
  previewUrl?: string;
  generated?: boolean;
  /** Conversation that owns the artifact/attachment. */
  conversationId?: string;
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
      const source = String(citation.url || citation.source || "").trim();
      if (!source) continue;
      const url = /^https?:\/\//i.test(source) ? source : undefined;
      push({
        id: `citation:${source}`,
        label: citation.title || citation.label || (url ? sourceLabel(url) : "Provider location"),
        ...(url ? { url } : {}),
        detail: citation.label || (url ? sourceLabel(url) : source),
        messageId: message.id,
      });
    }
  }

  return items.slice(-6).reverse();
};

export const collectAttachments = (
  messages: ChatMessage[],
  ownerConversationId?: string,
): ContextAttachment[] => {
  const items: Omit<ContextAttachment, "relatedCount">[] = [];
  const indexes = new Map<string, number>();
  const conversationId = String(ownerConversationId || "").trim() || undefined;
  const upsert = (item: Omit<ContextAttachment, "relatedCount">): void => {
    const existingIndex = indexes.get(item.id);
    if (existingIndex === undefined) {
      indexes.set(item.id, items.length);
      items.push(item);
      return;
    }
    const existing = items[existingIndex];
    const mergedKind = existing.kind === "image" || item.kind === "image"
      ? "image"
      : existing.kind || item.kind;
    items[existingIndex] = {
      ...existing,
      kind: mergedKind,
      label: isPlaceholderAttachmentLabel(existing.label) ? item.label : existing.label,
      artifactId: existing.artifactId || item.artifactId,
      docId: existing.docId || item.docId,
      path: existing.path || item.path,
      mediaType: existing.mediaType || item.mediaType,
      previewUrl: existing.previewUrl || item.previewUrl,
      generated: Boolean(existing.generated || item.generated),
      source: existing.source || item.source,
      conversationId: existing.conversationId || item.conversationId,
    };
  };
  for (const message of messages) {
    for (const attachment of message.attachmentRefs ?? []) {
      const resourceId = attachment.artifactId || attachment.docId || attachment.id || `${message.id}:${attachment.name}`;
      if (!resourceId) continue;
      upsert({
        id: `attachment:${resourceId}`,
        label: attachment.name || "附件",
        kind: attachment.kind,
        source: "attachment",
        messageId: message.id,
        artifactId: attachment.artifactId,
        docId: attachment.docId,
        mediaType: attachment.mediaType,
        previewUrl: inlineImageResourceUrl(attachment.dataUrl),
        conversationId,
      });
    }
    for (const attachment of message.replyAttachments ?? []) {
      const id = `reply:${attachment.path || `${message.id}:${items.length}`}`;
      upsert({
        id,
        label: shortPath(attachment.path),
        kind: attachment.isImage ? "image" : "file",
        source: "workspace",
        messageId: message.id,
        path: attachment.path,
        mediaType: attachment.isImage ? "image/*" : undefined,
        conversationId,
      });
    }
    for (const rawArtifact of message.artifacts ?? []) {
      const artifact = normalizeArtifactPreview(rawArtifact);
      const resourceId = artifact.artifactId || `${message.id}:${items.length}`;
      if (!resourceId) continue;
      const kind = canonicalArtifactKind(artifact.kind, artifact.mediaType);
      const mediaType = artifactMediaTypeForProjection(artifact.mediaType, kind);
      upsert({
        id: `artifact:${resourceId}`,
        label: cleanArtifactLabel(artifact.summary) || (kind === "image" ? "生成图片" : "生成文件"),
        kind: kind === "image" ? "image" : "file",
        source: "artifact",
        messageId: message.id,
        artifactId: artifact.artifactId,
        mediaType,
        previewUrl: inlineImageResourceUrl(artifact.url),
        generated: true,
        conversationId,
      });
    }
    // Older transcripts keep browser screenshots only on the tool record.
    // Project those records through the same canonical artifact path so the
    // context card does not disagree with Activity/Artifacts/LiveArtifacts.
    for (const record of getToolCallsFromMessage(message)) {
      const item = contextAttachmentFromToolRecord(message.id, record, conversationId);
      if (item) upsert(item);
    }
  }
  const groupCounts = new Map<string, number>();
  for (const item of items) groupCounts.set(item.messageId, (groupCounts.get(item.messageId) ?? 0) + 1);
  return items.slice(-8).reverse().map((item) => ({
    ...item,
    relatedCount: groupCounts.get(item.messageId) ?? 1,
  }));
};

const contextAttachmentFromToolRecord = (
  messageId: string,
  record: ToolCallRecord,
  conversationId?: string,
): Omit<ContextAttachment, "relatedCount"> | null => {
  const artifactId = String(record.artifactId || "").trim();
  if (!artifactId) return null;
  const kind = canonicalArtifactKind(record.artifactKind, record.artifactMediaType, record);
  const mediaType = artifactMediaTypeForProjection(record.artifactMediaType, kind);
  return {
    id: `artifact:${artifactId}`,
    label: artifactSummaryForRecord(record),
    kind: kind === "image" ? "image" : "file",
    source: "artifact",
    messageId,
    artifactId,
    mediaType,
    previewUrl: "",
    generated: true,
    conversationId: String(conversationId || "").trim() || undefined,
  };
};

const isPlaceholderAttachmentLabel = (value: string): boolean => {
  const label = cleanArtifactLabel(value);
  return !label || ["附件", "生成图片", "生成文件", "未命名产物"].includes(label);
};

const artifactDomTarget = (
  artifactId: string,
  conversationId?: string,
): HTMLElement | undefined => {
  const candidates = Array.from(document.querySelectorAll<HTMLElement>("[data-artifact-id]"))
    .filter((element) => element.dataset.artifactId === artifactId);
  if (!candidates.length) return undefined;
  const owner = conversationId?.trim() || "";
  if (!owner) return candidates.length === 1 ? candidates[0] : undefined;

  const scoped = candidates.find((element) =>
    element.dataset.artifactConversationId === owner,
  );
  if (scoped) return scoped;

  // Old transcript DOM nodes did not carry an owner. Keep that compatibility
  // path only when no owner-tagged candidate exists and the id is unambiguous.
  const hasOwnerTaggedCandidate = candidates.some((element) =>
    Boolean(element.dataset.artifactConversationId),
  );
  const unscoped = candidates.filter((element) => !element.dataset.artifactConversationId);
  return !hasOwnerTaggedCandidate && unscoped.length === 1 ? unscoped[0] : undefined;
};

function ContextAttachmentThumbnail({ attachment }: { attachment: ContextAttachment }) {
  const isConnected = useAppStore((state) => state.isConnected);
  const sessionId = isConnected ? String(getWebSocket()?.sessionId || "").trim() : "";
  const artifactId = String(attachment.artifactId || "").trim();
  const ownerConversationId = String(attachment.conversationId || "").trim();
  const [reloadNonce, setReloadNonce] = useState(0);
  const [loadState, setLoadState] = useState<"loading" | "loaded" | "error">("loading");
  const ownerScoped = attachment.source === "artifact" || attachment.source === "attachment";
  const baseUrl = useMemo(() => ownerScoped
    ? artifactImageResourceUrl({
        artifactId,
        conversationId: ownerConversationId,
        sessionId,
        source: attachment.source,
        originalUrl: attachment.previewUrl,
        isConnected,
      })
    : String(attachment.previewUrl || "").trim(), [
      artifactId,
      attachment.previewUrl,
      attachment.source,
      isConnected,
      ownerConversationId,
      ownerScoped,
      sessionId,
    ]);

  useEffect(() => {
    setReloadNonce(0);
    setLoadState("loading");
  }, [artifactId, attachment.id, baseUrl, isConnected, ownerConversationId, sessionId]);

  const imageUrl = withPreviewCacheBust(baseUrl, reloadNonce);

  const retry = (event: MouseEvent<HTMLSpanElement>) => {
    event.stopPropagation();
    setLoadState("loading");
    setReloadNonce((value) => value + 1);
  };
  const retryWithKeyboard = (event: KeyboardEvent<HTMLSpanElement>) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    event.stopPropagation();
    setLoadState("loading");
    setReloadNonce((value) => value + 1);
  };

  if (!imageUrl || loadState === "error") {
    const unavailableLabel = !ownerConversationId && ownerScoped
      ? "未关联会话"
      : ownerScoped && (!isConnected || !sessionId)
        ? "重连后载入"
        : loadState === "error"
          ? "加载失败"
          : "暂无预览";
    return (
      <span className="mc-chat-context-image-fallback" role={loadState === "error" ? "status" : undefined}>
        <FileImage size={15} aria-hidden="true" />
        <span className="mc-chat-context-image-error-label">{unavailableLabel}</span>
        {loadState === "error" && (
          <>
            <span
              className="mc-chat-context-image-retry"
              role="button"
              tabIndex={0}
              aria-label="重试图片"
              onClick={retry}
              onKeyDown={retryWithKeyboard}
            >
              重试
            </span>
          </>
        )}
      </span>
    );
  }

  return (
    <img
      key={`${imageUrl}:${reloadNonce}`}
      src={imageUrl}
      alt={attachment.label}
      data-load-state={loadState}
      onLoad={() => setLoadState("loaded")}
      onError={() => setLoadState("error")}
    />
  );
}

export const ChatContextCard = () => {
  const contextInputs = useAppStore(useShallow((state) => state.messages.flatMap((message) => [
    message.id, message.citations, message.attachmentRefs, message.replyAttachments, message.artifacts,
    ...getToolCallsFromMessage(message),
  ])));
  const subagents = useAppStore((state) => state.subagents);
  const backgroundTasks = useAppStore((state) => state.backgroundTasks);
  const conversationId = useAppStore((state) => state.conversationId);
  const workingDirectory = useAppStore((state) => state.workingDirectory);
  const workspaceGit = useAppStore((state) => state.workspaceGit);
  const { summary: changes, openReview } = useTurnChanges();
  const rightPanelOpen = useAppStore((state) => state.rightPanelOpen);
  const setRightStackTab = useAppStore((state) => state.setRightStackTab);
  const setFocusedSubagentId = useAppStore((state) => state.setFocusedSubagentId);
  const [browserTargets, setBrowserTargets] = useState<EmbeddedBrowserState[]>([]);
  const [collapsed, setCollapsed] = useState(false);
  const [cardPresence, setCardPresence] = useState<"visible" | "exiting" | "preparing" | "hidden">(
    rightPanelOpen ? "hidden" : "visible",
  );
  const previousRightPanelOpenRef = useRef(rightPanelOpen);
  const agentViews = useMemo(() => projectAgentViews(subagents), [subagents]);
  // Text-only deltas do not change context. Keep collection and thumbnail
  // rendering off that path while still responding to new artifacts/tools.
  const { sources, attachments } = useMemo(() => {
    const messages = useAppStore.getState().messages;
    return { sources: collectSources(messages), attachments: collectAttachments(messages, conversationId || undefined) };
  }, [conversationId, contextInputs]);

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
      if (!conversationId) return;
      void Promise.resolve(embeddedBrowserList(conversationId)).then((targets) => {
        if (!cancelled && Array.isArray(targets)) {
          setBrowserTargets(targets.filter((target) => target.url && target.url !== "about:blank"));
        }
      });
    };
    refresh();
    const unsubscribe = onEmbeddedBrowserEvent((event) => {
      if (event.conversationId !== conversationId) return;
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
  }, [conversationId]);

  useEffect(() => {
    if (!rightPanelOpen && isDesktop()) {
      if (!conversationId) return;
      void Promise.resolve(embeddedBrowserList(conversationId)).then((targets) => {
        if (Array.isArray(targets)) {
          setBrowserTargets(targets.filter((target) => target.url && target.url !== "about:blank"));
        }
      });
    }
  }, [conversationId, rightPanelOpen]);

  const activeAgents = agentViews.filter((agent) => agent.status !== "completed");
  const completedAgents = agentViews.filter((agent) => agent.status === "completed");
  const scopedBackgroundTasks = backgroundTasks.filter((task) =>
    Boolean(conversationId) && task.conversationId === conversationId
  );
  const stalledBackgroundTasks = scopedBackgroundTasks.filter((task) => task.status === "stalled").length;
  const runningBackgroundTasks = scopedBackgroundTasks.filter((task) => task.status === "running").length;
  const failedBackgroundTasks = scopedBackgroundTasks.filter((task) => task.status === "failed").length;
  const cancelledBackgroundTasks = scopedBackgroundTasks.filter((task) => task.status === "cancelled").length;
  const backgroundTaskSummary = stalledBackgroundTasks > 0
    ? `${stalledBackgroundTasks} 个等待输入`
    : runningBackgroundTasks > 0
      ? `${runningBackgroundTasks} 个运行中`
      : failedBackgroundTasks > 0
        ? `${failedBackgroundTasks} 个失败`
        : cancelledBackgroundTasks > 0
          ? "已取消"
          : "已完成";
  const hasBackgroundTasks = scopedBackgroundTasks.length > 0;
  const hasWorkspace = Boolean(conversationId && workingDirectory);
  const contextCount = attachments.length + sources.length + browserTargets.length + agentViews.length + (hasBackgroundTasks ? 1 : 0) + (hasWorkspace ? 1 : 0) + (changes ? 1 : 0);

  const openPanel = (tab: RightStackTab) => setRightStackTab(tab);
  const openAgent = (agentId?: string) => {
    setFocusedSubagentId(agentId ?? null);
    setRightStackTab("subagents");
  };
  const openBrowserTarget = (target?: EmbeddedBrowserState) => {
    if (target?.id && conversationId) void embeddedBrowserActivate(conversationId, target.id);
    setRightStackTab("browser");
  };
  const openAttachment = (attachment: ContextAttachment) => {
    const store = useAppStore.getState();
    const attachmentConversationId = attachment.conversationId?.trim() || undefined;
    if (attachment.source === "artifact" && attachment.artifactId) {
      const target = artifactDomTarget(attachment.artifactId, attachmentConversationId);
      if (target) {
        target.scrollIntoView?.({ behavior: "smooth", block: "center" });
        target.classList.add("assistant-cell-artifact-context-target");
        window.setTimeout(() => target.classList.remove("assistant-cell-artifact-context-target"), 1200);
        return;
      }
    }
    if (attachment.path) {
      openWorkspaceFilePreview({
        path: attachment.path,
        name: attachment.label,
        mediaType: attachment.mediaType,
        kind: attachment.kind,
        workspaceRoot: store.workingDirectory || "",
        conversationId: attachmentConversationId || store.conversationId || undefined,
      });
      return;
    }
    if (attachment.artifactId) {
      // Generated artifacts are stored in ArtifactStore and must be read via
      // the artifact endpoint.  Treating them as uploads sends the request to
      // `/api/attachments/raw`, which cannot resolve browser screenshots.
      if (attachment.source === "artifact") {
        if (!attachmentConversationId) return;
        openArtifactPreview({
          artifactId: attachment.artifactId,
          name: attachment.label,
          summary: attachment.label,
          mediaType: attachment.mediaType,
          kind: attachment.kind,
          conversationId: attachmentConversationId,
        });
        return;
      }
      if (!attachmentConversationId) return;
      openAttachmentPreview({
        artifactId: attachment.artifactId,
        name: attachment.label,
        mediaType: attachment.mediaType,
        kind: attachment.kind,
        conversationId: attachmentConversationId,
      });
      return;
    }
    store.setRightStackTab("artifacts");
  };
  const openBackgroundTasks = () => useAppStore.getState().setRightStackTab("tasks");

  if (contextCount === 0 || cardPresence === "hidden") return null;

  return (
    <aside
      className="mc-chat-context-card"
      data-state={cardPresence}
      data-collapsed={collapsed ? "true" : "false"}
      aria-label="工作区上下文摘要"
    >
      <button
        type="button"
        className="mc-chat-context-card-compact"
        aria-label={collapsed ? "展开上下文卡片" : "打开上下文详情"}
        title={collapsed ? "展开上下文卡片" : "打开上下文详情"}
        onClick={() => collapsed ? setCollapsed(false) : openPanel("tasks")}
      >
        <PanelRightOpen size={18} strokeWidth={1.8} />
      </button>

      <header className="mc-chat-context-card-header" hidden={collapsed}>
        <span>{hasWorkspace || changes ? "环境信息" : "上下文"}</span>
        <span className="mc-chat-context-card-header-actions">
          <button type="button" aria-label="打开上下文详情" title="打开上下文详情" onClick={() => openPanel("tasks")}>
            <PanelRightOpen size={16} strokeWidth={1.8} />
          </button>
          <button type="button" aria-label="收起上下文卡片" title="收起上下文卡片" onClick={() => setCollapsed(true)}>
            <ChevronRight size={16} strokeWidth={1.8} />
          </button>
        </span>
      </header>

      <div className="mc-chat-context-card-body" hidden={collapsed}>
        {(hasWorkspace || changes) && <section className="mc-chat-context-card-section" aria-label="环境信息">
          {changes && <button type="button" className="mc-chat-context-environment-row" onClick={openReview} title="审阅本轮文件更改">
            <FileDiff size={17} aria-hidden="true" />
            <span>变更</span>
            <span className="mc-chat-context-change-stats"><span className="chat-change-added">+{changes.additions}</span><span className="chat-change-deleted">-{changes.deletions}</span></span>
            <ChevronRight size={14} aria-hidden="true" />
          </button>}
          {hasWorkspace && <>
            <div className="mc-chat-context-environment-row"><Monitor size={17} aria-hidden="true" /><span>{workspaceGit?.isWorktree ? "独立工作树" : "本地工作区"}</span></div>
            <div className="mc-chat-context-environment-row" title={workingDirectory}><Folder size={17} aria-hidden="true" /><span>{shortPath(workingDirectory)}</span></div>
            {workspaceGit?.branch && <button type="button" className="mc-chat-context-environment-row" onClick={() => openPanel("diff")} title={workspaceGit.branch}>
              <GitBranch size={17} aria-hidden="true" /><span>{workspaceGit.branch}</span><ChevronRight size={14} aria-hidden="true" />
            </button>}
          </>}
        </section>}
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
                  <span>
                    {attachment.kind === "image"
                      ? <ContextAttachmentThumbnail attachment={attachment} />
                      : <Icon size={15} />}
                  </span>
                  <span className="mc-chat-context-source-content">
                    <span>{attachment.label}</span>
                    <small>
                      {attachment.relatedCount > 1
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
          {sources.slice(0, 3).map((source) => source.url ? (
            <button
              key={source.id}
              type="button"
              className="mc-chat-context-source"
              aria-label={`打开来源：${source.label}`}
              title={source.url}
              onClick={() => openWebTarget(source.url!)}
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
                <small>{source.detail}</small>
              </span>
            </button>
          ) : (
            <div
              key={source.id}
              className="mc-chat-context-source"
              role="note"
              title={source.detail}
            >
              <span><FileText size={15} aria-hidden="true" /></span>
              <span className="mc-chat-context-source-content">
                <span>{source.label}</span>
                <small>{source.detail}</small>
              </span>
            </div>
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
            <button type="button" className="mc-chat-context-task-row" onClick={openBackgroundTasks}>
              <ListChecks size={16} /> 查看后台任务
            </button>
          </section>
        )}
      </div>
    </aside>
  );
};
