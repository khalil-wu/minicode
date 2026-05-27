import { useEffect, useRef, useState } from "react";
import { useAppStore } from "../stores";
import { deriveSendState } from "../lib/send-state";
import { sendChatMessage } from "../chat/sendChatMessage";
import type { MessageAttachmentRef } from "../stores/types";
import { ContextChipRegion } from "./ActionChipRegion";
import { AttachmentStrip } from "./AttachmentStrip";
import { ComposerTextarea } from "./ComposerTextarea";
import { MenuOverlay } from "./MenuOverlay";
import { FooterRow } from "./FooterRow";
import { uploadComposerFiles } from "./uploads";
import { sendClientCommand } from "../protocol/ws-outbox";
import { buildContextFallback, buildContextPayload } from "./contextPayload";

export const Composer = () => {
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
  const toggleSkillsMarketplace = useAppStore((s) => s.toggleSkillsMarketplace);
  const appMode = useAppStore((s) => s.appMode);
  const gitChanges = useAppStore((s) => s.gitChanges);
  const selectedSkills = useAppStore((s) => s.selectedSkills);

  const containerRef = useRef<HTMLDivElement>(null);
  const [menuFilter, setMenuFilter] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const [selectedSlashCommand, setSelectedSlashCommand] = useState<string | null>(null);
  const hasReadyAttachment = useAppStore((s) => s.attachments.some((a) => a.status === "ready"));

  const sendState = deriveSendState({
    hasContent: draft.trim().length > 0 || hasReadyAttachment,
    isStreaming,
    isConnected,
  });
  const codeMode = appMode === "code";
  const activeSlashCommand = selectedSlashCommand ?? getActiveSlashCommand(draft);
  const commandModeActive = Boolean(activeSlashCommand && !slashPanelOpen);
  const additions = [...gitChanges.workingTree, ...gitChanges.staged].reduce((sum, file) => sum + file.additions, 0);
  const deletions = [...gitChanges.workingTree, ...gitChanges.staged].reduce((sum, file) => sum + file.deletions, 0);

  const addSystemNotice = (content: string) => {
    const state = useAppStore.getState();
    const notice = {
      id: `m-${Date.now().toString(36)}-sys`,
      role: "system" as const,
      content,
      artifacts: [],
      timestamp: Date.now(),
    };
    if (state.conversationId) {
      state.hydrateConversationMessages(state.conversationId, [...state.messages, notice], { activate: true });
    } else {
      useAppStore.setState({ messages: [...state.messages, notice] });
    }
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
    } catch {
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

  const submit = async () => {
    if (sendState === "stop") {
      interrupt();
      sendClientCommand({ type: "interrupt" });
      return;
    }
    if (sendState !== "idle") return;
    const content = selectedSlashCommand
      ? [selectedSlashCommand, draft.trim()].filter(Boolean).join(" ")
      : draft.trim();

    // Slash commands are command-mode input. Keep the UI quiet and let the
    // backend slash registry handle local commands, templates, and mode aliases.
    const slashMatch = content.match(/^(\/\w+)(?:\s+(.*))?$/s);
    if (slashMatch) {
      const cmd = slashMatch[1].toLowerCase();
      const rest = (slashMatch[2] || "").trim();

      // Extract inline @ mentions from the rest text
      const inlineAtRefs = rest.matchAll(/@(file|folder|url):([^\s]+)/g);
      for (const m of inlineAtRefs) {
        const kind = m[1] as "file" | "folder" | "url";
        const path = m[2];
        const name = kind === "url" ? path : (path.split(/[/\\]/).pop() || path);
        addSelectedMention({ path, name, kind });
      }

      const skill = findSkill(cmd.slice(1));
      if (skill) {
        addSelectedSkill({
          name: skill.name,
          description: skill.description,
          sourceLevel: skill.source_level,
        });
        sendClientCommand({ type: "load_skill", skill_name: skill.name });
        if (rest) {
          if (!await sendUserMessage(rest)) return;
          resetComposer();
        } else {
          setDraft("");
          closeSlashPanel();
          setMenuFilter("");
        }
        return;
      }

      const cleanRest = rest.replace(/@(file|folder|url):[^\s]+/g, "").trim();
      const commandLine = [cmd, cleanRest].filter(Boolean).join(" ");
      await executeSlashCommand(commandLine);
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
    const displayContent = content;

    if (!await sendUserMessage(finalContent, readyAttachments, { attachmentRefs, displayContent })) return;
    resetComposer();
  };

  const executeSlashCommand = async (commandLine: string) => {
    const [cmd, ...restParts] = commandLine.trim().split(/\s+/);
    const rest = restParts.join(" ");
      if (cmd === "/clear") {
      const { showConfirm } = await import("../overlays/DialogService");
      const ok = await showConfirm({
        title: "Clear conversation",
        message: "Clear all messages in the current conversation view? This cannot be undone.",
        confirmLabel: "Clear",
        danger: true,
      });
      if (!ok) return;
      const state = useAppStore.getState();
      if (state.conversationId) {
        sendClientCommand({ type: "conversation.clear", conversation_id: state.conversationId });
        state.hydrateConversationMessages(state.conversationId, [], { activate: true, isStreaming: false });
      } else {
        useAppStore.setState({ messages: [], isStreaming: false });
      }
      setDraft("");
      setSelectedSlashCommand(null);
      closeSlashPanel();
      setMenuFilter("");
      return;
    }

    if (cmd === "/skills" && !rest) {
      const state = useAppStore.getState();
      if (!state.skillsMarketplaceOpen) toggleSkillsMarketplace();
      sendClientCommand({ type: "skills.list" });
      sendClientCommand({ type: "skills.marketplace.list" });
      setDraft("");
      setSelectedSlashCommand(null);
      closeSlashPanel();
      setMenuFilter("");
      return;
    }

    if (cmd === "/compact") {
      useAppStore.getState().upsertSystemMessage(
        "system-compact-status",
        "Compacting context...",
        { replacePrefix: "Context compact" },
      );
    }

    const sent = sendChatMessage({
      displayContent: commandLine,
      backendContent: commandLine,
      skipLocalAppend: true,
    });
    if (sent) {
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

    const slashCommandLine = getSlashCommandLine(v);
    if (slashCommandLine && !slashPanelOpen) {
      openSlashPanel();
      setMenuFilter(slashCommandLine);
      sendClientCommand({ type: "skills.list" });
      if (/^\/skills(?:\s|$)/i.test(slashCommandLine)) {
        sendClientCommand({ type: "skills.list" });
      }
    } else if (slashPanelOpen) {
      if (slashCommandLine) {
        setMenuFilter(slashCommandLine);
        if (/^\/skills(?:\s|$)/i.test(slashCommandLine)) {
          sendClientCommand({ type: "skills.list" });
        }
      } else {
        closeSlashPanel();
        setMenuFilter("");
      }
    }

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
      const command = value.match(/^(\/[a-z][\w-]*)/i)?.[1] ?? value;
      if (shouldTokenizeSlashCommand(command)) {
        setSelectedSlashCommand(command);
        setDraft("");
      } else {
        setSelectedSlashCommand(null);
        setDraft(value);
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
      const rawPath = typed ? typed[2] : value;
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

  // Request full command list from backend on mount
  useEffect(() => {
    sendClientCommand({ type: "commands.list" });
    sendClientCommand({ type: "skills.list" });
  }, []);

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
    <div
      ref={containerRef}
      onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
      onDragLeave={() => setDragOver(false)}
      onDrop={handleDrop}
      style={{
        position: "relative",
        width: codeMode ? "min(1320px, calc(100% - 96px))" : "min(980px, calc(100% - 44px))",
        margin: codeMode ? "0 auto 14px" : "0 auto 20px",
        padding: codeMode ? "0" : "8px 10px 10px",
        background: commandModeActive ? commandComposerBackground : "var(--surface-page)",
        border: dragOver
          ? "2px dashed var(--command-accent, var(--state-info))"
          : commandModeActive
            ? "1px solid var(--command-border, var(--state-info))"
            : "1px solid var(--border-subtle)",
        borderRadius: codeMode ? "14px" : "14px",
        boxShadow: commandModeActive
          ? "0 0 0 1px color-mix(in oklch, var(--command-accent, var(--state-info)) 12%, transparent), 0 12px 32px color-mix(in oklch, var(--surface-base) 18%, transparent)"
          : "0 12px 32px color-mix(in oklch, var(--surface-base) 18%, transparent)",
        display: "flex",
        flexDirection: "column",
        transition: "background 140ms ease, border-color 140ms ease, box-shadow 140ms ease",
      }}
    >
      {codeMode && (
        <div style={codeComposerHeaderStyle}>
          <span style={{ flex: 1 }} />
          {(additions > 0 || deletions > 0) && (
            <span style={diffStatStyle}>
              {additions > 0 && <span style={{ color: "var(--state-success)" }}>+{additions.toLocaleString()}</span>}
              {deletions > 0 && <span style={{ color: "var(--state-danger)" }}>-{deletions.toLocaleString()}</span>}
            </span>
          )}
          <button type="button" style={commitButtonStyle} disabled>
            Commit changes
          </button>
        </div>
      )}
      <ContextChipRegion />
      <AttachmentStrip />
      <ComposerTextarea
        value={draft}
        onChange={handleChange}
        onSubmit={submit}
        menuOpen={slashPanelOpen || mentionPanelOpen}
        onDropFiles={handleComposerFiles}
        compact={codeMode}
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
      />
      <FooterRow sendState={sendState} onSend={submit} compact={codeMode} />
    </div>
  );
};

const codeComposerHeaderStyle: React.CSSProperties = {
  minHeight: 36,
  display: "flex",
  alignItems: "center",
  gap: 12,
  padding: "0 12px",
  borderBottom: "1px solid var(--border-subtle)",
  fontSize: "var(--text-sm)",
};

const diffStatStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  height: 26,
  padding: "0 10px",
  background: "var(--surface-base)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  fontFamily: "var(--font-mono)",
  fontWeight: 700,
};

const commitButtonStyle: React.CSSProperties = {
  height: 28,
  padding: "0 10px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  background: "var(--surface-base)",
  color: "var(--text-muted)",
  fontSize: "var(--text-sm)",
  cursor: "not-allowed",
};

const commandComposerBackground =
  "color-mix(in oklch, var(--command-accent, var(--state-info)) 7%, var(--surface-page))";

const findSkill = (query: string) => {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return null;
  const skills = useAppStore.getState().availableSkills;
  return (
    skills.find((skill) => skill.name.toLowerCase() === normalized) ??
    skills.find((skill) => skill.name.toLowerCase().includes(normalized)) ??
    null
  );
};

const normalizeMentionFilter = (value: string): string => {
  return value.trim();
};

const isComposerSlashCommand = (content: string): boolean => {
  if (!content.startsWith("/") || content.startsWith("//")) return false;
  return /^\/[a-z][\w-]*(?:\s+.*)?$/i.test(content.trimEnd());
};

const getSlashCommandLine = (value: string): string | null => {
  const lines = value.split("\n");
  if (lines.length > 1 && lines.slice(0, -1).some((line) => line.trim().length > 0)) return null;
  const line = lines[lines.length - 1];
  if (line === "/") return line;
  if (!isComposerSlashCommand(line)) return null;
  return line;
};

const getActiveSlashCommand = (value: string): string | null => {
  const line = getSlashCommandLine(value);
  if (!line || line === "/") return null;
  return line.match(/^(\/[a-z][\w-]*)/i)?.[1] ?? null;
};

const shouldTokenizeSlashCommand = (command: string): boolean => {
  const normalized = command.replace(/^\//, "").toLowerCase();
  const match = useAppStore.getState().slashCommands.find((item) =>
    item.command.toLowerCase() === normalized ||
    item.name.toLowerCase() === normalized ||
    item.label.toLowerCase() === `/${normalized}`
  );
  if (match) return match.type === "template";
  return new Set(["review", "debug", "refactor", "test", "docs", "explain", "commit"]).has(normalized);
};

const getMentionMatch = (line: string): RegExpMatchArray | null => {
  const match = line.match(/(?:^|\s)(@[A-Za-z0-9_./\\:-]*)$/);
  if (!match) return null;
  if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(match[1].slice(1))) return null;
  return match;
};
