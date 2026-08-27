import { Fragment, useEffect, useState } from "react";
import { useAppStore } from "../stores";
import { LEFT_SIDEBAR_DEFAULT_WIDTH } from "../stores/shared-helpers";
import { sendClientCommand } from "../protocol/ws-outbox";
import { sendChatMessage } from "../chat/sendChatMessage";
import { toBackendPermissionMode } from "../protocol/permissions";
import { hasRuntimePendingUserAction, hasRuntimePendingUserActionForConversation } from "../lib/runtime-session";
import { buildRuntimeSlashPaletteItems, executeRuntimeSlashCommand } from "../lib/runtime-commands";
import { buildInterruptCommand } from "../lib/interrupt-command";
import { hasLocalPendingPromptForConversation } from "../lib/pending-prompts";
import { openSettings } from "../lib/settings-navigation";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import { useFocusTrap } from "../hooks/useFocusTrap";

interface PaletteAction {
  id: string;
  label: string;
  hint?: string;
  group: "最近会话" | "导航" | "工作区" | "命令";
  run: () => void | Promise<void>;
}

/* Bold the first case-insensitive occurrence of the query inside a label so
   filtering results show WHY each row matched. */
const highlightMatch = (label: string, query: string): React.ReactNode => {
  const q = query.trim();
  if (!q) return label;
  const idx = label.toLowerCase().indexOf(q.toLowerCase());
  if (idx < 0) return label;
  return (
    <>
      {label.slice(0, idx)}
      <strong style={{ color: "var(--accent-primary)", fontWeight: "var(--fw-semibold)" }}>
        {label.slice(idx, idx + q.length)}
      </strong>
      {label.slice(idx + q.length)}
    </>
  );
};

