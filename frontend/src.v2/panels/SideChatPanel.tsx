import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowUp, MessageCirclePlus, Square } from "lucide-react";
import { useAppStore } from "../stores";
import type { ProgressContentBlock } from "../stores/types";
import { sendChatMessage } from "../chat/sendChatMessage";
import { AssistantMarkdownCell } from "../chat/cells/AssistantMarkdownCell";
import { ToolCallCard } from "../chat/tool-calls/ToolCallCard";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import { toBackendPermissionMode } from "../protocol/permissions";
import {
  commandResultSucceeded,
  sendClientCommand,
  sendClientCommandAwaitResult,
  sendConversationDeleteCommand,
} from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import { uniqueMessageId } from "../stores/shared-helpers";
import { buildInterruptCommand } from "../lib/interrupt-command";
import { releasePreviewScope } from "../chat/previewRequestScope";

const newSideChatId = (): string =>
  `side-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;

export const SideChatPanel = ({ active = true }: { active?: boolean }) => {
  const sideChats = useAppStore((s) => s.sideChats);
  const ensureSideChat = useAppStore((s) => s.ensureSideChat);
  const removeSideChat = useAppStore((s) => s.removeSideChat);
  const setDraft = useAppStore((s) => s.setSideChatDraft);
  const startMessage = useAppStore((s) => s.startSideChatMessage);
  const isConnected = useAppStore((s) => s.isConnected);
  const workingDirectory = useRef(useAppStore.getState().workingDirectory).current;
  const permissionMode = useRef(useAppStore.getState().permissionMode).current;
  const sendShortcut = useAppStore((s) => s.sendShortcut);

  const idRef = useRef<string>("");
  if (!idRef.current) {
    idRef.current = newSideChatId();
  }
  const id = idRef.current;
  const thread = sideChats[id];
  const createdOnServerRef = useRef(false);
  const deleteRequestedRef = useRef(false);
  const createInFlightRef = useRef<Promise<void> | null>(null);
  const mountedRef = useRef(true);
  const [serverReady, setServerReady] = useState(false);

  const cleanupServerConversation = () => {
    if (!createdOnServerRef.current || deleteRequestedRef.current) return;
    deleteRequestedRef.current = true;
    void sendConversationDeleteCommand({
      type: "conversation.delete",
      conversation_id: id,
    });
  };

  useEffect(() => {
    mountedRef.current = true;
    ensureSideChat(id);
    return () => {
      mountedRef.current = false;
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
    const create = async () => {
      try {
        const result = await sendClientCommandAwaitResult({
          type: "conversation.create",
          conversation_id: id,
          title: "侧边对话",
          conversation_type: "side_chat",
          workspace_root: workingDirectory || undefined,
          permission_mode: toBackendPermissionMode(permissionMode),
        }, "conversation.create");
        if (!commandResultSucceeded(result)) {
          if (mountedRef.current) pushToast(result.message || "无法创建侧边对话。", "error", 4000);
          return;
        }
        createdOnServerRef.current = true;
        if (mountedRef.current) {
          setServerReady(true);
        } else {
          cleanupServerConversation();
        }
      } catch {
        // The command transport already reports offline/timeout failures.
      } finally {
        createInFlightRef.current = null;
      }
    };
    createInFlightRef.current = create();
  }, [id, isConnected, workingDirectory, permissionMode]);

  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [thread?.messages.length, thread?.messages.at(-1)?.content]);

  useEffect(() => {
    if (active) inputRef.current?.focus();
  }, [active, thread?.selectedContext?.text]);

  const submit = () => {
    const content = (thread?.draft ?? "").trim();
    if (!content || !thread || thread.isStreaming || !isConnected || !serverReady) return;
    const selectedPrefix = thread.messages.length === 0 && thread.selectedContext?.text
      ? `Selected context${thread.selectedContext.source ? ` (${thread.selectedContext.source})` : ""}:\n\n${thread.selectedContext.text}\n\n`
      : "";
    const inheritedPrefix = thread.messages.length === 0 && thread.inheritedContext
      ? `${thread.inheritedContext}\n\nSide-chat question:\n`
      : "";
    const assistantMessageId = uniqueMessageId("sa");
    const userMessageId = uniqueMessageId("su");
    const sent = sendChatMessage({
      displayContent: content,
      backendContent: `${selectedPrefix}${inheritedPrefix}${content}`,
      conversationId: id,
      allowWhileStreaming: false,
      skipLocalAppend: true,
      assistantMessageId,
      userMessageId,
    });
    if (sent) startMessage(id, content, { assistantMessageId, userMessageId });
  };

  const stop = () => {
    sendClientCommand(buildInterruptCommand(useAppStore.getState(), id));
  };

  const sendDisabled = useMemo(
    () => !thread || thread.isStreaming || !isConnected || !serverReady || !(thread?.draft ?? "").trim(),
    [isConnected, serverReady, thread],
  );

  if (!thread) return null;

  return (
    <div className="side-chat-panel flex flex-col flex-1 min-h-0">
      {thread.messages.length > 0 && <div
        className="px-2.5 py-1.5 flex items-center gap-2"
        style={{
          borderBottom: "1px solid var(--border-subtle)",
          background: "var(--surface-page)",
          fontSize: "var(--text-xs)",
          color: "var(--text-muted)",
        }}
      >
        <span className="flex-1">
          {thread.selectedContext ? "询问所选内容" : "侧边对话"}
        </span>
        <span
          style={{
            color: thread.isStreaming ? "var(--state-info)" : "var(--text-muted)",
          }}
        >
          {thread.isStreaming ? "生成中..." : "空闲"}
        </span>
      </div>}

      <div
        ref={listRef}
        className="flex-1 overflow-y-auto px-3.5 py-3 flex flex-col gap-3"
      >
        {thread.messages.length === 0 ? (
          <div
            className="side-chat-empty"
            style={{
              color: "var(--text-muted)",
              fontSize: "var(--text-sm)",
            }}
          >
            <MessageCirclePlus size={32} strokeWidth={1.5} aria-hidden="true" />
            <h2>侧边聊天</h2>
            <p>临时聊天。关闭此标签后会清除，主任务保持不变。</p>
            {thread.selectedContext && (
              <div
                className="mt-2.5 p-2.5 text-left whitespace-pre-wrap max-h-40 overflow-auto"
                style={{
                  border: "1px solid var(--accent-primary)",
                  borderRadius: "var(--radius-sm, 6px)",
                  background: "var(--surface-raised)",
                  color: "var(--text-secondary)",
                  fontFamily: "var(--font-mono)",
                  fontSize: "var(--text-xs)",
                }}
              >
                {thread.selectedContext.source && <div className="mb-1">{thread.selectedContext.source}</div>}
                {thread.selectedContext.text}
              </div>
            )}
          </div>
        ) : (
          thread.messages.map((m) =>
            m.role === "user" ? (
              <div key={m.id} className="flex justify-end">
                <div
                  className="max-w-[85%] px-2.5 py-1.5 whitespace-pre-wrap break-words"
                  style={{
                    background: "var(--surface-raised)",
                    color: "var(--text-primary)",
                    borderRadius: "var(--radius-md, 10px)",
                    fontSize: "var(--text-sm)",
                  }}
                >
                  {m.content}
                </div>
              </div>
            ) : (
              <div key={m.id} className="flex flex-col gap-1.5">
                {getToolCallsFromMessage(m).length > 0 && (
                  <div className="flex flex-col gap-1">
                    {getToolCallsFromMessage(m).map((tc) => (
                      <ToolCallCard
                        key={tc.id}
                        record={tc}
                        workspaceDirectory={workingDirectory}
                        conversationId={id}
                      />
                    ))}
                  </div>
                )}
                {(m.content || m.isStreaming || m.artifacts.length || m.replyAttachments?.length || m.blocks?.some((block) => block.type === "progress" && block.stage === "image_generation")) && (
                  <div
                    style={{
                      color: "var(--text-primary)",
                      fontSize: "var(--text-sm)",
                      lineHeight: "var(--leading-normal)",
                    }}
                  >
                    <AssistantMarkdownCell
                      isTranscriptMode
                      workspaceRoot={workingDirectory}
                      conversationId={id}
                      cell={{
                        kind: "assistant_markdown",
                        id: m.id,
                        messageId: m.id,
                        markdownSource: m.content,
                        phase: "final",
                        copyable: true,
                        isStreaming: m.isStreaming,
                        citations: m.citations,
                        artifacts: m.artifacts,
                        attachments: m.replyAttachments,
                        imageProgress: m.blocks?.filter((block): block is ProgressContentBlock => block.type === "progress" && block.stage === "image_generation"),
                        failureMessage: m.failureMessage,
                        failureRecoverable: m.failureRecoverable,
                        createdAt: m.timestamp,
                      }}
                    />
                  </div>
                )}
              </div>
            ),
          )
        )}
      </div>

      <div
        className="side-chat-compose"
      >
        <textarea
          ref={inputRef}
          value={thread.draft}
          onChange={(e) => setDraft(id, e.target.value)}
          onKeyDown={(e) => {
            if (e.nativeEvent.isComposing || e.keyCode === 229) return;
            const send = sendShortcut === "enter"
              ? e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.metaKey
              : e.key === "Enter" && (e.ctrlKey || e.metaKey);
            if (send) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder="随心输入"
          aria-label="侧边对话消息"
          rows={2}
          className="side-chat-input"
        />
        <div className="side-chat-compose-actions">
          <button
            type="button"
            onClick={thread.isStreaming ? stop : submit}
            disabled={!thread.isStreaming && sendDisabled}
            aria-label={thread.isStreaming ? "停止" : "发送"}
            title={thread.isStreaming ? "停止" : "发送"}
            className="side-chat-submit"
          >
            {thread.isStreaming ? <Square size={12} fill="currentColor" /> : <ArrowUp size={18} />}
          </button>
        </div>
      </div>
    </div>
  );
};
