import { useEffect, useRef, useState } from "react";
import { GitBranch, Pause, Play, X } from "lucide-react";
import { useAppStore } from "../stores";
import { deriveSendState } from "../lib/send-state";
import { sendChatMessage } from "../chat/sendChatMessage";
import type { DiffReviewFile, GitChangeFile, MessageAttachmentRef } from "../stores/types";
import { ContextChipRegion } from "./ActionChipRegion";
import { AttachmentStrip } from "./AttachmentStrip";
import { ComposerTextarea } from "./ComposerTextarea";
import { MenuOverlay } from "./MenuOverlay";
import { FooterRow } from "./FooterRow";
import { uploadComposerFiles } from "./uploads";
import { InlineAgentPrompt } from "../chat/InlineAgentPrompt";
import { InlineTaskList } from "../chat/components/InlineTaskList";
import { sendClientCommand } from "../protocol/ws-outbox";
import { buildContextFallback, buildContextPayload } from "./contextPayload";
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
  const gitChanges = useAppStore((s) => s.gitChanges);
  const selectedSkills = useAppStore((s) => s.selectedSkills);
  const activeGoal = useAppStore((s) => s.activeGoal);
  const currentModel = useAppStore((s) => s.currentModel);

  const containerRef = useRef<HTMLDivElement>(null);
  const [menuFilter, setMenuFilter] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const [selectedSlashCommand, setSelectedSlashCommand] = useState<string | null>(null);
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
  const changedFiles = [...gitChanges.workingTree, ...gitChanges.staged];
  const hasGitChanges = changedFiles.length > 0 || gitChanges.untracked.length > 0;

  const openDiffReview = () => {
    const store = useAppStore.getState();
    const files = buildDiffReviewFiles(store.gitChanges.workingTree, store.gitChanges.staged);
    if (files.length > 0) {
      store.setDiffReviewState({
        requestId: "working-tree",
        toolName: "working tree",
        diff: files.map((file) => file.patch).filter(Boolean).join("\n\n"),
        files,
        selectedPath: files[0]?.path,
        status: "viewing",
        mode: "view",
        fileDecisions: {},
        lineComments: [],
      });
    }
    store.setRightStackTab("diff");
    store.requestGitChanges();
  };

  const sendUserMessage = async (
    content: string,
    readyAttachments: Record<string, unknown>[] = [],
    options?: { backendContent?: string; displayContent?: string; attachmentRefs?: MessageAttachmentRef[] },
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
    const prefix = contextPayload || fallbackPayload;
    const effectiveContent = [prefix, content].filter(Boolean).join("\n\n").trim();
    if (!effectiveContent && readyAttachments.length === 0) return false;
    return sendChatMessage({
      displayContent: options?.displayContent ?? content,
      backendContent: [prefix, options?.backendContent ?? content].filter(Boolean).join("\n\n").trim(),
      attachments: readyAttachments,
      attachmentRefs: options?.attachmentRefs ?? [],
      contextRefs,
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
    const prefix = contextPayload || fallbackPayload;
    return sendChatMessage({
      ...options,
      backendContent: [prefix, options.backendContent].filter(Boolean).join("\n\n").trim(),
      contextRefs,
    });
  };

  const resetComposer = () => {
    setDraft("");
    clearAttachments();
    clearSelectedMentions();
    clearSelectedSkills();
    setMentionResults([]);
    closeSlashPanel();
    closeMentionPanel();
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
    if (sendState === "stop" && draft.trim()) {
      // User has typed content during streaming; ignore Enter and keep the run alive.
      return;
    }
    if (sendState !== "idle") return;
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
      };
    });

    const finalContent = content;
    const mentions = useAppStore.getState().selectedMentions;
    const mentionSuffix = mentions.length > 0
      ? " " + mentions.map((m) => `@${m.name}`).join(" ")
      : "";
    const displayContent = content + mentionSuffix;

    if (!await sendUserMessage(finalContent, readyAttachments, { attachmentRefs, displayContent })) return;
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

    const atMatch = getMentionMatch(lastLine);
    if (atMatch) {
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
      <div
        ref={containerRef}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        className="composer-container relative mx-auto flex flex-col transition-[background_140ms_ease,border-color_300ms_ease,box-shadow_140ms_ease]"
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
      {codeMode && (
        <div className="min-h-[36px] flex items-center gap-3 px-3 text-sm" style={{ borderBottom: "1px solid var(--border-subtle)" }}>
          <span className="flex-1" />
          {hasGitChanges && (
            <button
              type="button"
              className="h-7 px-[10px] rounded-sm text-sm inline-flex items-center gap-1.5"
              style={{
                border: "1px solid var(--border-subtle)",
                background: "var(--surface-base)",
                color: "var(--text-secondary)",
                cursor: "pointer",
              }}
              onClick={openDiffReview}
            >
              <GitBranch size={13} />
              Review diff
            </button>
          )}
        </div>
      )}
      {activeGoal && <GoalBar />}
      <InlineAgentPrompt />
      <ContextChipRegion />
      <AttachmentStrip />
      <ComposerTextarea
        value={draft}
        onChange={handleChange}
        onSubmit={submit}
        menuOpen={slashPanelOpen || mentionPanelOpen}
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
        placeholder={selectedSlashCommand ? "Add instructions..." : codeMode ? "Type / for commands" : "Write a message..."}
      />
      <MenuOverlay
        open={slashPanelOpen || mentionPanelOpen}
        kind={slashPanelOpen ? "slash" : "mention"}
        filter={menuFilter}
        onSelect={handleMenuSelect}
        placement={minimal ? "below" : "above"}
      />
      <FooterRow sendState={sendState} onSend={sendState === "stop" ? stopRun : submit} compact={codeMode} minimal={minimal} />
      </div>
    </>
  );
};

const buildDiffReviewFiles = (workingTree: GitChangeFile[], staged: GitChangeFile[]): DiffReviewFile[] =>
  [...staged, ...workingTree].flatMap((file) => {
    if (!file.patch) return [];
    return [{
      path: file.path,
      patch: file.patch,
      additions: file.additions,
      deletions: file.deletions,
      isBinary: file.isBinary,
    }];
  });

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

const normalizeMentionFilter = (value: string): string => {
  return value.trim();
};

const getMentionMatch = (line: string): RegExpMatchArray | null => {
  const match = line.match(/(?:^|\s)(@[A-Za-z0-9_./\\:#-]*)$/);
  if (!match) return null;
  if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(match[1].slice(1))) return null;
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
