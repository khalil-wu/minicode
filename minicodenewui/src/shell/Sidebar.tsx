import { AnimatePresence, motion } from "motion/react";
import {
  Archive,
  ChevronDown,
  ChevronRight,
  Clock3,
  FolderClosed,
  FolderOpen,
  FolderPlus,
  Keyboard,
  MessageSquarePlus,
  Moon,
  MoreHorizontal,
  PanelLeftClose,
  Pencil,
  Pin,
  PinOff,
  Puzzle,
  Search,
  Settings,
  SquarePen,
  Sun,
  Trash2,
  CircleDot,
} from "lucide-react";
import { useRef, useState, type MouseEvent } from "react";
import { useApp } from "../store";
import type { Chat, Project } from "../types";
import { Menu, type MenuEntry, Popover, Spinner, useDisclosure, Dialog } from "../ui/primitives";
import { Logo } from "../ui/brand";
import { cx, modKey, relativeTime } from "../lib/util";

export function Sidebar() {
  const projects = useApp((s) => s.projects);
  const chats = useApp((s) => s.chats);
  const route = useApp((s) => s.route);
  const newChat = useApp((s) => s.newChat);
  const navigate = useApp((s) => s.navigate);
  const toggleSidebar = useApp((s) => s.toggleSidebar);
  const setPalette = useApp((s) => s.setPalette);
  const toggleProject = useApp((s) => s.toggleProject);
  const [addOpen, setAddOpen] = useState(false);

  const visible = chats.filter((c) => !c.archived);
  const pinned = visible.filter((c) => c.pinned).sort((a, b) => b.updatedAt - a.updatedAt);
  const byProject = (p: Project) =>
    visible.filter((c) => c.projectId === p.id && !c.pinned).sort((a, b) => b.updatedAt - a.updatedAt);
  const loose = visible.filter((c) => !c.projectId && !c.pinned).sort((a, b) => b.updatedAt - a.updatedAt);

  return (
    <aside className="sidebar" aria-label="侧边栏">
      <div className="sb-top">
        <BrandMenu />
        <div className="sb-top-actions">
          <button className="icon-btn" data-tip={`搜索  ${modKey} K`} aria-label="搜索" onClick={() => setPalette(true)}>
            <Search size={16} />
          </button>
          <button className="icon-btn" data-tip={`收起侧边栏  ${modKey} B`} aria-label="收起侧边栏" onClick={toggleSidebar}>
            <PanelLeftClose size={16} />
          </button>
        </div>
      </div>

      <nav className="sb-nav">
        <button className={cx("sb-row", route.name === "home" && "active")} onClick={() => newChat()}>
          <SquarePen size={16} className="sb-row-icon" />
          <span className="sb-row-label">新聊天</span>
          <span className="sb-row-hint">{modKey} N</span>
        </button>
        <button className={cx("sb-row", route.name === "automations" && "active")} onClick={() => navigate({ name: "automations" })}>
          <Clock3 size={16} className="sb-row-icon" />
          <span className="sb-row-label">自动化</span>
        </button>
        <button className={cx("sb-row", route.name === "plugins" && "active")} onClick={() => navigate({ name: "plugins" })}>
          <Puzzle size={16} className="sb-row-icon" />
          <span className="sb-row-label">插件</span>
        </button>
      </nav>

      <div className="sb-scroll">
        {pinned.length > 0 && (
          <section className="sb-section">
            <div className="sb-heading">置顶</div>
            {pinned.map((c) => (
              <ChatRow key={c.id} chat={c} />
            ))}
          </section>
        )}

        <section className="sb-section">
          <div className="sb-heading">
            <span>项目</span>
            <button className="icon-btn xs" data-tip="添加项目" aria-label="添加项目" onClick={() => setAddOpen(true)}>
              <FolderPlus size={14} />
            </button>
          </div>
          {projects.map((p) => {
            const list = byProject(p);
            return (
              <div key={p.id} className="sb-project">
                <div className="sb-row project" onClick={() => toggleProject(p.id)} role="button" aria-expanded={p.expanded}>
                  <span className="sb-row-icon folder">
                    {p.expanded ? <FolderOpen size={16} /> : <FolderClosed size={16} />}
                  </span>
                  <span className="sb-row-label">{p.name}</span>
                  <span className="sb-row-trailing">
                    <button
                      className="icon-btn xs hover-only"
                      data-tip="在此项目中新建聊天"
                      aria-label={`在 ${p.name} 中新建聊天`}
                      onClick={(e) => {
                        e.stopPropagation();
                        newChat(p.id);
                      }}
                    >
                      <MessageSquarePlus size={14} />
                    </button>
                    <ChevronRight size={14} className={cx("chev", p.expanded && "open")} />
                  </span>
                </div>
                <AnimatePresence initial={false}>
                  {p.expanded && (
                    <motion.div
                      className="sb-children"
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: "auto", opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }}
                      transition={{ duration: 0.22, ease: [0.2, 0.8, 0.2, 1] }}
                    >
                      {list.length === 0 ? (
                        <div className="sb-empty">还没有聊天</div>
                      ) : (
                        list.map((c) => <ChatRow key={c.id} chat={c} nested />)
                      )}
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
            );
          })}
        </section>

        <section className="sb-section">
          <div className="sb-heading">聊天</div>
          {loose.length === 0 ? <div className="sb-empty">没有项目外的聊天</div> : loose.map((c) => <ChatRow key={c.id} chat={c} />)}
        </section>
      </div>

      <SidebarFooter />
      <AddProjectDialog open={addOpen} onClose={() => setAddOpen(false)} />
    </aside>
  );
}

