import { useEffect, useRef, useState } from "react";
import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";
import { sendChatMessage } from "../chat/sendChatMessage";
import { toBackendPermissionMode } from "../protocol/permissions";

interface PaletteAction {
  id: string;
  label: string;
  hint?: string;
  run: () => void;
}

export const CommandPalette = () => {
  const commandPaletteOpen = useAppStore((s) => s.commandPaletteOpen);
  const themeMode = useAppStore((s) => s.themeMode);
  const panelSlots = useAppStore((s) => s.panelSlots);
  const toggleCommandPalette = useAppStore((s) => s.toggleCommandPalette);
  const setThemeMode = useAppStore((s) => s.setThemeMode);
  const addPanel = useAppStore((s) => s.addPanel);
  const openLivePreview = useAppStore((s) => s.openLivePreview);
  const toggleSkillsMarketplace = useAppStore((s) => s.toggleSkillsMarketplace);
  const focusPanel = useAppStore((s) => s.focusPanel);
  const togglePanelMaximized = useAppStore((s) => s.togglePanelMaximized);
  const removePanel = useAppStore((s) => s.removePanel);
  const resetPanelLayout = useAppStore((s) => s.resetPanelLayout);
  const setActiveBottomTab = useAppStore((s) => s.setActiveBottomTab);
  const setRightStackTab = useAppStore((s) => s.setRightStackTab);
  const setAppMode = useAppStore((s) => s.setAppMode);
  const [query, setQuery] = useState("");
  const [activeIdx, setActiveIdx] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (commandPaletteOpen) {
      setQuery("");
      setActiveIdx(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [commandPaletteOpen]);

  useEffect(() => {
    if (!commandPaletteOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      const state = useAppStore.getState();
      if (state.pendingApproval || state.pendingDiffReview || state.pendingAskUser) return;
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

  const actions: PaletteAction[] = [
    {
      id: "conversation.new",
      label: "New conversation",
      hint: "Ctrl+N",
      run: () => useAppStore.getState().createConversation(),
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
      run: () => useAppStore.getState().toggleSettings(),
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
      id: "plan.open",
      label: "Open plan",
      hint: "panel",
      run: () => openRightStack("plan"),
    },
    {
      id: "tasks.open",
      label: "Open tasks",
      hint: "stack",
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
      label: "Open Timeline",
      hint: "tool calls",
      run: () => openDockTab("timeline"),
    },
    {
      id: "quick.open",
      label: "Quick open file",
      hint: "Ctrl+P",
      run: () => useAppStore.setState({ quickOpenVisible: true }),
    },
    {
      id: "sidebar.toggle",
      label: "Toggle left sidebar",
      hint: "Ctrl+B",
      run: () => {
        const cur = useAppStore.getState().leftSidebarWidth;
        useAppStore.setState({ leftSidebarWidth: cur > 0 ? 0 : 280 });
      },
    },
    {
      id: "plan.request",
      label: "Switch to Plan mode",
      hint: "read-only",
      run: () => {
        useAppStore.getState().setPermissionMode("plan");
        useAppStore.getState().setRightStackTab("plan");
      },
    },
    {
      id: "interrupt",
      label: "Interrupt streaming",
      hint: "Esc",
      run: () => {
        sendClientCommand({ type: "interrupt" });
      },
    },
    {
      id: "clear",
      label: "Clear conversation",
      hint: "/clear",
      run: async () => {
        const { showConfirm } = await import("./DialogService");
        const ok = await showConfirm({
          title: "Clear conversation",
          message: "Clear all messages in the current conversation view? This cannot be undone.",
          confirmLabel: "Clear",
          danger: true,
        });
        if (!ok) return;
        const state = useAppStore.getState();
        if (state.conversationId) {
          state.hydrateConversationMessages(state.conversationId, [], { activate: true, isStreaming: false });
        } else {
          useAppStore.setState({ messages: [], isStreaming: false });
        }
      },
    },
    {
      id: "compact",
      label: "Compact context",
      hint: "/compact",
      run: () => {
        sendChatMessage({ displayContent: "/compact", backendContent: "/compact" });
      },
    },
    {
      id: "skills.marketplace",
      label: "Skills Marketplace",
      hint: "browse and install",
      run: () => toggleSkillsMarketplace(),
    },
  ];

  const filtered = query
    ? actions.filter((a) => a.label.toLowerCase().includes(query.toLowerCase()))
    : actions;
  const activeOptionId = filtered[activeIdx] ? `command-palette-option-${filtered[activeIdx].id}` : undefined;

  const runAction = (idx: number) => {
    if (filtered[idx]) {
      filtered[idx].run();
      toggleCommandPalette();
    }
  };

  return (
    <div
      className="overlay-backdrop"
      onClick={toggleCommandPalette}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.5)",
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "center",
        padding: "12vh 16px 16px",
        zIndex: 100,
      }}
    >
      <div
        className="modal-content"
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
        }}
      >
        <input
          ref={inputRef}
          role="combobox"
          aria-expanded="true"
          aria-controls="command-palette-listbox"
          aria-activedescendant={activeOptionId}
          aria-autocomplete="list"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setActiveIdx(0); }}
          onKeyDown={(e) => {
            if (e.key === "Escape") toggleCommandPalette();
            else if (e.key === "ArrowDown") {
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