const hasPendingUserAction = (): boolean => {
  const state = useAppStore.getState();
  const activeConversationId = state.conversationId ?? state.runtimeSession?.active_conversation_id;
  const hasRuntimePending = activeConversationId
    ? hasRuntimePendingUserActionForConversation(state.runtimeSession, activeConversationId)
    : hasRuntimePendingUserAction(state.runtimeSession);
  return Boolean(
    hasLocalPendingPromptForConversation(
      [
        state.pendingApproval,
        ...state.approvalQueue,
        state.pendingDiffReview,
        ...state.diffReviewQueue,
        state.pendingAskUser,
        ...state.askUserQueue,
      ],
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
  const openBottomTab = useAppStore((s) => s.openBottomTab);
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
  const openDockTab = (tab: "terminal" | "git" | "timeline" | "debug" | "budget") => {
    setAppMode("code");
    openBottomTab(tab);
  };
  const openRightStack = (tab: "preview" | "tasks" | "plan" | "subagents" | "inspector" | "diagnostics") => {
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
          title: "清空会话",
          message: "清空当前会话视图中的所有消息？此操作无法撤销。",
          confirmLabel: "清空",
          danger: true,
        });
      },
    });
  };
  const runtimeSlashActions: PaletteAction[] = buildRuntimeSlashPaletteItems(slashCommands, {
    exclude: ["clear", "skills", "compact", "new"],
  }).map((command) => ({
    id: command.id,
    label: `运行 ${command.name}`,
    hint: command.description || "斜杠命令",
    group: "命令",
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
    const shortcut = trimmedQuery ? "" : `Ctrl+${i + 1}`;
    const goalHint = trimmedQuery && c.goal?.text ? c.goal.text.slice(0, 60) : "";
    return {
      id: `conversation.switch.${c.id}`,
      label: c.title || "未命名会话",
      hint: goalHint || shortcut,
      group: "最近会话",
      run: () => useAppStore.getState().requestConversationSwitch(c.id),
    };
  });

  const unsortedActions: PaletteAction[] = [
    ...recentConversationActions,
    {
      id: "conversation.new",
      label: "新建会话",
      hint: "Ctrl+N",
      group: "导航",
      run: () => {
        const state = useAppStore.getState();
        state.createConversation({ appMode: state.appMode, bindWorkspace: Boolean(state.workingDirectory) });
      },
    },
    {
      id: "conversation.new.isolated",
      label: "新建隔离会话",
      hint: "独立分支",
      group: "导航",
      run: () => {
        const state = useAppStore.getState();
        sendClientCommand({
          type: "conversation.create",
          conversation_type: "main",
          git_isolated: true,
          workspace_root: state.workingDirectory,
          permission_mode: toBackendPermissionMode(state.permissionMode),
        });
      },
    },
    {
      id: "theme.cycle",
      label: "切换主题",
      hint: `当前：${themeMode}`,
      group: "导航",
      run: () => {
        const next = themeMode === "dark" ? "light" : themeMode === "light" ? "system" : "dark";
        setThemeMode(next);
      },
    },
    {
      id: "settings",
      label: "打开设置",
      hint: "Ctrl+,",
      group: "导航",
      run: () => openSettings(),
    },
    {
      id: "panel.editor",
      label: "打开编辑器",
      hint: "工作区文件",
      group: "工作区",
      run: () =>
        addPanel({ id: `editor-${Date.now()}`, kind: "editor", label: "Editor" }),
    },
    {
      id: "preview.open",
      label: "打开预览",
      hint: "右侧面板",
      group: "工作区",
      run: () => openRightStack("preview"),
    },
    {
      id: "preview.detect",
      label: "检测开发服务器",
      hint: "预览面板",
      group: "工作区",
      run: () => {
        useAppStore.getState().setPreviewServers([]);
        openRightStack("preview");
        sendClientCommand({ type: "preview.detect" });
      },
    },
    {
      id: "preview.open.current",
      label: "打开当前应用预览",
      hint: useAppStore.getState().livePreviewUrl ?? "暂无地址",
      group: "工作区",
      run: () => {
        const url = useAppStore.getState().livePreviewUrl;
        if (url) openLivePreview(url);
        else openRightStack("preview");
      },
    },
    {
      id: "panel.diff",
      label: "打开差异面板",
      hint: "审阅改动",
      group: "工作区",
      run: () => addPanel({ id: `diff-${Date.now()}`, kind: "diff", label: "Diff" }),
    },
    {
      id: "output.open",
      label: "打开上下文",
      hint: "来源与产物",
      group: "工作区",
      run: () => openRightStack("tasks"),
    },
    {
      id: "panel.subagents",
      label: "打开子智能体",
      hint: "/agents",
      group: "工作区",
      run: () => openRightStack("subagents"),
    },
    {
      id: "panel.inspector",
      label: "打开检查器",
      group: "工作区",
      run: () => openRightStack("inspector"),
    },
    {
      id: "diagnostics.open",
      label: "打开运行状态",
      hint: "诊断",
      group: "工作区",
      run: () => openRightStack("diagnostics"),
    },
    {
      id: "doctor.run",
      label: "运行诊断",
      hint: "/api/doctor",
      group: "命令",
      run: () => openRightStack("diagnostics"),
    },
    {
      id: "pane.focus.chat",
      label: "聚焦对话",
      hint: "工作区",
      group: "工作区",
      run: () => {
        const slot = useAppStore.getState().panelSlots.find((p) => p.kind === "chat");
        if (slot) focusPanel(slot.id);
      },
    },
    {
      id: "pane.focus.editor",
      label: "聚焦编辑器",
      hint: "工作区",
      group: "工作区",
      run: () => {
        const slot = useAppStore.getState().panelSlots.find((p) => p.kind === "editor");
        if (slot) focusPanel(slot.id);
        else addPanel({ id: `editor-${Date.now()}`, kind: "editor", label: "Editor" });
      },
    },
    {
      id: "pane.maximize",
      label: focusedPane?.maximized ? "还原当前面板" : "最大化当前面板",
      hint: focusedPane?.label ?? focusedPane?.kind,
      group: "工作区",
      run: () => {
        const slot = useAppStore.getState().panelSlots.find((p) => p.focused) ?? useAppStore.getState().panelSlots[0];
        if (slot) togglePanelMaximized(slot.id);
      },
    },
    {
      id: "pane.close",
      label: "关闭当前面板",
      hint: "Ctrl+\\",
      group: "工作区",
      run: () => {
        const state = useAppStore.getState();
        const slot = state.panelSlots.find((p) => p.focused);
        if (slot && state.panelSlots.length > 1) removePanel(slot.id);
      },
    },
    {
      id: "layout.reset",
      label: "重置工作区布局",
      hint: "对话 + 编辑器 + 面板",
      group: "工作区",
      run: () => resetPanelLayout(),
    },
    {
      id: "dock.git",
      label: "打开 Git",
      hint: "代码改动",
      group: "工作区",
      run: () => openDockTab("git"),
    },
    {
      id: "dock.terminal",
      label: "打开终端",
      hint: "终端与进程",
      group: "工作区",
      run: () => openDockTab("terminal"),
    },
    ...(globalSearchEnabled ? [{
      id: "quick.open",
      label: "快速打开文件",
      hint: "Ctrl+P",
      group: "导航" as const,
      run: () => useAppStore.getState().toggleQuickOpen(),
    }] : []),
    {
      id: "sidebar.toggle",
      label: "切换左侧栏",
      hint: "Ctrl+B",
      group: "导航",
      run: () => {
        const store = useAppStore.getState();
        store.setLeftSidebarWidth(store.leftSidebarWidth > 0 ? 0 : LEFT_SIDEBAR_DEFAULT_WIDTH);
      },
    },
    {
      id: "interrupt",
      label: "停止当前任务",
      hint: "Esc",
      group: "命令",
      run: () => {
        const state = useAppStore.getState();
        const command = buildInterruptCommand(state);
        if (state.isStreaming) sendClientCommand(command);
      },
    },
    {
      id: "clear",
      label: "清空会话",
      hint: "/clear",
      group: "命令",
      run: () => runRuntimeSlashCommand("/clear"),
    },
    {
      id: "compact",
      label: "压缩上下文",
      hint: "/compact",
      group: "命令",
      run: () => runRuntimeSlashCommand("/compact"),
    },
    {
      id: "skills.marketplace",
      label: "技能市场",
      hint: "浏览与安装",
      group: "导航",
      run: () => runRuntimeSlashCommand("/skills"),
    },
    ...(agentEditorEnabled ? [{
      id: "agents.editor",
      label: "智能体编辑器",
      hint: "管理子智能体角色",
      group: "导航" as const,
      run: () => useAppStore.getState().toggleAgentEditor(),
    }] : []),
    ...runtimeSlashActions,
  ];
  const groupOrder: PaletteAction["group"][] = ["最近会话", "导航", "工作区", "命令"];
  const actions = [...unsortedActions].sort(
    (left, right) => groupOrder.indexOf(left.group) - groupOrder.indexOf(right.group),
  );

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
        className="modal-content command-palette-surface"
        role="dialog"
        aria-modal="true"
        aria-label="命令面板"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "min(560px, 100%)",
          background: "var(--surface-raised)",
          border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-lg)",
          boxShadow: "var(--shadow-strong-overlay)",
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
          pointerEvents: "auto",
        }}
      >
        <input
          className="command-palette-input"
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
          placeholder="搜索命令或会话…"
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
          className="command-palette-listbox"
          role="listbox"
          aria-label="命令与会话"
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
              没有匹配结果 — 试试其他关键词，或按 Esc 关闭
            </div>
          ) : (
            filtered.map((a, i) => (
              <Fragment key={a.id}>
                {(i === 0 || filtered[i - 1]?.group !== a.group) && (
                  <div
                    aria-hidden="true"
                    data-palette-group={a.group}
                    className="command-palette-group"
                    style={{
                      padding: i === 0 ? "8px 12px 4px" : "12px 12px 4px",
                      color: "var(--text-muted)",
                      fontSize: "var(--text-2xs)",
                      fontWeight: "var(--fw-semibold)",
                    }}
                  >
                    {a.group}
                  </div>
                )}
                <button
                id={`command-palette-option-${a.id}`}
                className="command-palette-option"
                role="option"
                aria-selected={i === activeIdx}
                onClick={() => runAction(i)}
                onMouseEnter={() => setActiveIdx(i)}
                style={{
                  width: "100%",
                  textAlign: "left",
                  padding: "8px 12px",
                  background: i === activeIdx ? "var(--surface-active)" : "transparent",
                  border: 0,
                  cursor: "pointer",
                  color: "var(--text-primary)",
                  fontSize: "var(--text-sm)",
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                }}
              >
                <span style={{ flex: 1 }}>{highlightMatch(a.label, query)}</span>
                {a.hint && (
                  <kbd
                    className="mc-kbd"
                    style={{
                      color: "var(--text-muted)",
                    }}
                  >
                    {a.hint}
                  </kbd>
                )}
                </button>
              </Fragment>
            ))
          )}
        </div>
        <div
          aria-hidden="true"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            padding: "8px 16px",
            borderTop: "1px solid var(--border-subtle)",
            color: "var(--text-muted)",
            fontSize: "var(--text-2xs)",
          }}
        >
          <span><kbd className="mc-kbd">↑↓</kbd> 导航</span>
          <span><kbd className="mc-kbd">↵</kbd> 选择</span>
          <span><kbd className="mc-kbd">Esc</kbd> 关闭</span>
        </div>
      </div>
    </div>
  );
};
