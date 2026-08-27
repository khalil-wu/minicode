import { useCallback, useEffect, useRef, useState } from "react";
import { Pause, Play, X } from "lucide-react";
import { useAppStore } from "../stores";
import { deriveSendState } from "../lib/send-state";
import { sendChatMessage } from "../chat/sendChatMessage";
import type { ComposerQuote, MessageAttachmentRef } from "../stores/types";
import { ContextChipRegion } from "./ActionChipRegion";
import { AttachmentStrip } from "./AttachmentStrip";
import { ComposerTextarea } from "./ComposerTextarea";
import { MenuOverlay } from "./MenuOverlay";
import { FooterRow } from "./FooterRow";
import { PromptHistoryOverlay } from "./PromptHistoryOverlay";
import { QueuedMessageList } from "./QueuedMessageList";
import { appendPromptHistory, clearPromptHistory, readPromptHistory } from "./prompt-history";
import { acceptAttachmentConversationOwner, uploadComposerFiles } from "./uploads";
import { InlineAgentPrompt } from "../chat/InlineAgentPrompt";
import { TurnPlanProgress } from "../chat/components/TurnPlanProgress";
import { MessageQuote } from "../chat/components/MessageQuote";
import { sendClientCommand } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import { getWebSocket } from "../hooks/useWebSocket";
import { buildContextNativeAttachments, buildContextPayload } from "./contextPayload";
import {
  executeRuntimeSlashCommand,
  getActiveRuntimeSlashCommand,
  parseRuntimeSlashInput,
  resolveRuntimeSlashMenuSelection,
  syncRuntimeSlashPanelForDraft,
} from "../lib/runtime-commands";
import { buildInterruptCommand, hasInterruptFence } from "../lib/interrupt-command";
import { workspaceFilePathsEqual } from "../lib/workspace-path";

let initialCatalogRequested = false;

// Shared pre-send pipeline for both user messages and runtime slash messages:
// resolves the target conversation, uploads @mention/skill context attachments,
// and guards against conversation switches mid-upload. Returns null when the
// send must abort (a toast has already been shown).
const buildOutgoingContext = async (requestedConversationId: string) => {
  const stateAtSend = useAppStore.getState();
  const targetConversationId = String(
    requestedConversationId || stateAtSend.conversationId || "",
  ).trim();
  const contextRefs = [
    ...stateAtSend.selectedMentions,
    ...stateAtSend.selectedSkills,
  ];
  const skillInvocations = buildSkillInvocationLine(stateAtSend.selectedSkills);
  let contextPayload = "";
  try {
    contextPayload = await buildContextPayload(contextRefs);
  } catch (error) {
    // Log the error for debugging
    console.warn("Failed to build context payload for @mentions, using fallback:", error);
    contextPayload = "";
  }
  let nativeContext: Awaited<ReturnType<typeof buildContextNativeAttachments>> = {
    attachments: [] as Record<string, unknown>[],
    attachmentRefs: [] as MessageAttachmentRef[],
    notes: "",
    conversationId: targetConversationId || undefined,
  };
  try {
    nativeContext = await buildContextNativeAttachments(
      contextRefs,
      getWebSocket()?.sessionId,
      targetConversationId,
      stateAtSend.workingDirectory,
    );
  } catch (error) {
    console.warn("Failed to build native context attachments:", error);
    pushToast(
      error instanceof Error ? error.message : "上下文附件上传失败，请重试。",
      "error",
      4500,
    );
    return null;
  }
  if (nativeContext.notes) {
    pushToast("部分上下文附件无法读取，已在消息中标明。", "warning", 4200);
  }
  const resolvedConversationId = String(
    nativeContext.conversationId || targetConversationId,
  ).trim();
  if (
    targetConversationId
    && resolvedConversationId
    && resolvedConversationId !== targetConversationId
  ) {
    pushToast("附件所属会话已变化，请切回原会话后重试。", "error", 4500);
    return null;
  }
  if (
    !targetConversationId
    && resolvedConversationId
    && !acceptAttachmentConversationOwner(resolvedConversationId)
  ) {
    pushToast("附件已绑定到另一个会话，请切回该会话后发送。", "warning", 4500);
    return null;
  }
  const prefix = [skillInvocations, contextPayload, nativeContext.notes].filter(Boolean).join("\n\n");
  return {
    stateAtSend,
    contextRefs,
    prefix,
    attachments: nativeContext.attachments,
    attachmentRefs: nativeContext.attachmentRefs,
    conversationId: resolvedConversationId || targetConversationId || undefined,
  };
};

