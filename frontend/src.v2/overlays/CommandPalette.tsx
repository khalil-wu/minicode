import { useEffect, useState } from "react";
import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";
import { sendChatMessage } from "../chat/sendChatMessage";
import { toBackendPermissionMode } from "../protocol/permissions";
import { hasRuntimePendingUserAction, hasRuntimePendingUserActionForConversation } from "../lib/runtime-session";
import { buildRuntimeSlashPaletteItems, executeRuntimeSlashCommand } from "../lib/runtime-commands";
import { hasLocalPendingPromptForConversation } from "../lib/pending-prompts";
import { workspaceDisplayName } from "../lib/workspace-display";
import { openSettings } from "../lib/settings-navigation";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import { useFocusTrap } from "../hooks/useFocusTrap";

interface PaletteAction {
  id: string;
  label: string;
  hint?: string;
  run: () => void | Promise<void>;
}

const hasPendingUserAction = (): boolean => {
  const state = useAppStore.getState();
  const activeConversationId = state.conversationId ?? state.runtimeSession?.active_conversation_id;
  const hasRuntimePending = activeConversationId
    ? hasRuntimePendingUserActionForConversation(state.runtimeSession, activeConversationId)
    : hasRuntimePendingUserAction(state.runtimeSession);
  return Boolean(
    hasLocalPendingPromptForConversation(
      [state.pendingApproval, state.pendingDiffReview, state.pendingAskUser],
      activeConversationId,
      state.conversationId,
    ) ||
    hasRuntimePending,
  );
};