function BrandMenu() {
  const ref = useRef<HTMLButtonElement>(null);
  const menu = useDisclosure();
  const navigate = useApp((s) => s.navigate);
  const setShortcuts = useApp((s) => s.setShortcuts);
  const toast = useApp((s) => s.toast);
  return (
    <>
      <button ref={ref} className="sb-brand" onClick={menu.toggle} aria-haspopup="menu" aria-expanded={menu.open}>
        <Logo size={20} />
        <span>MiniCode</span>
        <ChevronDown size={14} className="muted" />
      </button>
      <Popover anchor={ref} open={menu.open} onClose={menu.close} label="应用菜单">
        <Menu
          onClose={menu.close}
          items={[
            { id: "about", label: "关于 MiniCode", onSelect: () => navigate({ name: "settings", section: "about" }) },
            { id: "update", label: "检查更新…", onSelect: () => toast({ text: "已是最新版本 0.3.0", tone: "success" }) },
            "divider",
            { id: "keys", label: "键盘快捷键", hint: `${modKey} /`, onSelect: () => setShortcuts(true) },
            { id: "settings", label: "设置", hint: `${modKey} ,`, onSelect: () => navigate({ name: "settings", section: "general" }) },
          ]}
        />
      </Popover>
    </>
  );
}

function ChatRow({ chat, nested }: { chat: Chat; nested?: boolean }) {
  const route = useApp((s) => s.route);
  const openChat = useApp((s) => s.openChat);
  const renaming = useApp((s) => s.renaming?.id === chat.id && s.renaming.where === "sidebar");
  const renameChat = useApp((s) => s.renameChat);
  const setRenaming = useApp((s) => s.setRenaming);
  const archiveChat = useApp((s) => s.archiveChat);
  const moreRef = useRef<HTMLButtonElement>(null);
  const menu = useDisclosure();
  const active = route.name === "chat" && route.id === chat.id;
  const last = chat.messages[chat.messages.length - 1];
  const running = last?.role === "assistant" && last.status === "running";
  const waiting = running && last.role === "assistant" && last.phase === "waiting";

  const onContext = (e: MouseEvent) => {
    e.preventDefault();
    menu.setOpen(true);
  };

  return (
    <div
      className={cx("sb-row chat", nested && "nested", active && "active", menu.open && "menu-open")}
      onClick={() => !renaming && openChat(chat.id)}
      onDoubleClick={() => setRenaming(chat.id)}
      onContextMenu={onContext}
      role="button"
      aria-current={active ? "page" : undefined}
      data-chat-id={chat.id}
    >
      {renaming ? (
        <input
          className="sb-rename"
          autoFocus
          defaultValue={chat.title}
          aria-label="重命名聊天"
          onClick={(e) => e.stopPropagation()}
          onFocus={(e) => e.currentTarget.select()}
          onBlur={(e) => renameChat(chat.id, e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") renameChat(chat.id, e.currentTarget.value);
            if (e.key === "Escape") setRenaming(null);
          }}
        />
      ) : (
        <span className="sb-row-label">{chat.title}</span>
      )}
      <span className="sb-row-trailing">
        {!renaming && (
          <span className="sb-row-status">
            {waiting ? (
              <span className="status-dot amber" data-tip="等待你的确认" aria-label="等待确认" />
            ) : running ? (
              <Spinner size={13} />
            ) : chat.unread ? (
              <span className="status-dot" aria-label="未读" />
            ) : (
              <span className="sb-time">{relativeTime(chat.updatedAt)}</span>
            )}
          </span>
        )}
        <span className="sb-row-actions">
          <button
            className="icon-btn xs"
            data-tip="归档"
            aria-label="归档"
            onClick={(e) => {
              e.stopPropagation();
              archiveChat(chat.id);
            }}
          >
            <Archive size={14} />
          </button>
          <button
            ref={moreRef}
            className="icon-btn xs"
            aria-label="更多操作"
            onClick={(e) => {
              e.stopPropagation();
              menu.toggle();
            }}
          >
            <MoreHorizontal size={14} />
          </button>
        </span>
      </span>
      <Popover anchor={moreRef} open={menu.open} onClose={menu.close} placement="bottom-end" label="聊天操作">
        <ChatMenu chat={chat} onClose={menu.close} />
      </Popover>
    </div>
  );
}