export const Composer = ({ minimal = false }: { minimal?: boolean } = {}) => {
  const draft = useAppStore((s) => s.draft);
  const setDraft = useAppStore((s) => s.setDraft);
  const quotedMessage = useAppStore((s) => s.quotedMessage);
  const clearQuotedMessage = useAppStore((s) => s.clearQuotedMessage);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const isConnected = useAppStore((s) => s.isConnected);
  const slashPanelOpen = useAppStore((s) => s.slashPanelOpen);
  const mentionPanelOpen = useAppStore((s) => s.mentionPanelOpen);
  const openSlashPanel = useAppStore((s) => s.openSlashPanel);
  const closeSlashPanel = useAppStore((s) => s.closeSlashPanel);
  const openMentionPanel = useAppStore((s) => s.openMentionPanel);
  const closeMentionPanel = useAppStore((s) => s.closeMentionPanel);
  const clearAttachments = useAppStore((s) => s.clearAttachments);
  const addSelectedMention = useAppStore((s) => s.addSelectedMention);
  const clearSelectedMentions = useAppStore((s) => s.clearSelectedMentions);
  const clearSelectedSkills = useAppStore((s) => s.clearSelectedSkills);
  const addSelectedSkill = useAppStore((s) => s.addSelectedSkill);
  const removeSelectedSkill = useAppStore((s) => s.removeSelectedSkill);
  const setMentionResults = useAppStore((s) => s.setMentionResults);
  const selectedSkills = useAppStore((s) => s.selectedSkills);
  const activeGoal = useAppStore((s) => s.activeGoal);
  const currentModel = useAppStore((s) => s.currentModel);
  const workingDirectory = useAppStore((s) => s.workingDirectory);

  const containerRef = useRef<HTMLDivElement>(null);
  const [menuFilter, setMenuFilter] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const [selectedSlashCommand, setSelectedSlashCommand] = useState<string | null>(null);
  const [skillPanelOpen, setSkillPanelOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyItems, setHistoryItems] = useState<string[]>([]);
  // ArrowUp/ArrowDown recall cursor (cc useArrowKeyHistory): -1 = live draft,
  // 0..n-1 = position in most-recent-first history. savedDraft preserves the
  // in-progress text so ArrowDown past the newest entry restores it.
  const historyCursorRef = useRef(-1);
  const historySavedDraftRef = useRef("");
  const hasReadyAttachment = useAppStore((s) => s.attachments.some((a) => a.status === "ready"));

  const sendState = deriveSendState({
    hasContent: draft.trim().length > 0 || hasReadyAttachment,
    isStreaming,
    isConnected,
    hasModel: currentModel.trim().length > 0,
  });
  // Both work modes use the code-mode chat presentation. Mode-specific tools
  // remain separate; transcript and composer geometry do not change.
  const codeLayout = !minimal;
  const wideMode = false;
  const activeSlashCommand = selectedSlashCommand ?? getActiveRuntimeSlashCommand(draft);
  const commandModeActive = Boolean(activeSlashCommand && !slashPanelOpen);

  const openPromptHistory = useCallback(() => {
    closeSlashPanel();
    closeMentionPanel();
    setSkillPanelOpen(false);
    setMenuFilter("");
    setHistoryItems(readPromptHistory(workingDirectory));
    setHistoryOpen(true);
  }, [closeMentionPanel, closeSlashPanel, workingDirectory]);

  useEffect(() => {
    window.addEventListener("composer:history-search", openPromptHistory);
    return () => window.removeEventListener("composer:history-search", openPromptHistory);
  }, [openPromptHistory]);

  const recallHistory = useCallback(
    (direction: "up" | "down"): string | null => {
      const items = readPromptHistory(workingDirectory);
      if (items.length === 0) return null;
      const cursor = historyCursorRef.current;
      if (direction === "up") {
        // Entering history from the live draft: save it so ArrowDown can return.
        if (cursor === -1) historySavedDraftRef.current = useAppStore.getState().draft;
        const next = Math.min(cursor + 1, items.length - 1);
        historyCursorRef.current = next;
        return items[next];
      }
      // direction === "down"
      if (cursor <= -1) return null; // already at live draft; let arrow move caret
      const next = cursor - 1;
      historyCursorRef.current = next;
      return next === -1 ? historySavedDraftRef.current : items[next];
    },
    [workingDirectory],
  );

  const escapeInterrupt = useCallback((): boolean => {
    if (!useAppStore.getState().isStreaming) return false;
    stopRun();
    return true;
  }, []);

  const sendUserMessage = async (
    content: string,
    readyAttachments: Record<string, unknown>[] = [],
    options?: {
      backendContent?: string;
      displayContent?: string;
      attachmentRefs?: MessageAttachmentRef[];
      conversationId?: string;
      allowWhileStreaming?: boolean;
      busyBehavior?: "queue" | "steer";
    },
  ) => {
    const outgoing = await buildOutgoingContext(options?.conversationId || "");
    if (!outgoing) return false;
    const mergedAttachments = [...readyAttachments, ...outgoing.attachments];
    const mergedAttachmentRefs = [...(options?.attachmentRefs ?? []), ...outgoing.attachmentRefs];
    const effectiveContent = [outgoing.prefix, content].filter(Boolean).join("\n\n").trim();
    if (!effectiveContent && mergedAttachments.length === 0) return false;
    return sendChatMessage({
      displayContent: options?.displayContent ?? content,
      backendContent: [outgoing.prefix, options?.backendContent ?? content].filter(Boolean).join("\n\n").trim(),
      attachments: mergedAttachments,
      attachmentRefs: mergedAttachmentRefs,
      conversationId: outgoing.conversationId,
      contextRefs: outgoing.contextRefs,
      allowWhileStreaming: options?.allowWhileStreaming,
      busyBehavior: options?.busyBehavior,
    });
  };

  const sendRuntimeSlashMessage = async (options: {
    displayContent: string;
    backendContent: string;
    skipLocalAppend: boolean;
  }) => {
    const outgoing = await buildOutgoingContext("");
    if (!outgoing) return false;
    return sendChatMessage({
      ...options,
      backendContent: [outgoing.prefix, options.backendContent].filter(Boolean).join("\n\n").trim(),
      attachments: outgoing.attachments,
      attachmentRefs: outgoing.attachmentRefs,
      conversationId: outgoing.conversationId,
      contextRefs: outgoing.contextRefs,
      allowWhileStreaming: outgoing.stateAtSend.isStreaming,
      busyBehavior: outgoing.stateAtSend.followUpBehavior,
    });
  };

  const resetComposer = () => {
    setDraft("");
    historyCursorRef.current = -1;
    historySavedDraftRef.current = "";
    clearAttachments();
    clearSelectedMentions();
    clearSelectedSkills();
    clearQuotedMessage();
    setMentionResults([]);
    closeSlashPanel();
    closeMentionPanel();
    setSkillPanelOpen(false);
    setMenuFilter("");
    setSelectedSlashCommand(null);
  };

  const stopRun = () => {
    const state = useAppStore.getState();
    const command = buildInterruptCommand(state);
    // Before agent.run.started binds a turn/message fence, the backend must
    // reject an unfenced interrupt rather than guessing which run to stop.
    // Finish the local optimistic state in that narrow window so Stop never
    // appears to do nothing; a fenced command still waits for the server's
    // authoritative terminal event.
    if (!hasInterruptFence(command)) state.interrupt();
    sendClientCommand(command);
  };

  const composerFingerprint = () => {
    const state = useAppStore.getState();
    return JSON.stringify({
      conversationId: String(state.conversationId || ""),
      draft: state.draft,
      attachments: state.attachments.map((item) => ({
        id: item.id,
        status: item.status,
        artifactId: item.artifactId || item.attachment?.artifact_id || item.attachment?.id || "",
      })),
      mentions: state.selectedMentions.map((item) => ({ path: item.path, name: item.name })),
      skills: state.selectedSkills.map((item) => ({ path: item.path, name: item.name })),
      quotedMessageId: state.quotedMessage?.id || "",
      selectedSlashCommand,
    });
  };

  const submit = async () => {
    if (sendState === "stop" && !draft.trim()) return;
    if (sendState !== "idle" && sendState !== "queue" && sendState !== "offline-queue") return;
    const queueWhileStreaming = sendState === "queue";
    const content = selectedSlashCommand
      ? [selectedSlashCommand, draft.trim()].filter(Boolean).join(" ")
      : draft.trim();

    const slashInput = parseRuntimeSlashInput(content);
    if (slashInput) {
      for (const mention of slashInput.mentions) {
        addSelectedMention(mention);
      }
      await executeSlashCommand(slashInput.commandLine);
      return;
    }

    const composerAttachments = useAppStore.getState().attachments;
    const blockingAttachment = composerAttachments.find((a) => a.status !== "ready" || !a.attachment);
    if (blockingAttachment) {
      const message = blockingAttachment.status === "uploading"
        ? `“${blockingAttachment.name}”仍在上传，请等待完成后发送。`
        : blockingAttachment.error
          ? `“${blockingAttachment.name}”${blockingAttachment.error}`
          : `“${blockingAttachment.name}”上传失败，请移除或重试后发送。`;
      pushToast(message, "warning", 3500);
      return;
    }

    const readyComposerAttachments = useAppStore.getState().attachments
      .filter((a) => a.status === "ready" && a.attachment);
    const missingOwner = readyComposerAttachments.find((attachment) => (
      !String(attachment.conversationId || "").trim()
    ));
    if (missingOwner) {
      pushToast(`“${missingOwner.name}”没有绑定会话，请重新上传。`, "error", 4200);
      return;
    }
    const attachmentOwners = new Set(
      readyComposerAttachments
        .map((attachment) => String(attachment.conversationId || "").trim())
        .filter(Boolean),
    );
    if (attachmentOwners.size > 1) {
      pushToast("这些附件来自不同会话，请移除后在同一会话重新上传。", "error", 4500);
      return;
    }
    const attachmentConversationId = [...attachmentOwners][0] || "";
    const currentConversationId = String(useAppStore.getState().conversationId || "").trim();
    if (
      attachmentConversationId
      && currentConversationId
      && attachmentConversationId !== currentConversationId
    ) {
      pushToast("附件属于另一个会话，请切回附件所在会话后发送。", "warning", 4500);
      return;
    }
    if (
      attachmentConversationId
      && !currentConversationId
      && !acceptAttachmentConversationOwner(attachmentConversationId)
    ) {
      pushToast("附件已绑定到另一个会话，请切回该会话后发送。", "warning", 4500);
      return;
    }
    const sendConversationId = attachmentConversationId || currentConversationId;
    const readyAttachments = readyComposerAttachments.map((a) => a.attachment as Record<string, unknown>);
    const attachmentRefs = readyComposerAttachments.map((a) => {
      const payload = a.attachment as Record<string, unknown>;
      const kind = String(payload.kind || (a.type.startsWith("image/") ? "image" : "document"));
      return {
        id: String(payload.id || a.id),
        name: String(payload.file_name || a.name),
        kind: kind === "image" ? "image" as const : kind === "document" ? "document" as const : "file" as const,
        mediaType: String(payload.media_type || a.type),
        sizeBytes: Number(payload.size_bytes || a.size || 0),
        artifactId: String(payload.artifact_id || a.artifactId || ""),
        docId: String(payload.doc_id || a.docId || ""),
        dataUrl: a.type.startsWith("image/") ? a.dataUrl : undefined,
        inputSource: payload.input_source === "pasted_text" || a.inputSource === "pasted_text"
          ? "pasted_text" as const
          : "upload" as const,
        sourceCharCount: Number(payload.source_char_count ?? a.sourceCharCount ?? 0) || undefined,
      };
    });

    const finalContent = content;
    const composerStateAtSend = composerFingerprint();
    const conversationAtSend = String(useAppStore.getState().conversationId || "").trim();
    const quoteContext = quotedMessage ? formatQuotedMessageForBackend(quotedMessage) : "";
    const mentions = useAppStore.getState().selectedMentions;
    const mentionSuffix = mentions.length > 0
      ? " " + mentions.map((m) => `@${m.name}`).join(" ")
      : "";
    const displayContent = content + mentionSuffix;

    if (!await sendUserMessage(finalContent, readyAttachments, {
      attachmentRefs,
      conversationId: sendConversationId || undefined,
      displayContent,
      backendContent: [quoteContext, finalContent].filter(Boolean).join("\n\n"),
      allowWhileStreaming: queueWhileStreaming,
      busyBehavior: useAppStore.getState().followUpBehavior,
    })) return;
    const stateAfterSend = useAppStore.getState();
    const currentConversation = String(stateAfterSend.conversationId || "").trim();
    const sameConversation = currentConversation === conversationAtSend;
    // Upload/context preparation is asynchronous.  If the user edited the
    // draft, changed attachments, or switched conversations while it was in
    // flight, preserve the new composer state instead of clearing it.
    if (sameConversation && composerFingerprint() === composerStateAtSend) {
      if (finalContent.trim()) appendPromptHistory(workingDirectory, finalContent);
      resetComposer();
    }
  };

  const executeSlashCommand = async (commandLine: string) => {
    const result = await executeRuntimeSlashCommand(commandLine, {
      getState: useAppStore.getState,
      setState: useAppStore.setState,
      sendClientCommand,
      sendChatMessage: sendRuntimeSlashMessage,
      sendUserMessage,
      confirmClear: async () => {
        const { showConfirm } = await import("../overlays/DialogService");
        return showConfirm({
          title: "清空会话",
          message: "清空当前会话视图中的全部消息？此操作无法撤销。",
          confirmLabel: "清空",
          danger: true,
        });
      },
    });
    if (result.reset === "composer") {
      resetComposer();
      return;
    }
    if (result.reset === "input") {
      setDraft("");
      setSelectedSlashCommand(null);
      closeSlashPanel();
      setMenuFilter("");
    }
  };

  const handleChange = (v: string) => {
    if (historyOpen) setHistoryOpen(false);
    // Reset the history-recall cursor whenever the draft changes for a reason
    // other than a recall fill, so the next ArrowUp starts from the live draft.
    if (historyCursorRef.current !== -1 && v !== historySavedDraftRef.current) {
      const items = readPromptHistory(workingDirectory);
      if (v !== items[historyCursorRef.current]) historyCursorRef.current = -1;
    }
    setDraft(v);

    const lines = v.split("\n");
    const lastLine = lines[lines.length - 1];

    syncRuntimeSlashPanelForDraft(v, {
      slashPanelOpen,
      openSlashPanel,
      closeSlashPanel,
      setMenuFilter,
      sendClientCommand,
    });

    const skillMatch = getSkillMatch(lastLine);
    if (skillMatch && !slashPanelOpen) {
      closeMentionPanel();
      closeSlashPanel();
      setSkillPanelOpen(true);
      setMenuFilter(skillMatch[1]);
      return;
    }

    if (skillPanelOpen) {
      setSkillPanelOpen(false);
      setMenuFilter("");
    }

    const atMatch = getMentionMatch(lastLine);
    if (atMatch) {
      setSkillPanelOpen(false);
      if (!mentionPanelOpen) openMentionPanel();
      setMenuFilter(normalizeMentionFilter(atMatch[1]));
    } else if (mentionPanelOpen) {
      closeMentionPanel();
      setMenuFilter("");
    }
  };

  const handleMenuSelect = (value: string) => {
    if (!value) {
      closeSlashPanel();
      closeMentionPanel();
      setSkillPanelOpen(false);
      setMenuFilter("");
      return;
    }

    if (skillPanelOpen) {
      const encodedPath = value.match(/^skill-path:(.+)$/)?.[1];
      const encodedName = value.match(/^skill-name:(.+)$/)?.[1];
      const skillPath = encodedPath ? decodeURIComponent(encodedPath) : "";
      const skillName = encodedName ? decodeURIComponent(encodedName) : value.replace(/^\$/, "");
      const skill = useAppStore.getState().availableSkills.find((item) => (
        skillPath
          ? workspaceFilePathsEqual(item.path, skillPath, workingDirectory)
          : item.name === skillName
      ));
      if (skill) {
        addSelectedSkill({
          name: skill.name,
          path: skill.path,
          description: skill.description,
          sourceLevel: skill.source_level,
        });
      }
      const dollarIdx = draft.lastIndexOf("$");
      if (dollarIdx >= 0) setDraft(draft.slice(0, dollarIdx));
      setSkillPanelOpen(false);
      setMenuFilter("");
      return;
    }

    if (slashPanelOpen) {
      const selection = resolveRuntimeSlashMenuSelection(value, useAppStore.getState());
      if (selection.kind === "skill_picker") {
        setSelectedSlashCommand(null);
        setDraft("/skill ");
        setMenuFilter("/skill ");
        sendClientCommand({ type: "skills.list" });
        return;
      }
      if (selection.kind === "skill") {
        addSelectedSkill(selection.skill);
        setSelectedSlashCommand(null);
        setDraft("");
        closeSlashPanel();
        setMenuFilter("");
        return;
      }
      if (selection.kind === "tokenize") {
        setSelectedSlashCommand(selection.command);
        setDraft("");
      } else if (selection.kind === "execute") {
        setSelectedSlashCommand(null);
        void executeSlashCommand(selection.commandLine);
      }
      closeSlashPanel();
      setMenuFilter("");
    } else if (mentionPanelOpen) {
      const encodedPlugin = value.match(/^plugin:(.+)$/)?.[1];
      if (encodedPlugin) {
        const configName = decodeURIComponent(encodedPlugin);
        addSelectedMention({
          kind: "plugin",
          name: configName,
          configName,
          path: `plugin://${configName}`,
        });
        const atIdx = draft.lastIndexOf("@");
        if (atIdx >= 0) setDraft(draft.slice(0, atIdx));
        setMentionResults([]);
        closeMentionPanel();
        setMenuFilter("");
        return;
      }
      const typed = value.match(/^(file|folder):(.*)$/);
      const rawPath = appendDraftLineAnchor(typed ? typed[2] : value, draft);
      const kind = typed?.[1] === "folder" || rawPath.endsWith("/") || rawPath.endsWith("\\") ? "folder" : "file";
      const name = rawPath.split(/[/\\]/).filter(Boolean).pop() || rawPath;

      addSelectedMention({ path: rawPath, name, kind: kind as "file" | "folder" });
      const atIdx = draft.lastIndexOf("@");
      if (atIdx >= 0) {
        setDraft(draft.slice(0, atIdx));
      }
      setMentionResults([]);
      closeMentionPanel();
      setMenuFilter("");
    }
  };

  useEffect(() => {
    const onClickOutside = (e: MouseEvent) => {
      if (!containerRef.current) return;
      if (!containerRef.current.contains(e.target as Node)) {
        closeSlashPanel();
        closeMentionPanel();
        setSkillPanelOpen(false);
        setMenuFilter("");
      }
    };
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, [closeSlashPanel, closeMentionPanel]);

  // Request full command/skill lists once; websocket reconnect already refreshes
  // command metadata, so repeated Composer remounts should not spam the backend.
  useEffect(() => {
    if (minimal || initialCatalogRequested) return;
    initialCatalogRequested = true;
    sendClientCommand({ type: "commands.list" });
    sendClientCommand({ type: "skills.list" });
  }, [minimal]);

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const files = e.dataTransfer.files;
    if (!files.length) return;
    uploadComposerFiles(Array.from(files));
  };

  const handleComposerFiles = (files: File[]) => {
    uploadComposerFiles(files);
  };

  return (
    <>
      {!minimal && <TurnPlanProgress wide={wideMode} />}
      <QueuedMessageList wide={wideMode} minimal={minimal} />
      <div
        ref={containerRef}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        className="composer-container relative mx-auto flex flex-col transition-[background_140ms_ease,border-color_300ms_ease,box-shadow_140ms_ease]"
        data-command-mode={commandModeActive ? "true" : "false"}
        data-drag-over={dragOver ? "true" : "false"}
        data-layout-mode={codeLayout ? "code" : "cowork"}
        style={{
          position: "relative",
          left: undefined,
          bottom: undefined,
          transform: undefined,
          zIndex: minimal ? undefined : "var(--z-composer)",
          display: "flex",
          flexDirection: "column",
          width: minimal ? "100%" : wideMode ? "var(--chat-wide-axis-width)" : "var(--chat-composer-axis-width)",
          marginBottom: codeLayout ? "14px" : 0,
          padding: codeLayout ? "0" : "8px 10px 10px",
          background: commandModeActive ? commandComposerBackground : "transparent",
          border: dragOver
            ? "2px dashed var(--command-accent, var(--state-info))"
            : commandModeActive
              ? "1px solid var(--command-border, var(--state-info))"
              : "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-lg, 16px)",
          boxShadow: commandModeActive
            ? "0 0 0 1px color-mix(in oklch, var(--command-accent, var(--state-info)) 10%, transparent)"
            : "none",
        }}
      >
      {activeGoal && <GoalBar />}
      <InlineAgentPrompt />
      <ContextChipRegion />
      <AttachmentStrip />
      {quotedMessage && <MessageQuote message={quotedMessage} onRemove={clearQuotedMessage} />}
      <ComposerTextarea
        value={draft}
        onChange={handleChange}
        onSubmit={submit}
        menuOpen={slashPanelOpen || mentionPanelOpen || skillPanelOpen || historyOpen}
        onHistorySearch={openPromptHistory}
        onDropFiles={handleComposerFiles}
        compact={codeLayout}
        minimal={minimal}
        commandMode={commandModeActive}
        commandLabel={selectedSlashCommand ? selectedSlashCommand.slice(1) : null}
        onRecallHistory={recallHistory}
        onEscape={escapeInterrupt}
        onClearCommand={() => setSelectedSlashCommand(null)}
        skillTokens={selectedSkills.map((skill) => ({
          name: skill.name,
          description: skill.description,
        }))}
        onRemoveSkill={removeSelectedSkill}
        onRemoveLastSkill={() => {
          const last = useAppStore.getState().selectedSkills.at(-1);
          if (last) removeSelectedSkill(last.name);
        }}
        placeholder={selectedSlashCommand ? "补充指令…" : "描述任务或提出问题…"}
      />
      <MenuOverlay
        open={slashPanelOpen || mentionPanelOpen || skillPanelOpen}
        kind={slashPanelOpen ? "slash" : skillPanelOpen ? "skill" : "mention"}
        filter={menuFilter}
        onSelect={handleMenuSelect}
        placement={minimal ? "below" : "above"}
      />
      <FooterRow
        sendState={sendState}
        onSend={sendState === "stop" ? stopRun : submit}
        onStop={stopRun}
        compact={codeLayout}
        minimal={minimal}
      />
      <PromptHistoryOverlay
        open={historyOpen}
        items={historyItems}
        placement={minimal ? "below" : "above"}
        onClose={() => setHistoryOpen(false)}
        onSelect={(prompt) => {
          setDraft(prompt);
          setHistoryOpen(false);
          queueMicrotask(() => window.dispatchEvent(new Event("composer:focus")));
        }}
        onClear={() => {
          clearPromptHistory(workingDirectory);
          setHistoryItems([]);
        }}
      />
      </div>
    </>
  );
};

