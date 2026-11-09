import { FolderGit2, GitBranch, MoreHorizontal, PanelLeftOpen, PanelRight, SquarePen } from "lucide-react";
import { useRef } from "react";
import { selectChat, useApp } from "../store";
import { Popover, useDisclosure } from "../ui/primitives";
import { ChatMenu } from "./Sidebar";
import { cx, modKey } from "../lib/util";

export function Topbar() {
  const sidebarOpen = useApp((s) => s.sidebarOpen);
  const toggleSidebar = useApp((s) => s.toggleSidebar);
  const newChat = useApp((s) => s.newChat);
  const chat = useApp(selectChat);
  const projects = useApp((s) => s.projects);
  const panelOpen = useApp((s) => s.panelOpen);
  const togglePanel = useApp((s) => s.togglePanel);
  const openPanel = useApp((s) => s.openPanel);
  const renaming = useApp((s) => !!chat && s.renaming?.id === chat.id && s.renaming.where === "title");
  const setRenaming = useApp((s) => s.setRenaming);
  const renameChat = useApp((s) => s.renameChat);
  const moreRef = useRef<HTMLButtonElement>(null);
  const more = useDisclosure();

  const project = chat ? projects.find((p) => p.id === chat.projectId) : undefined;
  const totals = (chat?.messages ?? []).reduce(
    (acc, m) => {
      if (m.role !== "assistant") return acc;
      for (const f of m.files ?? []) {
        if (f.reverted) continue;
        acc.add += f.add;
        acc.del += f.del;
        acc.files += 1;
      }
      return acc;
    },
    { add: 0, del: 0, files: 0 },
  );

  return (
    <header className="topbar">
      {!sidebarOpen && (
        <div className="topbar-lead">
          <button className="icon-btn" data-tip={`展开侧边栏  ${modKey} B`} aria-label="展开侧边栏" onClick={toggleSidebar}>
            <PanelLeftOpen size={16} />
          </button>
          <button className="icon-btn" data-tip={`新聊天  ${modKey} N`} aria-label="新聊天" onClick={() => newChat()}>
            <SquarePen size={16} />
          </button>
        </div>
      )}

      {chat && (
        <div className="topbar-title">
          {renaming ? (
            <input
              className="title-input"
              autoFocus
              defaultValue={chat.title}
              aria-label="重命名聊天"
              onFocus={(e) => e.currentTarget.select()}
              onBlur={(e) => renameChat(chat.id, e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") renameChat(chat.id, e.currentTarget.value);
                if (e.key === "Escape") setRenaming(null);
              }}
            />
          ) : (
            <h1 className="title" onDoubleClick={() => setRenaming(chat.id, "title")} title="双击重命名">
              {chat.title}
            </h1>
          )}
          {project && (
            <span className="crumb" title={project.path}>
              <FolderGit2 size={13} />
              {project.name}
              <span className="crumb-sep" />
              <GitBranch size={13} />
              {project.branch}
            </span>
          )}
        </div>
      )}

      <div className="topbar-actions">
        {totals.files > 0 && (
          <button className="diffstat-btn" onClick={() => openPanel("changes")} data-tip="查看本次聊天的全部变更">
            <span className="add">+{totals.add}</span>
            <span className="del">−{totals.del}</span>
          </button>
        )}
        {chat && (
          <>
            <button ref={moreRef} className="icon-btn" aria-label="聊天操作" onClick={more.toggle}>
              <MoreHorizontal size={16} />
            </button>
            <Popover anchor={moreRef} open={more.open} onClose={more.close} placement="bottom-end" label="聊天操作">
              <ChatMenu chat={chat} onClose={more.close} where="title" />
            </Popover>
          </>
        )}
        <button
          className={cx("icon-btn", panelOpen && "on")}
          data-tip={`${panelOpen ? "关闭" : "打开"}审阅面板  ${modKey} J`}
          aria-label="切换审阅面板"
          aria-pressed={panelOpen}
          onClick={() => togglePanel()}
        >
          <PanelRight size={16} />
        </button>
      </div>
    </header>
  );
}
