import { useEffect, useState, useMemo, useCallback } from "react";
import {
  Plus,
  ChevronDown,
  ChevronUp,
  Trash2,
  Copy,
  FolderOpen,
  Download,
  RotateCw,
  Search,
  Command,
  FileText,
  Settings,
  Zap,
} from "lucide-react";
import { useAppStore } from "../../stores";
import { createTodoItem } from "../../lib/todo-utils";
import "./command-palette.css";

interface Command {
  id: string;
  label: string;
  description: string;
  icon: React.ReactNode;
  hotkey?: string;
  category: "tasks" | "messages" | "view" | "file" | "settings";
  action: () => void;
}

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
}

export function CommandPalette({ open, onClose }: CommandPaletteProps) {
  const [search, setSearch] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);

  const addTodo = useAppStore((s) => s.addTodo);
  const todos = useAppStore((s) => s.todos);
  const messages = useAppStore((s) => s.messages);

  const commands: Command[] = useMemo(
    () => [
      // Tasks
      {
        id: "new-task",
        label: "新建任务",
        description: "添加一个新任务到清单",
        icon: <Plus size={16} />,
        hotkey: "Cmd+T",
        category: "tasks",
        action: () => {
          addTodo({
            id: `todo-${Date.now()}`,
            content: "新任务",
            activeForm: "",
            status: "pending",
          });
          onClose();
        },
      },
      {
        id: "collapse-tasks",
        label: "折叠所有任务",
        description: "收起任务清单",
        icon: <ChevronUp size={16} />,
        category: "tasks",
        action: () => {
          // TODO: Implement collapse all
          onClose();
        },
      },
      {
        id: "expand-tasks",
        label: "展开所有任务",
        description: "展开任务清单",
        icon: <ChevronDown size={16} />,
        category: "tasks",
        action: () => {
          // TODO: Implement expand all
          onClose();
        },
      },
      {
        id: "clear-completed",
        label: "清除已完成任务",
        description: "删除所有已完成的任务",
        icon: <Trash2 size={16} />,
        category: "tasks",
        action: () => {
          todos
            .filter((t) => t.status === "completed")
            .forEach((t) => useAppStore.getState().removeTodo(t.id));
          onClose();
        },
      },
      {
        id: "export-tasks",
        label: "导出任务列表",
        description: "将任务导出为 Markdown",
        icon: <Download size={16} />,
        hotkey: "Cmd+E",
        category: "tasks",
        action: () => {
          const markdown = todos
            .map((t) => {
              const status = t.status === "completed" ? "x" : " ";
              return `- [${status}] ${t.content}`;
            })
            .join("\n");
          navigator.clipboard.writeText(markdown);
          onClose();
        },
      },

      // Messages
      {
        id: "copy-conversation",
        label: "复制对话",
        description: "复制整个对话到剪贴板",
        icon: <Copy size={16} />,
        hotkey: "Cmd+Shift+C",
        category: "messages",
        action: () => {
          const text = messages
            .map((m) => `${m.role === "user" ? "User" : "Assistant"}: ${m.content}`)
            .join("\n\n");
          navigator.clipboard.writeText(text);
          onClose();
        },
      },
      {
        id: "clear-conversation",
        label: "清空对话",
        description: "删除所有消息",
        icon: <Trash2 size={16} />,
        category: "messages",
        action: () => {
          if (confirm("确定要清空所有消息吗？")) {
            messages.forEach((m) => useAppStore.getState().deleteMessage(m.id));
          }
          onClose();
        },
      },
      {
        id: "regenerate-last",
        label: "重新生成最后回复",
        description: "重新生成最后一条助手回复",
        icon: <RotateCw size={16} />,
        hotkey: "Cmd+R",
        category: "messages",
        action: () => {
          // TODO: Implement regenerate
          onClose();
        },
      },

      // View
      {
        id: "search-messages",
        label: "搜索消息",
        description: "在对话中搜索",
        icon: <Search size={16} />,
        hotkey: "Cmd+F",
        category: "view",
        action: () => {
          // TODO: Implement search
          onClose();
        },
      },
      {
        id: "open-file",
        label: "打开文件",
        description: "快速打开项目文件",
        icon: <FolderOpen size={16} />,
        hotkey: "Cmd+P",
        category: "file",
        action: () => {
          // TODO: Implement file picker
          onClose();
        },
      },

      // Settings
      {
        id: "settings",
        label: "设置",
        description: "打开设置面板",
        icon: <Settings size={16} />,
        hotkey: "Cmd+,",
        category: "settings",
        action: () => {
          // TODO: Open settings
          onClose();
        },
      },
      {
        id: "keyboard-shortcuts",
        label: "键盘快捷键",
        description: "查看所有快捷键",
        icon: <Command size={16} />,
        hotkey: "Cmd+/",
        category: "settings",
        action: () => {
          // TODO: Show shortcuts
          onClose();
        },
      },
    ],
    [todos, messages, addTodo, onClose]
  );

  const filteredCommands = useMemo(() => {
    if (!search) return commands;
    const lowerSearch = search.toLowerCase();
    return commands.filter(
      (cmd) =>
        cmd.label.toLowerCase().includes(lowerSearch) ||
        cmd.description.toLowerCase().includes(lowerSearch) ||
        cmd.category.toLowerCase().includes(lowerSearch)
    );
  }, [commands, search]);

  // Keyboard navigation
  useEffect(() => {
    if (!open) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIndex((i) => (i + 1) % filteredCommands.length);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIndex((i) => (i - 1 + filteredCommands.length) % filteredCommands.length);
      } else if (e.key === "Enter") {
        e.preventDefault();
        filteredCommands[selectedIndex]?.action();
      } else if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, selectedIndex, filteredCommands, onClose]);

  // Reset state when opening
  useEffect(() => {
    if (open) {
      setSearch("");
      setSelectedIndex(0);
    }
  }, [open]);

  if (!open) return null;

  return (
    <div className="command-palette-overlay" onClick={onClose}>
      <div className="command-palette" onClick={(e) => e.stopPropagation()}>
        {/* Search input */}
        <div className="command-palette-search">
          <Search size={18} className="command-palette-search-icon" />
          <input
            type="text"
            className="command-palette-input"
            placeholder="输入命令或搜索..."
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setSelectedIndex(0);
            }}
            autoFocus
          />
          <kbd className="command-palette-kbd">ESC</kbd>
        </div>

        {/* Command list */}
        <div className="command-palette-list">
          {filteredCommands.length === 0 ? (
            <div className="command-palette-empty">没有找到匹配的命令</div>
          ) : (
            filteredCommands.map((cmd, index) => (
              <button
                key={cmd.id}
                className={`command-palette-item ${index === selectedIndex ? "selected" : ""}`}
                onClick={cmd.action}
                onMouseEnter={() => setSelectedIndex(index)}
              >
                <div className="command-palette-item-icon">{cmd.icon}</div>
                <div className="command-palette-item-content">
                  <div className="command-palette-item-label">{cmd.label}</div>
                  <div className="command-palette-item-description">{cmd.description}</div>
                </div>
                {cmd.hotkey && <kbd className="command-palette-item-hotkey">{cmd.hotkey}</kbd>}
              </button>
            ))
          )}
        </div>

        {/* Footer */}
        <div className="command-palette-footer">
          <div className="command-palette-footer-hint">
            <kbd>↑</kbd> <kbd>↓</kbd> 导航 · <kbd>Enter</kbd> 选择 · <kbd>ESC</kbd> 关闭
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * Hook to manage command palette state
 */
export function useCommandPalette() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Cmd+K or Ctrl+K
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setOpen((prev) => !prev);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  return { open, setOpen };
}
