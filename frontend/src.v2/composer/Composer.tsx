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
import { uploadComposerFiles } from "./uploads";
import { InlineAgentPrompt } from "../chat/InlineAgentPrompt";
import { InlineTaskList } from "../chat/components/InlineTaskList";
import { MessageQuote } from "../chat/components/MessageQuote";
import { sendClientCommand } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import { getWebSocket } from "../hooks/useWebSocket";
import { buildContextFallback, buildContextNativeAttachments, buildContextPayload } from "./contextPayload";
import {
  executeRuntimeSlashCommand,
  getActiveRuntimeSlashCommand,
  parseRuntimeSlashInput,
  resolveRuntimeSlashMenuSelection,
  syncRuntimeSlashPanelForDraft,
} from "../lib/runtime-commands";

let initialCatalogRequested = false;

export const Composer = ({ minimal = false }: { minimal?: boolean } = {}) => {
  const draft = useAppStore((s) => s.draft);
  const setDraft = useAppStore((s) => s.setDraft);
  const quotedMessage = useAppStore((s) => s.quotedMessage);
  const clearQuotedMessage = useAppStore((s) => s.clearQuotedMessage);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const isConnected = useAppStore((s) => s.isConnected);
  const interrupt = useAppStore((s) => s.interrupt);
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
  const appMode = useAppStore((s) => s.appMode);
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
  const hasReadyAttachment = useAppStore((s) => s.attachments.some((a) => a.status === "ready"));

  const sendState = deriveSendState({
    hasContent: draft.trim().length > 0 || hasReadyAttachment,
    isStreaming,
    isConnected,
    hasModel: currentModel.trim().length > 0,
  });
  const codeMode = appMode === "code";
  const wideMode = codeMode;
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

  const sendUserMessage = async (
    content: string,
    readyAttachments: Record<string, unknown>[] = [],
    options?: {
      backendContent?: string;
      displayContent?: string;
      attachmentRefs?: MessageAttachmentRef[];
      allowWhileStreaming?: boolean;
    },
  ) => {
    const contextRefs = [
      ...useAppStore.getState().selectedMentions,
      ...useAppStore.getState().selectedSkills,
    ];
    const fallbackPayload = buildContextFallback(contextRefs);
    let contextPayload = "";
    try {
      contextPayload = await buildContextPayload(contextRefs);
    } catch (error) {
      // Log the error for debugging
      console.warn("Failed to build context payload for @mentions, using fallback:", error);
      contextPayload = "";
    }
    let nativeContext = { attachments: [] as Record<string, unknown>[], attachmentRefs: [] as MessageAttachmentRef[], notes: "" };
    try {
      nativeContext = await buildContextNativeAttachments(contextRefs, getWebSocket()?.sessionId);
    } catch (error) {
      console.warn("Failed to build native context attachments:", error);
    }
    const prefix = [contextPayload || fallbackPayload, nativeContext.notes].filter(Boolean).join("\n\n");
    const mergedAttachments = [...readyAttachments, ...nativeContext.attachments];
    const mergedAttachmentRefs = [...(options?.attachmentRefs ?? []), ...nativeContext.attachmentRefs];
    const effectiveContent = [prefix, content].filter(Boolean).join("\n\n").trim();
    if (!effectiveContent && mergedAttachments.length === 0) return false;
    return sendChatMessage({
      displayContent: options?.displayContent ?? content,
      backendContent: [prefix, options?.backendContent ?? content].filter(Boolean).join("\n\n").trim(),
      attachments: mergedAttachments,
      attachmentRefs: mergedAttachmentRefs,
      contextRefs,
      allowWhileStreaming: options?.allowWhileStreaming,
    });
  };

  const sendRuntimeSlashMessage = async (options: {
    displayContent: string;
    backendContent: string;
    skipLocalAppend: boolean;
  }) => {
    const contextRefs = [
      ...useAppStore.getState().selectedMentions,
      ...useAppStore.getState().selectedSkills,
    ];
    const fallbackPayload = buildContextFallback(contextRefs);
    let contextPayload = "";
    try {
      contextPayload = await buildContextPayload(contextRefs);
    } catch (error) {
      // Log the error for debugging
      console.warn("Failed to build context payload for @mentions, using fallback:", error);
      contextPayload = "";
    }
    let nativeContext = { attachments: [] as Record<string, unknown>[], attachmentRefs: [] as MessageAttachmentRef[], notes: "" };
    try {
      nativeContext = await buildContextNativeAttachments(contextRefs, getWebSocket()?.sessionId);
    } catch (error) {
      console.warn("Failed to build native context attachments:", error);
    }
    const prefix = [contextPayload || fallbackPayload, nativeContext.notes].filter(Boolean).join("\n\n");
    return sendChatMessage({
      ...options,
      backendContent: [prefix, options.backendContent].filter(Boolean).join("\n\n").trim(),
      attachments: nativeContext.attachments,
      attachmentRefs: nativeContext.attachmentRefs,
      contextRefs,
      allowWhileStreaming: useAppStore.getState().isStreaming,
    });
  };

  const resetComposer = () => {
    setDraft("");
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
    const conversationId = useAppStore.getState().conversationId;
    interrupt();
    sendClientCommand({
      type: "interrupt",
      ...(conversationId ? { conversation_id: conversationId } : {}),
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
      const verb = blockingAttachment.status === "uploading" ? "finish uploading" : "be removed or uploaded again";
      pushToast(`Wait for "${blockingAttachment.name}" to ${verb} before sending.`, "warning", 3500);
      return;
    }

    const readyComposerAttachments = useAppStore.getState().attachments
      .filter((a) => a.status === "ready" && a.attachment);
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
        indexedChunks: Number(payload.indexed_chunks ?? a.indexedChunks ?? 0),
        dataUrl: a.type.startsWith("image/") ? a.dataUrl : undefined,
        inputSource: payload.input_source === "pasted_text" || a.inputSource === "pasted_text"
          ? "pasted_text" as const
          : "upload" as const,
        sourceCharCount: Number(payload.source_char_count ?? a.sourceCharCount ?? 0) || undefined,
      };
    });

    const finalContent = content;
    const quoteContext = quotedMessage ? formatQuotedMessageForBackend(quotedMessage) : "";
    const mentions = useAppStore.getState().selectedMentions;
    const mentionSuffix = mentions.length > 0
      ? " " + mentions.map((m) => `@${m.name}`).join(" ")
      : "";
    const displayContent = content + mentionSuffix;

    if (!await sendUserMessage(finalContent, readyAttachments, {
      attachmentRefs,
      displayContent,
      backendContent: [quoteContext, finalContent].filter(Boolean).join("\n\n"),
      allowWhileStreaming: queueWhileStreaming,
    })) return;
    if (finalContent.trim()) appendPromptHistory(workingDirectory, finalContent);
    resetComposer();
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
          title: "Clear conversation",
          message: "Clear all messages in the current conversation view? This cannot be undone.",
          confirmLabel: "Clear",
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
      sendClientCommand({ type: "skills.list" });
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
      const skillName = value.match(/^skill:(.+)$/)?.[1] ?? value.replace(/^\$/, "");
      const skill = useAppStore.getState().availableSkills.find((item) => item.name === skillName);
      if (skill) {
        addSelectedSkill({
          name: skill.name,
          description: skill.description,
          sourceLevel: skill.source_level,
        });
        sendClientCommand({ type: "load_skill", skill_name: skill.name });
      }
      const dollarIdx = draft.lastIndexOf("$");
      if (dollarIdx >= 0) setDraft(draft.slice(0, dollarIdx));
      setSkillPanelOpen(false);
      setMenuFilter("");
      return;
    }

    if (slashPanelOpen) {
      const selection = resolveRuntimeSlashMenuSelection(value, useAppStore.getState());
      if (selection.kind === "skill") {
        addSelectedSkill(selection.skill);
        sendClientCommand({ type: "load_skill", skill_name: selection.skill.name });
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
      const skillName = value.match(/^skill:(.+)$/)?.[1];
      if (skillName) {
        const skill = useAppStore.getState().availableSkills.find((item) => item.name === skillName);
        addSelectedSkill({
          name: skillName,
          description: skill?.description,
          sourceLevel: skill?.source_level,
        });
        sendClientCommand({ type: "load_skill", skill_name: skillName });
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
      {!minimal && <InlineTaskList wide={wideMode} />}
      <QueuedMessageList wide={wideMode} minimal={minimal} />
      <div
        ref={containerRef}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        className="composer-container relative mx-auto flex flex-col transition-[background_140ms_ease,border-color_300ms_ease,box-shadow_140ms_ease]"
        data-command-mode={commandModeActive ? "true" : "false"}
        data-drag-over={dragOver ? "true" : "false"}
        style={{
          position: "relative",
          left: undefined,
          bottom: undefined,
          transform: undefined,
          zIndex: minimal ? undefined : 6,
          display: "flex",
          flexDirection: "column",
          width: minimal ? "100%" : wideMode ? "var(--chat-wide-axis-width)" : "var(--chat-composer-axis-width)",
          marginBottom: codeMode ? "14px" : minimal ? 0 : "16px",
          padding: codeMode ? "0" : "8px 10px 10px",
          background: commandModeActive ? commandComposerBackground : "var(--surface-page)",
          border: dragOver
            ? "2px dashed var(--command-accent, var(--state-info))"
            : commandModeActive
              ? "1px solid var(--command-border, var(--state-info))"
              : "1px solid var(--border-subtle)",
          borderRadius: codeMode ? "var(--radius-md, 8px)" : "var(--radius-lg, 8px)",
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
        compact={codeMode}
        minimal={minimal}
        commandMode={commandModeActive}
        commandLabel={selectedSlashCommand ? selectedSlashCommand.slice(1) : null}
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
        placeholder={selectedSlashCommand ? "Add instructions..." : "Write a message..."}
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
        compact={codeMode}
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
        className="flex-none text-[11px] font-bold uppercase"
        style={{ color: paused ? "var(--text-muted)" : "var(--accent-primary)" }}
      >
        {paused ? "Paused" : "Goal"}
      </span>
      <span className="flex-1 min-w-0 overflow-hidden text-ellipsis whitespace-nowrap text-sm" style={{ color: "var(--text-primary)" }} title={goal.text}>
        {goal.text}
      </span>
      <button
        type="button"
        title={paused ? "Resume goal" : "Pause goal"}
        aria-label={paused ? "Resume goal" : "Pause goal"}
        className="w-6 h-6 inline-flex items-center justify-center rounded-sm cursor-pointer"
        style={{ border: "1px solid var(--border-subtle)", background: "var(--surface-soft)", color: "var(--text-secondary)" }}
        onClick={() => sendGoalAction(paused ? "resume" : "pause")}
      >
        {paused ? <Play size={13} /> : <Pause size={13} />}
      </button>
      <button
        type="button"
        title="Clear goal"
        aria-label="Clear goal"
        className="w-6 h-6 inline-flex items-center justify-center rounded-sm cursor-pointer"
        style={{ border: "1px solid var(--border-subtle)", background: "var(--surface-soft)", color: "var(--text-secondary)" }}
        onClick={() => sendGoalAction("clear")}
      >
        <X size={13} />
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
