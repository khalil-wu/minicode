import { useI18n } from "../i18n";
import { Fragment, useEffect, useState, useSyncExternalStore } from "react";
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
import { openPreviewServiceManager } from "../lib/preview-server-actions";
import { openNewBrowserTab } from "../chat/openWebInBrowser";
import { formatShortcut } from "../lib/keyboard-shortcuts";
import { dispatchQueuedMessageHistory, queuedMessageHistorySnapshot, subscribeQueuedMessageHistory } from "../composer/queued-message-undo";
import { useConversationSearch } from "../hooks/useConversationSearch";
import { getWebSocket } from "../hooks/useWebSocket";
import { searchResultConversation } from "../protocol/conversation-search";
import { revealConversationMessage } from "../chat/revealConversationMessage";

interface PaletteAction {
  id: string;
  label: string;
  hint?: string;
  preview?: string;
  codeOnly?: boolean;
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
    ) ||
    hasRuntimePending,
  );
};

export const CommandPalette = () => {
  const { t: tr } = useI18n();
  const commandPaletteOpen = useAppStore((s) => s.commandPaletteOpen);
  const appMode = useAppStore((s) => s.appMode);
  const themeMode = useAppStore((s) => s.themeMode);
  const shortcuts = useAppStore((s) => s.shortcutBindings);
  const queueHistory = useSyncExternalStore(subscribeQueuedMessageHistory, queuedMessageHistorySnapshot);
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
  const openTerminal = useAppStore(s => s.openTerminal);
  const setRightStackTab = useAppStore((s) => s.setRightStackTab);
  const conversations = useAppStore((s) => s.conversations);
  const conversationId = useAppStore((s) => s.conversationId);
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities);
  const globalSearchEnabled = capabilityFeatureEnabled(runtimeCapabilities, "global_search", true);
  const [query, setQuery] = useState("");
  const [activeIdx, setActiveIdx] = useState(0);
  const bodySearch = useConversationSearch({ query, enabled: commandPaletteOpen && globalSearchEnabled });
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

  if (!commandPaletteOpen) return null;

  const focusedPane = panelSlots.find((slot) => slot.focused) ?? panelSlots[0];
  const runRuntimeSlashCommand = async (commandLine: string) => {
    await executeRuntimeSlashCommand(commandLine, {
      getState: useAppStore.getState,
      setState: useAppStore.setState,
      sendClientCommand,
      sendChatMessage,
      confirmClear: async () => {
        const { showConfirm } = await import("./DialogService");
        return showConfirm({
          title: tr("清空会话"),
          message: tr("清空当前会话视图中的所有消息？此操作无法撤销。"),
          confirmLabel: tr("清空"),
          danger: true,
        });
      },
    });
  };
  const runtimeSlashActions: PaletteAction[] = buildRuntimeSlashPaletteItems(slashCommands, {
    exclude: ["clear", "skills", "compact", "new"],
  }).map((command) => ({
    id: command.id,
    label: tr("运行 {v0}", { v0: command.name }),
    hint: command.description || tr("斜杠命令"),
    group: "命令",
    run: () => runRuntimeSlashCommand(command.commandLine),
  }));

  const trimmedQuery = query.trim().toLowerCase();
  const conversationCandidates = conversations.filter(
    (c) => !c.archived && c.conversationType !== "side_chat" && c.id !== conversationId,
  );
  const metadataMatches = trimmedQuery && globalSearchEnabled
    ? conversationCandidates.filter((c) =>
        `${c.title ?? ""} ${c.goal?.text ?? ""}`.toLowerCase().includes(trimmedQuery),
      )
    : conversationCandidates.slice(0, 9);
  const contentMatches = new Map(bodySearch.results.filter(result => result.conversation.id !== conversationId
    && !result.conversation.archived && result.conversation.conversation_type !== "side_chat"
    && !conversations.some(c => c.id === result.conversation.id && c.archived))
    .map(result => [result.conversation.id, result]));
  const matchedConversations = trimmedQuery && globalSearchEnabled
    ? [...new Map([...metadataMatches, ...[...contentMatches.values()].map(result =>
      conversationCandidates.find(c => c.id === result.conversation.id) || searchResultConversation(result))]
      .map(c => [c.id, c])).values()].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
    : metadataMatches;
  const recentConversationActions: PaletteAction[] = matchedConversations.map((c, i) => {
    const shortcut = trimmedQuery ? "" : `Ctrl+${i + 1}`;
    const goal = c.goal?.text ?? "";
    const goalStart = Math.max(0, goal.toLowerCase().indexOf(trimmedQuery) - 20);
    const goalHint = trimmedQuery && goal
      ? `${goalStart ? "…" : ""}${goal.slice(goalStart, goalStart + 80)}` : "";
    const match = contentMatches.get(c.id);
    return {
      id: `conversation.switch.${c.id}`,
      label: c.title || tr("未命名会话"),
      hint: shortcut,
      preview: match?.snippet || goalHint,
      group: "最近会话",
      run: () => {
        if (match) {
          if (getWebSocket()?.sessionId === bodySearch.sessionId) revealConversationMessage(c.id, match.message_id);
        } else useAppStore.getState().requestConversationSwitch(c.id);
      },
    };
  });

  const unsortedActions: PaletteAction[] = [
    ...recentConversationActions,
    ...(queueHistory.undo ? [{ id: "undoAppAction", label: tr("撤销"), hint: formatShortcut(shortcuts.undoAppAction),
      group: "命令" as const, run: () => { dispatchQueuedMessageHistory("undo"); } }] : []),
    ...(queueHistory.redo ? [{ id: "redoAppAction", label: tr("重做"), hint: formatShortcut(shortcuts.redoAppAction),
      group: "命令" as const, run: () => { dispatchQueuedMessageHistory("redo"); } }] : []),
    {
      id: "conversation.new",
      label: tr("新建会话"),
      hint: formatShortcut(shortcuts.newConversation),
      group: "导航",
      run: () => {
        const state = useAppStore.getState();
        state.createConversation({ appMode: state.appMode, bindWorkspace: Boolean(state.workingDirectory) });
      },
    },
    {
      id: "conversation.new.isolated",
      codeOnly: true,
      label: tr("新建隔离会话"),
      hint: tr("独立分支"),
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
      codeOnly: true,
      label: tr("切换主题"),
      hint: tr("当前：{v0}", { v0: themeMode }),
      group: "导航",
      run: () => {
        const next = themeMode === "dark" ? "light" : themeMode === "light" ? "system" : "dark";
        setThemeMode(next);
      },
    },
    {
      id: "settings",
      label: tr("打开设置"),
      hint: formatShortcut(shortcuts.settings),
      group: "导航",
      run: () => openSettings(),
    },
    {
      id: "panel.editor",
      codeOnly: true,
      label: tr("打开编辑器"),
      hint: tr("工作区文件"),
      group: "工作区",
      run: () =>
        addPanel({ id: `editor-${Date.now()}`, kind: "editor", label: "Editor" }),
    },
    ...(globalSearchEnabled ? [{
      id: "workspace.search.content",
      codeOnly: true,
      label: tr("搜索项目内容"),
      hint: formatShortcut(shortcuts.workspaceSearch),
      group: "工作区" as const,
      run: () => useAppStore.getState().openWorkspaceSearch(),
    }] : []),
    {
      id: "workspace.problems",
      codeOnly: true,
      label: tr("查看代码问题"),
      hint: tr("诊断与修复"),
      group: "工作区",
      run: () => openBottomTab("problems"),
    },
    {
      id: "preview.servers",
      codeOnly: true,
      label: tr("管理预览服务"),
      hint: tr("启动、停止、日志与配置"),
      group: "工作区",
      run: openPreviewServiceManager,
    },
    {
      id: "preview.open",
      codeOnly: true,
      label: tr("打开预览"),
      hint: tr("右侧面板"),
      group: "工作区",
      run: () => setRightStackTab("preview"),
    },
    {
      id: "preview.detect",
      codeOnly: true,
      label: tr("检测开发服务器"),
      hint: tr("预览面板"),
      group: "工作区",
      run: () => {
        useAppStore.getState().setPreviewServers([]);
        setRightStackTab("preview");
        sendClientCommand({ type: "preview.detect" });
      },
    },
    {
      id: "preview.open.current",
      codeOnly: true,
      label: tr("打开当前应用预览"),
      hint: useAppStore.getState().livePreviewUrl ?? tr("暂无地址"),
      group: "工作区",
      run: () => {
        const url = useAppStore.getState().livePreviewUrl;
        if (url) openLivePreview(url);
        else setRightStackTab("preview");
      },
    },
    {
      id: "panel.diff",
      label: tr("打开差异面板"),
      hint: tr("审阅改动"),
      group: "工作区",
      run: () => setRightStackTab("diff"),
    },
    {
      id: "browser.open",
      label: tr("打开浏览器"),
      hint: tr("新建标签页"),
      group: "工作区",
      run: openNewBrowserTab,
    },
    {
      id: "output.open",
      label: tr("打开上下文"),
      hint: tr("来源与产物"),
      group: "工作区",
      run: () => setRightStackTab("tasks"),
    },
    {
      id: "panel.subagents",
      codeOnly: true,
      label: tr("打开子智能体"),
      hint: "/agents",
      group: "工作区",
      run: () => setRightStackTab("subagents"),
    },
    {
      id: "pane.focus.chat",
      label: tr("聚焦对话"),
      hint: tr("工作区"),
      group: "工作区",
      run: () => {
        const slot = useAppStore.getState().panelSlots.find((p) => p.kind === "chat");
        if (slot) focusPanel(slot.id);
      },
    },
    {
      id: "pane.focus.editor",
      codeOnly: true,
      label: tr("聚焦编辑器"),
      hint: tr("工作区"),
      group: "工作区",
      run: () => {
        const slot = useAppStore.getState().panelSlots.find((p) => p.kind === "editor");
        if (slot) focusPanel(slot.id);
        else addPanel({ id: `editor-${Date.now()}`, kind: "editor", label: "Editor" });
      },
    },
    {
      id: "pane.maximize",
      codeOnly: true,
      label: focusedPane?.maximized ? tr("还原当前面板") : tr("最大化当前面板"),
      hint: focusedPane?.label ?? focusedPane?.kind,
      group: "工作区",
      run: () => {
        const slot = useAppStore.getState().panelSlots.find((p) => p.focused) ?? useAppStore.getState().panelSlots[0];
        if (slot) togglePanelMaximized(slot.id);
      },
    },
    {
      id: "pane.close",
      codeOnly: true,
      label: tr("关闭当前面板"),
      hint: formatShortcut(shortcuts.closePanel),
      group: "工作区",
      run: () => {
        const state = useAppStore.getState();
        const slot = state.panelSlots.find((p) => p.focused);
        if (slot && state.panelSlots.length > 1) removePanel(slot.id);
      },
    },
    {
      id: "layout.reset",
      codeOnly: true,
      label: tr("重置工作区布局"),
      hint: tr("对话 + 编辑器 + 面板"),
      group: "工作区",
      run: () => resetPanelLayout(),
    },
    {
      id: "dock.git",
      codeOnly: true,
      label: tr("打开 Git"),
      hint: tr("代码改动"),
      group: "工作区",
      run: () => openBottomTab("git"),
    },
    {
      id: "dock.terminal",
      label: tr("打开终端"),
      hint: tr("终端与进程"),
      group: "工作区",
      run: openTerminal,
    },
    ...(globalSearchEnabled ? [{
      id: "quick.open",
      label: tr("快速打开文件"),
      hint: formatShortcut(shortcuts.globalSearch),
      group: "导航" as const,
      run: () => useAppStore.getState().toggleQuickOpen(),
    }] : []),
    {
      id: "sidebar.toggle",
      label: tr("切换左侧栏"),
      hint: formatShortcut(shortcuts.leftSidebar),
      group: "导航",
      run: () => {
        const store = useAppStore.getState();
        store.setLeftSidebarWidth(store.leftSidebarWidth > 0 ? 0 : LEFT_SIDEBAR_DEFAULT_WIDTH);
      },
    },
    {
      id: "interrupt",
      label: tr("停止当前任务"),
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
      label: tr("清空会话"),
      hint: "/clear",
      group: "命令",
      run: () => runRuntimeSlashCommand("/clear"),
    },
    {
      id: "compact",
      label: tr("压缩上下文"),
      hint: "/compact",
      group: "命令",
      run: () => runRuntimeSlashCommand("/compact"),
    },
    {
      id: "skills.marketplace",
      label: tr("技能市场"),
      hint: tr("浏览与安装"),
      group: "导航",
      run: () => runRuntimeSlashCommand("/skills"),
    },
    ...runtimeSlashActions,
  ];
  const groupOrder: PaletteAction["group"][] = ["最近会话", "导航", "工作区", "命令"];
  const actions = unsortedActions.filter(action => !action.codeOnly || appMode === "code").sort(
    (left, right) => groupOrder.indexOf(left.group) - groupOrder.indexOf(right.group),
  );

  const filtered = trimmedQuery
    ? actions.filter((a) => a.group === "最近会话" && globalSearchEnabled
      || `${a.label} ${a.hint ?? ""}`.toLowerCase().includes(trimmedQuery))
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
        aria-label={tr("命令面板")}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key !== "Escape" || e.nativeEvent.isComposing) return;
          e.preventDefault();
          e.stopPropagation();
          closePaletteIfIdle();
        }}
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
            if (e.nativeEvent.isComposing || e.keyCode === 229) return;
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
          placeholder={tr("搜索命令或会话…")}
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
          aria-label={tr("命令与会话")}
          onScroll={event => {
            const list = event.currentTarget;
            if (list.scrollHeight - list.scrollTop - list.clientHeight < 80) bodySearch.loadMore();
          }}
          style={{
            borderTop: "1px solid var(--border-subtle)",
            maxHeight: 360,
            overflowY: "auto",
          }}
        >
          {filtered.length === 0 && !bodySearch.loading && !bodySearch.error ? (
            <div
              style={{
                padding: 14,
                color: "var(--text-muted)",
                fontSize: "var(--text-sm)",
              }}
            >
              {tr("没有匹配结果 — 试试其他关键词，或按 Esc 关闭")}</div>
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
                    {tr(a.group)}
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
                <span style={{ flex: 1, minWidth: 0, display: "grid", gap: 3 }}>
                  <span>{highlightMatch(a.label, query)}</span>
                  {a.preview && <span className="command-palette-preview" style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)",
                    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{highlightMatch(a.preview, query)}</span>}
                </span>
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
          {bodySearch.loading && <div role="status" style={{ padding: "8px 12px", color: "var(--text-muted)" }}>{tr("正在搜索…")}</div>}
          {bodySearch.error && <div role="alert" style={{ padding: "8px 12px", color: "var(--text-secondary)" }}>{bodySearch.error}</div>}
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
          <span><kbd className="mc-kbd">↑↓</kbd> {tr("导航")}</span>
          <span><kbd className="mc-kbd">↵</kbd> {tr("选择")}</span>
          <span><kbd className="mc-kbd">Esc</kbd> {tr("关闭")}</span>
        </div>
      </div>
    </div>
  );
};
