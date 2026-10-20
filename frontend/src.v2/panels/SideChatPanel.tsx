import { useI18n } from "../i18n";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowUp, AtSign, LoaderCircle, MessageCirclePlus, Plus, Square, X } from "lucide-react";
import { useAppStore } from "../stores";
import type { CommandResultEvent } from "../protocol/events";
import { attachmentRefFromPayload, sendChatMessage } from "../chat/sendChatMessage";
import { captureSteerTurnId } from "../lib/steer-command";
import type { CodeSelectionContext, FollowUpBehavior, MessageContextRef, PermissionMode } from "../stores/types";
import { contextReferenceLabel, openContextReference } from "../chat/contextReferenceActions";
import { AttachmentStrip } from "../composer/AttachmentStrip";
import { ComposerTextarea } from "../composer/ComposerTextarea";
import { hasRichComposerContent } from "../composer/rich-composer-document";
import { appendComposerTokenAnchor, composerTokenAtSelection, removeComposerToken, type ComposerSelection } from "../composer/inputSelection";
import { MenuOverlay } from "../composer/MenuOverlay";
import { QueuedMessageList } from "../composer/QueuedMessageList";
import { cancelComposerUpload, uploadComposerFiles } from "../composer/uploads";
import { buildContextNativeAttachments, buildContextPayload } from "../composer/contextPayload";
import { getWebSocket } from "../hooks/useWebSocket";
import { pushToast } from "../overlays/ToastContainer";
import { formatModelLabel } from "../lib/model-label";
import { ChatTurn } from "../chat/components/ChatTurn";
import { projectMessagesToTurns } from "../chat/chatSurfaceState";
import { applyAuthoritativeTurnDiff } from "../lib/turn-diff";
import { InlineAgentPrompt } from "../chat/InlineAgentPrompt";
import { toBackendPermissionMode } from "../protocol/permissions";
import {
  commandResultSucceeded,
  sendClientCommand,
  sendClientCommandAwaitResult,
  sendConversationDeleteCommand,
} from "../protocol/ws-outbox";
import { buildInterruptCommand } from "../lib/interrupt-command";
import { releasePreviewScope } from "../chat/previewRequestScope";
import { hasLocalPendingPromptForConversation } from "../lib/pending-prompts";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { useReplyViewport } from "../chat/useReplyViewport";
import { newSideChatId } from "../chat/openSideChat";
import { hydrateMessages } from "../chat/transcriptHydration";
import type { ConversationTranscriptMessage } from "../protocol/conversation-types";
import "./SideChatPanel.css";

type SideChatOwner = { id: string; workspaceRoot: string; permissionMode: PermissionMode; parentConversationId?: string };

export const SideChatPanel = ({ active = true }: { active?: boolean }) => {
  const workspaceRoot = useAppStore((state) => state.workingDirectory);
  const permissionMode = useAppStore((state) => state.permissionMode);
  const parentConversationId = useAppStore((state) => state.conversationId);
  const activeSideChatId = useAppStore((state) => state.activeSideChatId);
  const draftsReady = useAppStore(state => state.composerDraftsReady);
  const [owners, setOwners] = useState<SideChatOwner[]>([]);
  useLayoutEffect(() => {
    if (!draftsReady || !active) return;
    const state = useAppStore.getState();
    const selected = activeSideChatId ? state.sideChats[activeSideChatId] : undefined;
    const owned = selected && workspaceRootsEqual(selected.workspaceRoot, workspaceRoot)
      && (selected.parentConversationId === parentConversationId || selected.parentConversationId == null) ? selected : undefined;
    const saved = owned ?? Object.values(state.sideChats).find(thread => workspaceRootsEqual(thread.workspaceRoot, workspaceRoot)
      && (thread.parentConversationId === parentConversationId || thread.parentConversationId == null)
      && (thread.draft || thread.attachments?.length || thread.contextRefs?.length || thread.selectedContext || thread.isStreaming));
    const id = saved?.id ?? newSideChatId();
    if (!saved) state.ensureSideChat(id, { workspaceRoot, permissionMode, parentConversationId: parentConversationId ?? undefined });
    if (!owners.some(owner => owner.id === id)) setOwners(current => [...current, { id, workspaceRoot,
      permissionMode: saved?.permissionMode ?? permissionMode, parentConversationId: saved ? saved.parentConversationId : parentConversationId ?? undefined }]);
    if (activeSideChatId !== id) useAppStore.setState({ activeSideChatId: id });
  }, [active, owners, workspaceRoot, permissionMode, draftsReady, parentConversationId, activeSideChatId]);

  return <div style={{ display: "flex", flex: 1, minHeight: 0, minWidth: 0 }}>
    {owners.map((owner) => {
      const selected = owner.id === activeSideChatId;
      return <div key={owner.id} hidden={!selected} data-side-chat-workspace={owner.workspaceRoot}
        style={{ display: selected ? "flex" : "none", flex: 1, minHeight: 0, minWidth: 0 }}>
        <SideChatThreadPanel owner={owner} active={active && selected} />
      </div>;
    })}
  </div>;
};