export const CommandPalette = () => {
  const commandPaletteOpen = useAppStore((s) => s.commandPaletteOpen);
  const themeMode = useAppStore((s) => s.themeMode);
  const panelSlots = useAppStore((s) => s.panelSlots);
  const slashCommands = useAppStore((s) => s.slashCommands);
  const toggleCommandPalette = useAppStore((s) => s.toggleCommandPalette);
  const setThemeMode = useAppStore((s) => s.setThemeMode);
  const addPanel = useAppStore((s) => s.addPanel);
  const openLivePreview = useAppStore((s) => s.openLivePreview);
  const focusPanel = useAppStore((s) => s.focusPanel);
  const togglePanelMaximized = useAppStore((s) => s.togglePanelMaximized);
  const removePanel = useAppStore((s) => s.removePanel);
  const resetPanelLayout = useAppStore((s) => s.resetPanelLayout);
  const setActiveBottomTab = useAppStore((s) => s.setActiveBottomTab);
  const setRightStackTab = useAppStore((s) => s.setRightStackTab);
  const setAppMode = useAppStore((s) => s.setAppMode);
  const conversations = useAppStore((s) => s.conversations);
  const conversationId = useAppStore((s) => s.conversationId);
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities);
  const globalSearchEnabled = capabilityFeatureEnabled(runtimeCapabilities, "global_search", true);
  const agentEditorEnabled = capabilityFeatureEnabled(runtimeCapabilities, "agent_editor", true);
  const [query, setQuery] = useState("");
  const [activeIdx, setActiveIdx] = useState(0);
  const dialogRef = useFocusTrap(commandPaletteOpen);
  const closePaletteIfIdle = () => {
    if (hasPendingUserAction()) return;
    toggleCommandPalette();
  };

  useEffect(() => {
    if (commandPaletteOpen) {
      setQuery("");
      setActiveIdx(0);
      sendClientCommand({ type: "commands.list" });
    }
  }, [commandPaletteOpen]);

  useEffect(() => {
    if (!commandPaletteOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (hasPendingUserAction()) return;
      event.preventDefault();
      toggleCommandPalette();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [commandPaletteOpen, toggleCommandPalette]);

  if (!commandPaletteOpen) return null;

  const focusedPane = panelSlots.find((slot) => slot.focused) ?? panelSlots[0];
  const openDockTab = (tab: "git" | "timeline" | "debug" | "budget") => {
    setActiveBottomTab(tab);
    useAppStore.setState({ dockCollapsed: false });
  };
  const openRightStack = (tab: "preview" | "terminal" | "tasks" | "plan" | "subagents" | "inspector" | "diagnostics") => {
    setAppMode("code");
    setRightStackTab(tab);
  };
  const runRuntimeSlashCommand = async (commandLine: string) => {
    await executeRuntimeSlashCommand(commandLine, {
      getState: useAppStore.getState,
      setState: useAppStore.setState,
      sendClientCommand,
      sendChatMessage,
      confirmClear: async () => {
        const { showConfirm } = await import("./DialogService");
        return showConfirm({
          title: "Clear conversation",
          message: "Clear all messages in the current conversation view? This cannot be undone.",
          confirmLabel: "Clear",
          danger: true,
        });
      },
    });
  };
  const runtimeSlashActions: PaletteAction[] = buildRuntimeSlashPaletteItems(slashCommands, {
    exclude: ["clear", "skills", "compact", "new"],
  }).map((command) => ({
    id: command.id,
    label: `Run ${command.name}`,
    hint: command.description || "slash command",
    run: () => runRuntimeSlashCommand(command.commandLine),
  }));

  const trimmedQuery = query.trim().toLowerCase();
  // With a query, search across ALL conversations by title + goal so the
  // palette acts as a global conversation search; without one, show the recent
  // 9 (with Ctrl+N shortcut hints) as quick switch targets.
  const conversationCandidates = conversations.filter(
    (c) => !c.archived && c.id !== conversationId,
  );
  const matchedConversations = trimmedQuery && globalSearchEnabled
    ? conversationCandidates.filter((c) =>
        `${c.title ?? ""} ${c.goal?.text ?? ""}`.toLowerCase().includes(trimmedQuery),
      )
    : conversationCandidates.slice(0, 9);
  const recentConversationActions: PaletteAction[] = matchedConversations.map((c, i) => {
    const project = workspaceDisplayName(c.workspaceRoot || c.worktreePath, "Computer");
    const branch = c.gitBranch ? ` · ${c.gitBranch}` : "";
    const shortcut = trimmedQuery ? "" : ` · Ctrl+${i + 1}`;
    const goalHint = trimmedQuery && c.goal?.text ? ` · ${c.goal.text.slice(0, 60)}` : "";
    return {
      id: `conversation.switch.${c.id}`,
      label: c.title || "Untitled",
      hint: `${project}${branch}${shortcut}${goalHint}`,
      run: () => useAppStore.getState().requestConversationSwitch(c.id),
    };
  });

  const actions: PaletteAction[] = [
    ...recentConversationActions,
    {
      id: "conversation.new",
      label: "New conversation",
      hint: "Ctrl+N",
      run: () => {
        const state = useAppStore.getState();
        state.createConversation({ appMode: state.appMode, bindWorkspace: Boolean(state.workingDirectory) });
      },
    },
    {
      id: "conversation.new.isolated",
      label: "New protected session",
      hint: "separate branch",
      run: () => {
        const state = useAppStore.getState();
        sendClientCommand({
          type: "conversation.create",
          git_isolated: true,
          workspace_root: state.workingDirectory,
          permission_mode: toBackendPermissionMode(state.permissionMode),
        });
      },
    },
    {
      id: "theme.cycle",
      label: "Cycle theme",
      hint: `current: ${themeMode}`,
      run: () => {
        const next = themeMode === "dark" ? "light" : themeMode === "light" ? "system" : "dark";
        setThemeMode(next);
      },
    },
    {
      id: "settings",
      label: "Open settings",
      hint: "Ctrl+,",
      run: () => openSettings(),
    },
    {
      id: "panel.editor",
      label: "Open editor panel",
      hint: "workspace file",
      run: () =>
        addPanel({ id: `editor-${Date.now()}`, kind: "editor", label: "Editor" }),
    },
    {
      id: "preview.open",
      label: "Open preview pane",
      hint: "stack",
      run: () => openRightStack("preview"),
    },
    {
      id: "preview.detect",
      label: "Detect dev servers",
      hint: "Preview Pane",
      run: () => {
        useAppStore.getState().setPreviewServers([]);
        openRightStack("preview");
        sendClientCommand({ type: "preview.detect" });
      },
    },
    {
      id: "preview.open.current",
      label: "Open current app preview",
      hint: useAppStore.getState().livePreviewUrl ?? "No URL",
      run: () => {
        const url = useAppStore.getState().livePreviewUrl;
        if (url) openLivePreview(url);
        else openRightStack("preview");
      },
    },
    {
      id: "panel.diff",
      label: "Open diff panel",
      hint: "review changes",
      run: () => addPanel({ id: `diff-${Date.now()}`, kind: "diff", label: "Diff" }),
    },
    {
      id: "terminal.openRight",
      label: "Open terminal",
      hint: "Ctrl+J",
      run: () => openRightStack("terminal"),
    },
    {
      id: "output.open",
      label: "Open output",
      hint: "sources and artifacts",
      run: () => openRightStack("tasks"),
    },
    {
      id: "panel.subagents",
      label: "Open subagents",
      hint: "/agents",
      run: () => openRightStack("subagents"),
    },
    {
      id: "panel.inspector",
      label: "Open inspector",
      run: () => openRightStack("inspector"),
    },
    {
      id: "diagnostics.open",
      label: "Open diagnostics",
      hint: "Doctor",
      run: () => openRightStack("diagnostics"),
    },
    {
      id: "doctor.run",
      label: "Run Doctor",
      hint: "/api/doctor",
      run: () => openRightStack("diagnostics"),
    },
    {
      id: "pane.focus.chat",
      label: "Focus chat pane",
      hint: "Workbench",
      run: () => {
        const slot = useAppStore.getState().panelSlots.find((p) => p.kind === "chat");
        if (slot) focusPanel(slot.id);
      },
    },
    {
      id: "pane.focus.editor",
      label: "Focus editor pane",
      hint: "Workbench",
      run: () => {
        const slot = useAppStore.getState().panelSlots.find((p) => p.kind === "editor");
        if (slot) focusPanel(slot.id);
        else addPanel({ id: `editor-${Date.now()}`, kind: "editor", label: "Editor" });
      },
    },
    {
      id: "pane.maximize",
      label: focusedPane?.maximized ? "Restore focused pane" : "Maximize focused pane",
      hint: focusedPane?.label ?? focusedPane?.kind,
      run: () => {
        const slot = useAppStore.getState().panelSlots.find((p) => p.focused) ?? useAppStore.getState().panelSlots[0];
        if (slot) togglePanelMaximized(slot.id);
      },
    },
    {
      id: "pane.close",
      label: "Close focused pane",
      hint: "Ctrl+\\",
      run: () => {
        const state = useAppStore.getState();
        const slot = state.panelSlots.find((p) => p.focused);
        if (slot && state.panelSlots.length > 1) removePanel(slot.id);
      },
    },
    {
      id: "layout.reset",
      label: "Reset workbench layout",
      hint: "Chat + Editor + Stack",
      run: () => resetPanelLayout(),
    },
    {
      id: "dock.git",
      label: "Open Git",
      hint: "changes",
      run: () => openDockTab("git"),
    },
    {
      id: "dock.timeline",
      label: "Open Activity",
      hint: "tool activity",
      run: () => openDockTab("timeline"),
    },
    ...(globalSearchEnabled ? [{
      id: "quick.open",
      label: "Quick open file",
      hint: "Ctrl+P",
      run: () => useAppStore.getState().toggleQuickOpen(),
    }] : []),
    {
      id: "sidebar.toggle",
      label: "Toggle left sidebar",
      hint: "Ctrl+B",
      run: () => {
        const store = useAppStore.getState();
        store.setLeftSidebarWidth(store.leftSidebarWidth > 0 ? 0 : 320);
      },
    },
    {
      id: "interrupt",
      label: "Interrupt streaming",
      hint: "Esc",
      run: () => {
        const conversationId = useAppStore.getState().conversationId;
        sendClientCommand({
          type: "interrupt",
          ...(conversationId ? { conversation_id: conversationId } : {}),
        });
      },
    },
    {
      id: "clear",
      label: "Clear conversation",
      hint: "/clear",
      run: () => runRuntimeSlashCommand("/clear"),
    },
    {
      id: "compact",
      label: "Compact context",
      hint: "/compact",
      run: () => runRuntimeSlashCommand("/compact"),
    },
    {
      id: "skills.marketplace",
      label: "Skills Marketplace",
      hint: "browse and install",
      run: () => runRuntimeSlashCommand("/skills"),
    },
    ...(agentEditorEnabled ? [{
      id: "agents.editor",
      label: "Agent editor",
      hint: "create/edit subagent roles",
      run: () => useAppStore.getState().toggleAgentEditor(),
    }] : []),
    ...runtimeSlashActions,
  ];

  const filtered = query
    ? actions.filter((a) => `${a.label} ${a.hint ?? ""}`.toLowerCase().includes(query.toLowerCase()))
    : actions;
  const activeOptionId = filtered[activeIdx] ? `command-palette-option-${filtered[activeIdx].id}` : undefined;

  const runAction = (idx: number) => {
    if (filtered[idx]) {
      filtered[idx].run();
      useAppStore.setState({ commandPaletteOpen: false });
    }
  };

  return (
    <div
      className="overlay-backdrop"
      onClick={closePaletteIfIdle}
      style={{
        position: "fixed",
        inset: 0,
        background: "var(--backdrop-overlay)",
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "center",
        padding: "12vh 16px 16px",
        zIndex: "var(--z-modal)",
        pointerEvents: "auto",
      }}
    >
      <div
        ref={dialogRef}
        className="modal-content"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "min(560px, 100%)",
          background: "var(--surface-raised)",
          border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-md, 12px)",
          boxShadow: "var(--shadow-strong, var(--shadow-md))",
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
          pointerEvents: "auto",
        }}
      >
        <input
          role="combobox"
          aria-expanded="true"
          aria-controls="command-palette-listbox"
          aria-activedescendant={activeOptionId}
          aria-autocomplete="list"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setActiveIdx(0); }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setActiveIdx((i) => filtered.length ? (i + 1) % filtered.length : 0);
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActiveIdx((i) => filtered.length ? (i - 1 + filtered.length) % filtered.length : 0);
            } else if (e.key === "Enter") {
              e.preventDefault();
              runAction(activeIdx);
            }
          }}
          placeholder="Type a command..."
          style={{
            background: "transparent",
            border: 0,
            padding: "14px 16px",
            color: "var(--text-primary)",
            fontSize: "var(--text-md)",
            outline: 0,
          }}
        />
        <div
          id="command-palette-listbox"
          role="listbox"
          aria-label="Commands"
          style={{
            borderTop: "1px solid var(--border-subtle)",
            maxHeight: 360,
            overflowY: "auto",
          }}
        >
          {filtered.length === 0 ? (
            <div
              style={{
                padding: 14,
                color: "var(--text-muted)",
                fontSize: "var(--text-sm)",
              }}
            >
              No matches.
            </div>
          ) : (
            filtered.map((a, i) => (
              <button
                key={a.id}
                id={`command-palette-option-${a.id}`}
                role="option"
                aria-selected={i === activeIdx}
                onClick={() => runAction(i)}
                onMouseEnter={() => setActiveIdx(i)}
                style={{
                  width: "100%",
                  textAlign: "left",
                  padding: "10px 16px",
                  background: i === activeIdx ? "var(--surface-active)" : "transparent",
                  border: 0,
                  cursor: "pointer",
                  color: "var(--text-primary)",
                  fontSize: "var(--text-sm)",
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                }}
              >
                <span style={{ flex: 1 }}>{a.label}</span>
                {a.hint && (
                  <span
                    style={{
                      fontFamily: "var(--font-mono)",
                      fontSize: "var(--text-xs)",
                      color: "var(--text-muted)",
                      background: "var(--surface-soft)",
                      padding: "1px 6px",
                      borderRadius: 4,
                    }}
                  >
                    {a.hint}
                  </span>
                )}
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
};