export function ChatMenu({ chat, onClose, where = "sidebar" }: { chat: Chat; onClose: () => void; where?: "sidebar" | "title" }) {
  const setRenaming = useApp((s) => s.setRenaming);
  const togglePin = useApp((s) => s.togglePin);
  const toggleUnread = useApp((s) => s.toggleUnread);
  const archiveChat = useApp((s) => s.archiveChat);
  const deleteChat = useApp((s) => s.deleteChat);
  const askConfirm = useApp((s) => s.askConfirm);
  const items: MenuEntry[] = [
    { id: "rename", label: "重命名", icon: <Pencil size={15} />, onSelect: () => setRenaming(chat.id, where) },
    {
      id: "pin",
      label: chat.pinned ? "取消置顶" : "置顶",
      icon: chat.pinned ? <PinOff size={15} /> : <Pin size={15} />,
      onSelect: () => togglePin(chat.id),
    },
    { id: "unread", label: chat.unread ? "标为已读" : "标为未读", icon: <CircleDot size={15} />, onSelect: () => toggleUnread(chat.id) },
    "divider",
    { id: "archive", label: "归档", icon: <Archive size={15} />, onSelect: () => archiveChat(chat.id) },
    {
      id: "delete",
      label: "删除…",
      icon: <Trash2 size={15} />,
      danger: true,
      onSelect: () =>
        askConfirm({
          title: "删除这个聊天？",
          body: `「${chat.title}」及其中的所有消息都会被删除。删除后 5 秒内可以撤销。`,
          confirmLabel: "删除",
          danger: true,
          onConfirm: () => deleteChat(chat.id),
        }),
    },
  ];
  return <Menu items={items} onClose={onClose} />;
}

function SidebarFooter() {
  const ref = useRef<HTMLButtonElement>(null);
  const menu = useDisclosure();
  const navigate = useApp((s) => s.navigate);
  const theme = useApp((s) => s.settings.theme);
  const updateSettings = useApp((s) => s.updateSettings);
  const setShortcuts = useApp((s) => s.setShortcuts);
  const dark = theme === "dark" || (theme === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  return (
    <div className="sb-foot">
      <button ref={ref} className="sb-account" onClick={menu.toggle} aria-haspopup="menu" aria-expanded={menu.open}>
        <span className="avatar">吴</span>
        <span className="sb-account-text">
          <span className="sb-account-name">本地用户</span>
          <span className="sb-account-sub">Claude Opus 5 · API</span>
        </span>
        <Settings size={16} className="muted" />
      </button>
      <Popover anchor={ref} open={menu.open} onClose={menu.close} placement="top-start" label="账户菜单">
        <Menu
          onClose={menu.close}
          items={[
            { id: "settings", label: "设置", icon: <Settings size={15} />, hint: `${modKey} ,`, onSelect: () => navigate({ name: "settings", section: "general" }) },
            {
              id: "theme",
              label: dark ? "切换到浅色" : "切换到深色",
              icon: dark ? <Sun size={15} /> : <Moon size={15} />,
              onSelect: () => updateSettings({ theme: dark ? "light" : "dark" }),
            },
            { id: "keys", label: "键盘快捷键", icon: <Keyboard size={15} />, hint: `${modKey} /`, onSelect: () => setShortcuts(true) },
            "divider",
            { id: "archived", label: "已归档的聊天", icon: <Archive size={15} />, onSelect: () => navigate({ name: "settings", section: "archived" }) },
          ]}
        />
      </Popover>
    </div>
  );
}

function AddProjectDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const addProject = useApp((s) => s.addProject);
  const [name, setName] = useState("");
  const submit = () => {
    if (!name.trim()) return;
    addProject(name.trim());
    setName("");
    onClose();
  };
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="添加项目"
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={!name.trim()} onClick={submit}>添加</button>
        </>
      }
    >
      <p className="dialog-text">选择一个本地文件夹作为项目。MiniCode 只会在你允许的范围内读取和修改其中的文件。</p>
      <label className="field">
        <span className="field-label">项目名称</span>
        <input
          className="input"
          autoFocus
          placeholder="例如 my-app"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
        />
      </label>
      <div className="field-hint">演示中不会真正访问磁盘，路径会自动生成。</div>
    </Dialog>
  );
}