const GoalBar = () => {
  const goal = useAppStore((s) => s.activeGoal);
  const conversationId = useAppStore((s) => s.conversationId);
  if (!goal) return null;
  const paused = goal.status === "paused";
  const sendGoalAction = (action: "pause" | "resume" | "clear") => {
    sendClientCommand({
      type: "conversation.goal.set",
      conversation_id: conversationId || undefined,
      action,
      source: "frontend.goal_bar",
    });
  };
  return (
    <div className="min-h-[34px] flex items-center gap-2 px-3" style={{ borderBottom: "1px solid var(--border-subtle)", background: "color-mix(in oklch, var(--accent-primary) 8%, var(--surface-page))" }}>
      <span
        className="flex-none text-3xs font-bold uppercase"
        style={{ color: paused ? "var(--text-muted)" : "var(--accent-primary)" }}
      >
        {paused ? "已暂停" : "目标"}
      </span>
      <span className="flex-1 min-w-0 overflow-hidden text-ellipsis whitespace-nowrap text-sm" style={{ color: "var(--text-primary)" }} title={goal.text}>
        {goal.text}
      </span>
      <button
        type="button"
        title={paused ? "继续目标" : "暂停目标"}
        aria-label={paused ? "继续目标" : "暂停目标"}
        className="w-6 h-6 inline-flex items-center justify-center rounded-sm cursor-pointer"
        style={{ border: "1px solid var(--border-subtle)", background: "var(--surface-soft)", color: "var(--text-secondary)" }}
        onClick={() => sendGoalAction(paused ? "resume" : "pause")}
      >
        {paused ? <Play size={14} /> : <Pause size={14} />}
      </button>
      <button
        type="button"
        title="清除目标"
        aria-label="清除目标"
        className="w-6 h-6 inline-flex items-center justify-center rounded-sm cursor-pointer"
        style={{ border: "1px solid var(--border-subtle)", background: "var(--surface-soft)", color: "var(--text-secondary)" }}
        onClick={() => sendGoalAction("clear")}
      >
        <X size={14} />
      </button>
    </div>
  );
};

