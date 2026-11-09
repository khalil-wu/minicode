import { AnimatePresence, motion } from "motion/react";
import {
  CheckCircle2,
  Keyboard,
  ListChecks,
  MessageSquare,
  Moon,
  PanelLeft,
  PanelRight,
  Puzzle,
  Search,
  Settings,
  SquarePen,
  Clock3,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useApp } from "../store";
import { Dialog } from "../ui/primitives";
import { SHORTCUTS } from "../pages/Settings";
import { cx, modKey, relativeTime } from "../lib/util";

interface PaletteItem {
  id: string;
  group: string;
  label: string;
  sub?: string;
  icon: ReactNode;
  hint?: string;
  run: () => void;
}

export function CommandPalette() {
  const open = useApp((s) => s.paletteOpen);
  const setPalette = useApp((s) => s.setPalette);
  const close = () => setPalette(false);
  return (
    <Dialog open={open} onClose={close} width={600} className="palette">
      <PaletteBody onClose={close} />
    </Dialog>
  );
}

function PaletteBody({ onClose }: { onClose: () => void }) {
  const chats = useApp((s) => s.chats);
  const projects = useApp((s) => s.projects);
  const st = useApp.getState;
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);

  const items = useMemo<PaletteItem[]>(() => {
    const actions: PaletteItem[] = [
      { id: "new", group: "操作", label: "新聊天", icon: <SquarePen size={15} />, hint: `${modKey} N`, run: () => st().newChat() },
      { id: "sidebar", group: "操作", label: "显示/隐藏侧边栏", icon: <PanelLeft size={15} />, hint: `${modKey} B`, run: () => st().toggleSidebar() },
      { id: "panel", group: "操作", label: "显示/隐藏审阅面板", icon: <PanelRight size={15} />, hint: `${modKey} J`, run: () => st().togglePanel() },
      {
        id: "theme",
        group: "操作",
        label: "切换深色 / 浅色",
        icon: <Moon size={15} />,
        run: () => st().updateSettings({ theme: document.documentElement.dataset.theme === "dark" ? "light" : "dark" }),
      },
      { id: "plan", group: "操作", label: "切换计划模式", icon: <ListChecks size={15} />, run: () => st().togglePlanMode() },
      { id: "settings", group: "操作", label: "打开设置", icon: <Settings size={15} />, hint: `${modKey} ,`, run: () => st().navigate({ name: "settings", section: "general" }) },
      { id: "plugins", group: "操作", label: "插件", icon: <Puzzle size={15} />, run: () => st().navigate({ name: "plugins" }) },
      { id: "auto", group: "操作", label: "自动化", icon: <Clock3 size={15} />, run: () => st().navigate({ name: "automations" }) },
      { id: "keys", group: "操作", label: "键盘快捷键", icon: <Keyboard size={15} />, hint: `${modKey} /`, run: () => st().setShortcuts(true) },
    ];
    const q = query.trim().toLowerCase();
    const chatItems: PaletteItem[] = chats
      .filter((c) => !c.archived)
      .map((c) => {
        const hit = q ? c.messages.find((m) => m.text.toLowerCase().includes(q)) : undefined;
        const snippet = hit ? excerpt(hit.text, q) : undefined;
        return {
          id: c.id,
          group: "聊天",
          label: c.title,
          sub: snippet ?? `${projects.find((p) => p.id === c.projectId)?.name ?? "无项目"} · ${relativeTime(c.updatedAt)}`,
          icon: <MessageSquare size={15} />,
          run: () => st().openChat(c.id),
          _match: !q || c.title.toLowerCase().includes(q) || !!hit,
        } as PaletteItem & { _match: boolean };
      })
      .filter((c) => (c as PaletteItem & { _match: boolean })._match);
    const actionItems = actions.filter((a) => !q || a.label.toLowerCase().includes(q));
    return q ? [...chatItems, ...actionItems] : [...actionItems.slice(0, 5), ...chatItems.slice(0, 6)];
  }, [query, chats, projects, st]);

  useEffect(() => setActive(0), [query]);
  useEffect(() => {
    listRef.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const run = (item: PaletteItem | undefined) => {
    if (!item) return;
    onClose();
    item.run();
  };

  let lastGroup = "";
  return (
    <div
      onKeyDown={(e) => {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setActive((a) => Math.min(items.length - 1, a + 1));
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          setActive((a) => Math.max(0, a - 1));
        } else if (e.key === "Enter") {
          e.preventDefault();
          run(items[active]);
        }
      }}
    >
      <label className="palette-search">
        <Search size={17} className="muted" />
        <input autoFocus placeholder="搜索聊天、消息或命令…" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="搜索" />
        <kbd className="kbd">Esc</kbd>
      </label>
      <div className="palette-list" ref={listRef} role="listbox">
        {items.length === 0 && <div className="palette-empty">没有找到「{query}」</div>}
        {items.map((item, i) => {
          const heading = item.group !== lastGroup ? <div className="palette-group">{item.group}</div> : null;
          lastGroup = item.group;
          return (
            <div key={item.id}>
              {heading}
              <button
                data-index={i}
                role="option"
                aria-selected={i === active}
                className={cx("palette-item", i === active && "active")}
                onMouseMove={() => setActive(i)}
                onClick={() => run(item)}
              >
                <span className="palette-icon">{item.icon}</span>
                <span className="palette-text">
                  <span className="palette-label">{item.label}</span>
                  {item.sub && <span className="palette-sub">{item.sub}</span>}
                </span>
                {item.hint && <span className="menu-hint">{item.hint}</span>}
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function excerpt(text: string, q: string): string {
  const i = text.toLowerCase().indexOf(q);
  const start = Math.max(0, i - 12);
  return (start > 0 ? "…" : "") + text.slice(start, i + q.length + 24).replace(/\s+/g, " ");
}

export function ShortcutsDialog() {
  const open = useApp((s) => s.shortcutsOpen);
  const setShortcuts = useApp((s) => s.setShortcuts);
  return (
    <Dialog open={open} onClose={() => setShortcuts(false)} title="键盘快捷键" width={520}>
      <div className="shortcut-grid">
        {SHORTCUTS.map((g) => (
          <div key={g.group}>
            <div className="menu-heading">{g.group}</div>
            {g.items.map(([label, keys]) => (
              <div key={label} className="shortcut-row">
                <span>{label}</span>
                <span className="keys">
                  {keys.split(" ").map((k) => (
                    <kbd key={k} className="kbd">
                      {k}
                    </kbd>
                  ))}
                </span>
              </div>
            ))}
          </div>
        ))}
      </div>
    </Dialog>
  );
}

export function ConfirmDialog() {
  const confirm = useApp((s) => s.confirm);
  const askConfirm = useApp((s) => s.askConfirm);
  const close = () => askConfirm(null);
  return (
    <Dialog
      open={!!confirm}
      onClose={close}
      width={400}
      className="confirm"
      footer={
        confirm && (
          <>
            <button className="btn" onClick={close}>
              取消
            </button>
            <button
              className={cx("btn", confirm.danger ? "danger" : "primary")}
              autoFocus
              onClick={() => {
                confirm.onConfirm();
                close();
              }}
            >
              {confirm.confirmLabel}
            </button>
          </>
        )
      }
    >
      {confirm && (
        <>
          <div className="confirm-title">{confirm.title}</div>
          <p className="dialog-text">{confirm.body}</p>
        </>
      )}
    </Dialog>
  );
}

/** 全局提示层：任何带 data-tip 的元素悬停 450ms 后显示，不会被滚动容器裁切 */
export function TooltipLayer() {
  const [tip, setTip] = useState<{ text: string; x: number; y: number; below: boolean } | null>(null);
  useEffect(() => {
    let timer = 0;
    let current: HTMLElement | null = null;
    const hide = () => {
      clearTimeout(timer);
      current = null;
      setTip(null);
    };
    const over = (e: PointerEvent) => {
      const el = (e.target as HTMLElement).closest<HTMLElement>("[data-tip]");
      if (el === current) return;
      hide();
      if (!el || e.pointerType === "touch") return;
      current = el;
      timer = window.setTimeout(() => {
        if (!current?.isConnected) return;
        const r = current.getBoundingClientRect();
        const below = r.top < 60;
        setTip({ text: current.dataset.tip ?? "", x: r.left + r.width / 2, y: below ? r.bottom + 6 : r.top - 6, below });
      }, 450);
    };
    window.addEventListener("pointerover", over);
    window.addEventListener("pointerdown", hide, true);
    window.addEventListener("scroll", hide, true);
    window.addEventListener("keydown", hide, true);
    return () => {
      clearTimeout(timer);
      window.removeEventListener("pointerover", over);
      window.removeEventListener("pointerdown", hide, true);
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("keydown", hide, true);
    };
  }, []);
  return createPortal(
    <AnimatePresence>
      {tip && tip.text && (
        <motion.div
          key={tip.text + tip.x}
          className={cx("tooltip", tip.below && "below")}
          // 定位用的平移交给 motion，避免和缩放动画抢 transform
          style={{ left: tip.x, top: tip.y, x: "-50%", y: tip.below ? 0 : "-100%" }}
          initial={{ opacity: 0, scale: 0.96 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0, transition: { duration: 0.08 } }}
          transition={{ duration: 0.12 }}
          role="tooltip"
        >
          {tip.text.split("  ").map((part, i) => (i === 0 ? <span key={i}>{part}</span> : <kbd key={i} className="tooltip-kbd">{part}</kbd>))}
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}

export function Toasts() {
  const toasts = useApp((s) => s.toasts);
  const dismiss = useApp((s) => s.dismissToast);
  return createPortal(
    <div className="toasts" aria-live="polite">
      <AnimatePresence initial={false}>
        {toasts.map((t) => (
          <motion.div
            key={t.id}
            layout
            className={cx("toast", t.tone)}
            initial={{ opacity: 0, x: 24, scale: 0.96 }}
            animate={{ opacity: 1, x: 0, scale: 1 }}
            exit={{ opacity: 0, scale: 0.96, transition: { duration: 0.14 } }}
            transition={{ type: "spring", stiffness: 500, damping: 36 }}
            role="status"
          >
            {t.tone === "success" && <CheckCircle2 size={15} className="toast-icon" />}
            <span className="toast-text">{t.text}</span>
            {t.action && (
              <button
                className="toast-action"
                onClick={() => {
                  t.action!.run();
                  dismiss(t.id);
                }}
              >
                {t.action.label}
              </button>
            )}
            <button className="toast-close" aria-label="关闭提示" onClick={() => dismiss(t.id)}>
              <X size={13} />
            </button>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>,
    document.body,
  );
}