const SideChatThreadPanel = ({ active, owner }: { active: boolean; owner: SideChatOwner }) => {
  const { t: tr } = useI18n();
  const sideChats = useAppStore((s) => s.sideChats);
  const ensureSideChat = useAppStore((s) => s.ensureSideChat);
  const removeSideChat = useAppStore((s) => s.removeSideChat);
  const setDraft = useAppStore((s) => s.setSideChatDraft);
  const isConnected = useAppStore((s) => s.isConnected);
  const workingDirectory = owner.workspaceRoot;
  const permissionMode = owner.permissionMode;
  const sendShortcut = useAppStore((s) => s.sendShortcut);

  const id = owner.id;
  const thread = sideChats[id];
  const waitingForInput = useAppStore((s) => hasLocalPendingPromptForConversation([
    s.pendingApproval, ...s.approvalQueue, s.pendingAskUser, ...s.askUserQueue,
    s.pendingDiffReview, ...s.diffReviewQueue,
  ], id));
  const hasPendingApproval = useAppStore((s) => hasLocalPendingPromptForConversation([
    s.pendingApproval, ...s.approvalQueue, s.pendingDiffReview, ...s.diffReviewQueue,
  ], id));
  const submittedContextRef = useRef<CodeSelectionContext>();
  const hasPendingContext = Boolean(thread?.selectedContext && thread.selectedContext !== submittedContextRef.current);
  const [submitting, setSubmitting] = useState(false);
  const [mentionFilter, setMentionFilter] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const composerRef = useRef<HTMLDivElement>(null);
  const inputSelectionRef = useRef<ComposerSelection>({ start: 0, end: 0 });
  const [selectionRequest, setSelectionRequest] = useState<ComposerSelection>();
  const turnDiff = useAppStore((s) => s.turnDiffs[id]);
  const turns = useMemo(() => thread
    ? applyAuthoritativeTurnDiff(projectMessagesToTurns(thread.messages, thread.isStreaming, workingDirectory), turnDiff)
    : [], [thread?.messages, thread?.isStreaming, turnDiff, workingDirectory]);
  const createdOnServerRef = useRef(false);
  const deleteRequestedRef = useRef(false);
  const createInFlightRef = useRef<Promise<void> | null>(null);
  const mountedRef = useRef(true);
  const [serverReady, setServerReady] = useState(false);
  const [creationError, setCreationError] = useState("");
  const [creationAttempt, setCreationAttempt] = useState(0);

  const cleanupServerConversation = () => {
    const saved = useAppStore.getState().sideChats[id];
    if (saved && (saved.draft || saved.attachments?.length || saved.contextRefs?.length || saved.selectedContext || saved.isStreaming)) return;
    if (!createdOnServerRef.current || deleteRequestedRef.current) return;
    deleteRequestedRef.current = true;
    void sendConversationDeleteCommand({
      type: "conversation.delete",
      conversation_id: id,
    });
  };

  useEffect(() => {
    mountedRef.current = true;
    ensureSideChat(id, { workspaceRoot: workingDirectory, permissionMode, parentConversationId: owner.parentConversationId });
    return () => {
      mountedRef.current = false;
      const saved = useAppStore.getState().sideChats[id];
      if (saved && (saved.draft || saved.attachments?.length || saved.contextRefs?.length || saved.selectedContext || saved.isStreaming)) return;
      for (const attachment of useAppStore.getState().sideChats[id]?.attachments ?? []) {
        cancelComposerUpload(attachment.id);
        if (attachment.dataUrl?.startsWith("blob:")) URL.revokeObjectURL?.(attachment.dataUrl);
      }
      cleanupServerConversation();
      releasePreviewScope(id);
      const state = useAppStore.getState();
      if (state.previewOwnerConversationId === id) {
        state.restorePreviewState(state.conversationId || undefined);
      }
      removeSideChat(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {
    if (!isConnected || createdOnServerRef.current || createInFlightRef.current) return;
    setCreationError("");
    const create = async () => {
      try {
        const result = await sendClientCommandAwaitResult({
          type: "conversation.create",
          conversation_id: id,
          title: tr("侧边对话"),
          conversation_type: "side_chat",
          ...(owner.parentConversationId ? { source_conversation_id: owner.parentConversationId } : {}),
          workspace_root: workingDirectory || undefined,
          permission_mode: toBackendPermissionMode(permissionMode),
          activate: false,
        }, "conversation.create");
        if (!commandResultSucceeded(result)) throw new Error(result.message || tr("无法创建侧边对话。"));
        if (result.data?.conversation_id !== id) throw new Error(tr("服务器没有确认当前侧边对话的标识。"));
        if (result.data.side_chat_transcript !== undefined) {
          const restored = hydrateMessages(result.data.side_chat_transcript as ConversationTranscriptMessage[]);
          useAppStore.setState(state => {
            const saved = state.sideChats[id];
            if (!saved) return state;
            const live = new Map(saved.messages.map(message => [message.id, message]));
            const restoredIds = new Set(restored.map(message => message.id));
            return { sideChats: { ...state.sideChats, [id]: { ...saved, messages: [
              ...restored.map(message => live.get(message.id)?.isStreaming ? live.get(message.id)! : message),
              ...saved.messages.filter(message => !restoredIds.has(message.id)),
            ] } } };
          });
        }
        createdOnServerRef.current = true;
        if (mountedRef.current) {
          setServerReady(true);
        } else {
          cleanupServerConversation();
        }
      } catch (error) {
        if (mountedRef.current) setCreationError(error instanceof Error ? error.message : String(error));
      } finally {
        createInFlightRef.current = null;
      }
    };
    createInFlightRef.current = create();
  }, [id, isConnected, workingDirectory, permissionMode, creationAttempt]);

  const listRef = useRef<HTMLDivElement>(null);
  const isFollowingRef = useRef(true);
  const [showScrollButton, setShowScrollButton] = useState(false);
  const followLatest = useCallback(() => {
    if (active && isFollowingRef.current && listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [active]);
  useReplyViewport(listRef, followLatest, Boolean(thread));
  useLayoutEffect(followLatest, [active, thread?.messages, thread?.isStreaming]);

  useEffect(() => {
    if (active) window.dispatchEvent(new Event(`composer:focus:${id}`));
  }, [active, thread?.selectedContext?.text]);

  const selectionRef: MessageContextRef | undefined = hasPendingContext && thread?.selectedContext?.source ? {
    kind: "file", name: thread.selectedContext.source.split(/[/\\]/).pop()!, path: thread.selectedContext.source,
    range: thread.selectedContext.range, text: thread.selectedContext.text, workspaceRoot: thread.selectedContext.workspaceRoot,
  } : undefined;

  const submit = () => submitWithBehavior(useAppStore.getState().followUpBehavior);
  const submitAlternateFollowUp = () => submitWithBehavior(useAppStore.getState().followUpBehavior === "queue" ? "steer" : "queue");
  const submitWithBehavior = async (busyBehavior: FollowUpBehavior) => {
    const expectedTurnId = busyBehavior === "steer" ? captureSteerTurnId(useAppStore.getState(), id) : undefined;
    const content = hasRichComposerContent(thread?.richDraft, thread?.draft) ? thread!.draft.trim() : "";
    if (!thread || submitting || waitingForInput || !isConnected || !serverReady) return;
    const attachments = thread.attachments ?? [];
    if (!content && !attachments.length && !thread.contextRefs?.length && !hasPendingContext) return;
    if (attachments.some((attachment) => attachment.status !== "ready")) {
      pushToast(tr("请等待附件上传完成，或重试 / 移除失败的附件。"), "warning");
      return;
    }
    const selectedPrefix = hasPendingContext && !selectionRef && thread.selectedContext?.text
      ? `Selected context${thread.selectedContext.source ? ` (${thread.selectedContext.source})` : ""}:\n\n${thread.selectedContext.text}\n\n`
      : "";
    const contextRefs = [...(thread.contextRefs ?? []), ...(selectionRef ? [selectionRef] : [])];
    setSubmitting(true);
    try {
      const accepted: { result?: CommandResultEvent } = {};
      const nativeContext = await buildContextNativeAttachments(contextRefs, getWebSocket()?.sessionId, id, workingDirectory);
      const prefix = await buildContextPayload(contextRefs);
      if (!mountedRef.current) return;
      const sent = await sendChatMessage({
        displayContent: content,
        backendContent: [selectedPrefix, prefix, nativeContext.notes, content].filter(Boolean).join("\n\n"),
        conversationId: id,
        primaryFile: thread.selectedContext?.source,
        attachments: [...attachments.map((attachment) => attachment.attachment!), ...nativeContext.attachments],
        attachmentRefs: [...attachments.map((attachment) => ({ ...attachmentRefFromPayload(attachment.attachment!)!, dataUrl: attachment.dataUrl })), ...nativeContext.attachmentRefs],
        contextRefs,
        allowWhileStreaming: thread.isStreaming,
        busyBehavior,
        expectedTurnId,
        awaitAdmission: true,
        onAdmission: event => { accepted.result = event; },
      });
      if (sent) {
        if (accepted.result?.data?.native_completion === "completed") {
          useAppStore.setState(state => {
            const current = state.sideChats[id];
            return current?.draft === thread.draft && current.richDraft === thread.richDraft ? { sideChats: { ...state.sideChats,
              [id]: { ...current, draft: "", richDraft: undefined },
            } } : state;
          });
          return;
        }
        for (const attachment of attachments) if (attachment.dataUrl?.startsWith("blob:")) URL.revokeObjectURL?.(attachment.dataUrl);
        submittedContextRef.current = thread.selectedContext;
        isFollowingRef.current = true;
        setShowScrollButton(false);
        useAppStore.setState((state) => {
          const current = state.sideChats[id];
          if (!current) return state;
          return { sideChats: { ...state.sideChats, [id]: { ...current,
            ...(current.draft === thread.draft && current.richDraft === thread.richDraft ? { draft: "", richDraft: undefined } : {}),
            selectedContext: current.selectedContext === thread.selectedContext ? undefined : current.selectedContext,
            contextRefs: current.contextRefs?.filter((ref) => !contextRefs.includes(ref)),
            attachments: current.attachments?.filter((attachment) => !attachments.includes(attachment)),
          } } };
        });
      }
    } catch (error) {
      pushToast(error instanceof Error ? error.message : tr("侧边消息发送失败，请重试。"), "error");
    } finally { setSubmitting(false); }
  };

  const stop = () => {
    sendClientCommand(buildInterruptCommand(useAppStore.getState(), id));
  };

  const sendDisabled = useMemo(
    () => !thread || submitting || waitingForInput || !isConnected || !serverReady
      || (!hasRichComposerContent(thread.richDraft, thread.draft) && !thread.attachments?.length && !thread.contextRefs?.length && !hasPendingContext),
    [isConnected, serverReady, thread, waitingForInput, submitting, hasPendingContext],
  );

  const updateContext = (refs: MessageContextRef[]) => useAppStore.setState((state) => ({ sideChats: {
    ...state.sideChats, [id]: { ...state.sideChats[id], contextRefs: refs },
  } }));
  const attachFiles = (files: File[]) => {
    if (!serverReady) { pushToast(tr("侧边对话正在准备，完成后即可添加附件。"), "info"); return; }
    uploadComposerFiles(files, id);
  };
  const onMentionSelect = (value: string) => {
    setMentionFilter(null);
    if (!value) return;
    const token = composerTokenAtSelection(thread?.draft ?? "", inputSelectionRef.current);
    const plugin = value.match(/^plugin:(.+)$/)?.[1];
    const typed = value.match(/^(file|folder):(.*)$/);
    const path = appendComposerTokenAnchor(typed?.[2] ?? value, token?.value ?? "");
    const configName = plugin ? decodeURIComponent(plugin) : "";
    const ref: MessageContextRef = plugin ? { kind: "plugin", name: configName, configName, path: `plugin://${configName}` }
      : { kind: typed?.[1] === "folder" ? "folder" : "file", name: path.split(/[/\\]/).filter(Boolean).pop() || path, path, workspaceRoot: workingDirectory };
    updateContext([...(thread?.contextRefs ?? []).filter((item) => item.path !== ref.path), ref]);
    if (token?.kind === "mention") {
      setDraft(id, removeComposerToken(thread!.draft, token));
      setSelectionRequest({ start: token.start, end: token.start });
    }
  };
  const refreshMentionMenu = (value: string, selection: ComposerSelection) => {
    inputSelectionRef.current = selection;
    const token = composerTokenAtSelection(value, selection);
    setMentionFilter(token?.kind === "mention" ? token.value : null);
  };

  if (!thread) return null;

  return (
    <div className="side-chat-panel chat-pane" data-conversation-id={id} onKeyDown={(event) => {
      if (event.defaultPrevented || event.key !== "Escape" || event.nativeEvent.isComposing || event.keyCode === 229) return;
      event.stopPropagation();
      if (thread.isStreaming) {
        event.preventDefault();
        stop();
      }
    }}>
      <div className="side-chat-transcript-region">
        <div
          ref={listRef}
          className="side-chat-transcript"
          role="log"
          aria-label={tr("侧边对话历史")}
          tabIndex={0}
          onScroll={(event) => {
            const list = event.currentTarget;
            const gap = list.scrollHeight - list.scrollTop - list.clientHeight;
            isFollowingRef.current = gap <= 64;
            setShowScrollButton(gap > 128);
          }}
          onLoadCapture={followLatest}
        >
          {thread.messages.length === 0 ? (
            <div className="side-chat-empty">
              <MessageCirclePlus size={32} strokeWidth={1.5} aria-hidden="true" />
              <h2>{tr("侧边聊天")}</h2>
              <p>{tr("临时聊天。关闭此标签后会清除，主任务保持不变。")}</p>
            </div>
          ) : (
            turns.map((turn, index) => <div key={turn.id}
              data-reply-viewport={index === turns.length - 1 && turn.userCell ? "true" : "false"}
              style={{ flex: "0 0 auto", minHeight: index === turns.length - 1 && turn.userCell ? "var(--reply-viewport-height)" : undefined }}>
              <ChatTurn turn={turn} conversationId={id} workspaceRoot={workingDirectory} />
            </div>)
          )}
        </div>
        {showScrollButton && <button type="button" className="side-chat-follow-latest" aria-label={tr("回到侧边对话最新消息")} onClick={() => {
          isFollowingRef.current = true;
          setShowScrollButton(false);
          listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
        }}><ArrowDown size={18} aria-hidden="true" /></button>}
      </div>
      <div className="side-chat-composer-region" data-approval-replacement={hasPendingApproval ? "true" : "false"}>
        <QueuedMessageList ownerId={id} minimal />
        <div className="side-chat-prompt-region"><InlineAgentPrompt conversationId={id} /></div>
        <div ref={composerRef} className="side-chat-composer composer-container" data-dictation-side-chat={id} data-command-mode="false" hidden={hasPendingApproval}>
          {creationError && <div role="alert" className="side-chat-creation-error">
            <span>{creationError}</span>
            <button type="button" disabled={!isConnected} onClick={() => setCreationAttempt((attempt) => attempt + 1)}>{tr("重试创建")}</button>
          </div>}
          {thread.selectedContext && hasPendingContext && <details className="side-chat-selected-context">
            <summary>{selectionRef ? contextReferenceLabel(selectionRef) : tr("所选内容")} · {thread.selectedContext.text.length.toLocaleString()} {tr("字符")}</summary>
            <div className="side-chat-context-actions">
              {selectionRef && <button type="button" onClick={() => openContextReference(selectionRef, id)}>{tr("返回原选区")}</button>}
              <button type="button" onClick={() => useAppStore.setState((state) => ({ sideChats: { ...state.sideChats, [id]: { ...state.sideChats[id], selectedContext: undefined } } }))}>{tr("移除引用")}</button>
            </div>
            <pre>{thread.selectedContext.text}</pre>
          </details>}
          <div className="side-chat-context-chips">{thread.contextRefs?.map((ref) => <span key={ref.path}>
            <button type="button" onClick={() => openContextReference(ref, id)} title={ref.path}>{contextReferenceLabel(ref)}</button>
            <button type="button" aria-label={tr("移除 {v0}", { v0: ref.name })} onClick={() => updateContext(thread.contextRefs!.filter((item) => item !== ref))}><X size={12} /></button>
          </span>)}</div>
          <AttachmentStrip conversationId={id} />
          <ComposerTextarea rich richDocument={thread.richDraft} documentKey={id} value={thread.draft} conversationId={id} ariaLabel={tr("侧边对话消息")}
            onChange={(value, selection = { start: value.length, end: value.length }, rich) => { setDraft(id, value, rich); refreshMentionMenu(value, selection); }}
            onSelectionChange={refreshMentionMenu}
            selectionRequest={selectionRequest}
            onSubmit={submit} onAlternateSubmit={submitAlternateFollowUp} isResponseInProgress={thread.isStreaming}
            menuOpen={mentionFilter !== null} onDropFiles={attachFiles}
            onEscape={() => { if (thread.isStreaming) { stop(); return true; } return false; }}
            placeholder={thread.isStreaming ? tr("补充要求，将加入队列…") : tr("描述任务或提出问题…")} />
          <MenuOverlay open={mentionFilter !== null && !hasPendingApproval} kind="mention" filter={mentionFilter ?? ""} workspaceRoot={workingDirectory} onSelect={onMentionSelect} />
          <div className="side-chat-composer-footer composer-footer">
            <input type="file" ref={fileInputRef} multiple hidden onChange={(event) => { attachFiles(Array.from(event.target.files ?? [])); event.target.value = ""; }} />
            <button type="button" className="composer-attach-btn" aria-label={tr("添加附件")} disabled={!serverReady} onClick={() => fileInputRef.current?.click()}><Plus size={17} /></button>
            <button type="button" className="composer-attach-btn" aria-label={tr("添加上下文")} onClick={() => {
              const next = `${thread.draft}${thread.draft && !/\s$/.test(thread.draft) ? " " : ""}@`;
              setDraft(id, next); refreshMentionMenu(next, { start: next.length, end: next.length });
              setSelectionRequest({ start: next.length, end: next.length });
            }}><AtSign size={15} /></button>
            <span className="side-chat-send-hint" title={sendShortcut === "enter" ? tr("Enter 发送 · Shift Enter 换行") : sendShortcut === "cmdIfMultiline" ? tr("单行按 Enter 发送，多行按 Ctrl/Cmd + Enter 发送") : tr("Ctrl Enter 发送")}>{!isConnected ? tr("正在连接…") : !serverReady ? tr("正在准备对话…") : thread.model ? formatModelLabel(thread.model) : tr("会话默认模型")}</span>
            {thread.isStreaming && <button type="button" className="composer-stop-current-btn" aria-label={tr("停止")} onClick={stop}><Square size={12} fill="currentColor" /></button>}
            <button
              type="button"
              onClick={submit}
              disabled={sendDisabled}
              aria-label={thread.isStreaming ? tr("将消息加入队列") : tr("发送")}
              title={thread.isStreaming ? tr("将消息加入队列") : tr("发送")}
              className="composer-send-btn"
            >
              {submitting ? <LoaderCircle size={16} className="animate-spin" /> : <ArrowUp size={18} />}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