const commandComposerBackground =
  "color-mix(in oklch, var(--command-accent, var(--state-info)) 7%, var(--surface-page))";

const formatQuotedMessageForBackend = (quote: ComposerQuote): string => {
  const speaker = quote.role === "user" ? "User" : quote.role === "assistant" ? "Assistant" : "System";
  return [`Quoted ${speaker} message:`, quote.content.trim()].filter(Boolean).join("\n");
};

const normalizeMentionFilter = (value: string): string => {
  return value.trim();
};

const getMentionMatch = (line: string): RegExpMatchArray | null => {
  const match = line.match(/(?:^|\s)(@[A-Za-z0-9_./\\:#-]*)$/);
  if (!match) return null;
  if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(match[1].slice(1))) return null;
  return match;
};

const getSkillMatch = (line: string): RegExpMatchArray | null => {
  const match = line.match(/(?:^|\s)(\$[A-Za-z0-9_.:/\\-]*)$/);
  if (!match) return null;
  return match;
};

const buildSkillInvocationLine = (skills: Array<{ name: string }>): string =>
  skills.map((skill) => `$${skill.name}`).join(" ");

const appendDraftLineAnchor = (path: string, draft: string): string => {
  if (path.includes("#")) return path;
  const currentLine = draft.split("\n").at(-1) ?? draft;
  const token = getMentionMatch(currentLine)?.[1] ?? "";
  const anchor = normalizeLineAnchor(token);
  return anchor ? `${path}#${anchor}` : path;
};

const normalizeLineAnchor = (token: string): string => {
  const anchor = token.match(/#L?(\d+)(?:-L?(\d+))?$/i);
  if (!anchor) return "";
  return anchor[2] ? `${anchor[1]}-${anchor[2]}` : anchor[1];
};
