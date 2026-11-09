import { AnimatePresence, motion } from "motion/react";
import {
  ArrowUp,
  AtSign,
  Check,
  ChevronDown,
  FileText,
  FolderGit2,
  Hand,
  ImageIcon,
  ListChecks,
  Lock,
  Paperclip,
  Plus,
  ShieldAlert,
  Square,
  X,
  Zap,
} from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useApp } from "../store";
import { efforts, fileTree, models, permissionModes, type TreeNode } from "../mock";
import type { Attachment, Chat, PermissionMode } from "../types";
import { Menu, Popover, Segmented, useDisclosure } from "../ui/primitives";
import { cx, modKey, uid } from "../lib/util";

const EMPTY_DRAFT = { text: "", attachments: [] as Attachment[] };

interface SlashCommand {
  id: string;
  label: string;
  desc: string;
  icon: ReactNode;
}

const SLASH: SlashCommand[] = [
  { id: "plan", label: "/plan", desc: "切换计划模式：先出方案，确认后再动手", icon: <ListChecks size={15} /> },
  { id: "model", label: "/model", desc: "切换模型与推理强度", icon: <Zap size={15} /> },
  { id: "review", label: "/review", desc: "审阅当前工作区的未提交改动", icon: <FileText size={15} /> },
  { id: "compact", label: "/compact", desc: "压缩上下文，释放窗口空间", icon: <Lock size={15} /> },
  { id: "new", label: "/new", desc: "开始一个新聊天", icon: <Plus size={15} /> },
];

function flatFiles(nodes: TreeNode[]): string[] {
  return nodes.flatMap((n) => (n.dir ? flatFiles(n.children ?? []) : [n.path]));
}
const ALL_FILES = flatFiles(fileTree);

export function Composer({ draftKey, chat, autoFocus }: { draftKey: string; chat?: Chat; autoFocus?: boolean }) {
  const draft = useApp((s) => s.drafts[draftKey]) ?? EMPTY_DRAFT;
  const setDraft = useApp((s) => s.setDraft);
  const send = useApp((s) => s.send);
  const stop = useApp((s) => s.stop);
  const planMode = useApp((s) => s.planMode);
  const togglePlanMode = useApp((s) => s.togglePlanMode);
  const sendKey = useApp((s) => s.settings.sendKey);
  const newChat = useApp((s) => s.newChat);
  const toast = useApp((s) => s.toast);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const modelRef = useRef<HTMLButtonElement>(null);
  const [dragging, setDragging] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const [active, setActive] = useState(0);
  const composing = useRef(false);

  const last = chat?.messages[chat.messages.length - 1];
  const running = last?.role === "assistant" && last.status === "running";
  const text = draft.text;
  const canSend = text.trim().length > 0 || draft.attachments.length > 0;

  // 斜杠命令只在开头触发；@ 引用取光标前最后一个词
  const slashQuery = /^\/(\S*)$/.exec(text)?.[1];
  const mentionQuery = /(?:^|\s)@([^\s@]*)$/.exec(text)?.[1];
  const slashItems = useMemo(
    () => (slashQuery === undefined ? [] : SLASH.filter((c) => c.id.startsWith(slashQuery.toLowerCase()))),
    [slashQuery],
  );
  const mentionItems = useMemo(
    () =>
      mentionQuery === undefined
        ? []
        : ALL_FILES.filter((f) => f.toLowerCase().includes(mentionQuery.toLowerCase())).slice(0, 6),
    [mentionQuery],
  );
  const suggestOpen = slashItems.length > 0 || mentionItems.length > 0;

  useEffect(() => setActive(0), [slashQuery, mentionQuery]);

  useLayoutEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "0px";
    ta.style.height = Math.min(ta.scrollHeight, 260) + "px";
  }, [text]);

  useEffect(() => {
    if (autoFocus) taRef.current?.focus();
  }, [autoFocus, draftKey]);

  // 全局快捷键 / 聚焦输入框
  useEffect(() => {
    const onFocus = () => taRef.current?.focus();
    window.addEventListener("minicode:focus-composer", onFocus);
    return () => window.removeEventListener("minicode:focus-composer", onFocus);
  }, []);

  const setText = (t: string) => setDraft(draftKey, { text: t });

  const addFiles = (files: FileList | File[]) => {
    const list = Array.from(files).map<Attachment>((f) => ({
      id: uid("att"),
      name: f.name,
      kind: f.type.startsWith("image/") ? "image" : "file",
      size: f.size > 1024 * 1024 ? `${(f.size / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(f.size / 1024))} KB`,
    }));
    if (list.length) setDraft(draftKey, { attachments: [...draft.attachments, ...list] });
  };

  const runSlash = (cmd: SlashCommand) => {
    setText("");
    if (cmd.id === "plan") {
      togglePlanMode();
      toast({ text: planMode ? "已退出计划模式" : "已进入计划模式：先给方案，确认后再修改" });
    } else if (cmd.id === "model") {
      setModelOpen(true);
    } else if (cmd.id === "review") {
      send(draftKey, "审阅当前工作区的未提交改动，指出风险并给出修改建议");
    } else if (cmd.id === "compact") {
      toast({ text: "已压缩上下文：保留最近 3 轮与关键文件摘要", tone: "success" });
    } else if (cmd.id === "new") {
      newChat();
    }
  };

  const pickMention = (path: string) => {
    setText(text.replace(/@([^\s@]*)$/, `@${path} `));
    taRef.current?.focus();
  };

  const submit = () => {
    if (!canSend) return;
    send(draftKey, text);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (composing.current || e.nativeEvent.isComposing) return;
    if (suggestOpen) {
      const count = slashItems.length || mentionItems.length;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setActive((a) => (a + (e.key === "ArrowDown" ? 1 : -1) + count) % count);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        if (slashItems.length) runSlash(slashItems[active]);
        else pickMention(mentionItems[active]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setText(text.replace(/[/@][^\s]*$/, ""));
        return;
      }
    }
    if (e.key === "Enter") {
      const mod = e.metaKey || e.ctrlKey;
      if ((sendKey === "enter" && !e.shiftKey && !mod) || mod) {
        e.preventDefault();
        submit();
      }
      return;
    }
    if (e.key === "Escape" && running && chat) {
      e.preventDefault();
      stop(chat.id);
    }
  };

  return (
    <div
      ref={boxRef}
      className={cx("composer", dragging && "dragging", planMode && "plan")}
      onDragOver={(e) => {
        if (e.dataTransfer.types.includes("Files")) {
          e.preventDefault();
          setDragging(true);
        }
      }}
      onDragLeave={(e) => {
        if (!boxRef.current?.contains(e.relatedTarget as Node)) setDragging(false);
      }}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        addFiles(e.dataTransfer.files);
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) taRef.current?.focus();
      }}
    >
      <AnimatePresence>
        {suggestOpen && (
          <motion.div
            className="suggest"
            role="listbox"
            aria-label={slashItems.length ? "命令" : "引用文件"}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 4, transition: { duration: 0.1 } }}
            transition={{ duration: 0.16 }}
          >
            <div className="suggest-head">{slashItems.length ? "命令" : "引用文件"}</div>
            {slashItems.map((c, i) => (
              <button
                key={c.id}
                role="option"
                aria-selected={i === active}
                className={cx("suggest-item", i === active && "active")}
                onMouseEnter={() => setActive(i)}
                onMouseDown={(e) => {
                  e.preventDefault();
                  runSlash(c);
                }}
              >
                <span className="suggest-icon">{c.icon}</span>
                <span className="suggest-label mono">{c.label}</span>
                <span className="suggest-desc">{c.desc}</span>
              </button>
            ))}
            {mentionItems.map((f, i) => (
              <button
                key={f}
                role="option"
                aria-selected={i === active}
                className={cx("suggest-item", i === active && "active")}
                onMouseEnter={() => setActive(i)}
                onMouseDown={(e) => {
                  e.preventDefault();
                  pickMention(f);
                }}
              >
                <span className="suggest-icon"><FileText size={15} /></span>
                <span className="suggest-label">{f.split("/").pop()}</span>
                <span className="suggest-desc mono">{f}</span>
              </button>
            ))}
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence initial={false}>
        {draft.attachments.length > 0 && (
          <motion.div
            className="attachments"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
          >
            {draft.attachments.map((a) => (
              <div key={a.id} className="attachment">
                <span className={cx("attachment-thumb", a.kind)}>
                  {a.kind === "image" ? <ImageIcon size={15} /> : <FileText size={15} />}
                </span>
                <span className="attachment-text">
                  <span className="attachment-name">{a.name}</span>
                  <span className="attachment-size">{a.size}</span>
                </span>
                <button
                  className="attachment-remove"
                  aria-label={`移除 ${a.name}`}
                  onClick={() => setDraft(draftKey, { attachments: draft.attachments.filter((x) => x.id !== a.id) })}
                >
                  <X size={12} />
                </button>
              </div>
            ))}
          </motion.div>
        )}
      </AnimatePresence>

      <textarea
        ref={taRef}
        className="composer-input"
        rows={1}
        value={text}
        aria-label="消息"
        placeholder={running ? "继续输入，回复结束后自动发送…" : planMode ? "描述要做的事，我会先给出计划…" : "随心输入，/ 调用命令，@ 引用文件"}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        onCompositionStart={() => (composing.current = true)}
        onCompositionEnd={() => (composing.current = false)}
        onPaste={(e) => {
          if (e.clipboardData.files.length) {
            e.preventDefault();
            addFiles(e.clipboardData.files);
          }
        }}
      />

      <div className="composer-row">
        <AddMenu
          onPickFiles={() => fileRef.current?.click()}
          onMention={() => {
            setText(text + (text && !text.endsWith(" ") ? " @" : "@"));
            taRef.current?.focus();
          }}
        />
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          data-testid="file-input"
          onChange={(e) => {
            if (e.target.files) addFiles(e.target.files);
            e.target.value = "";
          }}
        />
        <PermissionPicker />
        <AnimatePresence>
          {planMode && (
            <motion.button
              className="chip plan-chip"
              initial={{ opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.9 }}
              onClick={() => togglePlanMode(false)}
              data-tip="点击退出计划模式"
            >
              <ListChecks size={14} />
              计划模式
              <X size={12} className="chip-x" />
            </motion.button>
          )}
        </AnimatePresence>
        <div className="grow" />
        {chat && <ContextRing chat={chat} />}
        <button ref={modelRef} className="model-btn" onClick={() => setModelOpen((o) => !o)} aria-haspopup="dialog" aria-expanded={modelOpen}>
          <ModelLabel />
          <ChevronDown size={13} className="muted" />
        </button>
        <Popover anchor={modelRef} open={modelOpen} onClose={() => setModelOpen(false)} placement="top-end" label="模型与推理强度">
          <ModelPicker onClose={() => setModelOpen(false)} />
        </Popover>
        {running && !canSend ? (
          <button className="send-btn stop" aria-label="停止" data-tip="停止  Esc" onClick={() => chat && stop(chat.id)}>
            <Square size={11} fill="currentColor" />
          </button>
        ) : (
          <button
            className={cx("send-btn", canSend && "ready")}
            aria-label={running ? "加入队列" : "发送"}
            data-tip={running ? "加入队列，当前回复结束后发送" : sendKey === "enter" ? "发送  Enter" : `发送  ${modKey} Enter`}
            disabled={!canSend}
            onClick={submit}
          >
            <ArrowUp size={16} strokeWidth={2.4} />
          </button>
        )}
      </div>

      {dragging && (
        <div className="drop-hint">
          <Paperclip size={18} />
          松开即可添加附件
        </div>
      )}
    </div>
  );
}

function AddMenu({ onPickFiles, onMention }: { onPickFiles: () => void; onMention: () => void }) {
  const ref = useRef<HTMLButtonElement>(null);
  const menu = useDisclosure();
  const planMode = useApp((s) => s.planMode);
  const togglePlanMode = useApp((s) => s.togglePlanMode);
  return (
    <>
      <button ref={ref} className="icon-btn round" aria-label="添加" data-tip="添加文件、引用或切换模式" onClick={menu.toggle}>
        <Plus size={17} />
      </button>
      <Popover anchor={ref} open={menu.open} onClose={menu.close} placement="top-start" label="添加">
        <Menu
          onClose={menu.close}
          items={[
            { id: "files", label: "添加文件或图片", icon: <Paperclip size={15} />, hint: "拖入 / 粘贴", onSelect: onPickFiles },
            { id: "mention", label: "引用项目文件", icon: <AtSign size={15} />, hint: "@", onSelect: onMention },
            "divider",
            {
              id: "plan",
              label: "计划模式",
              desc: "先出方案，确认后再修改",
              icon: <ListChecks size={15} />,
              checked: planMode,
              onSelect: () => togglePlanMode(),
            },
          ]}
        />
      </Popover>
    </>
  );
}

const PERMISSION_ICON: Record<PermissionMode, ReactNode> = {
  readonly: <Lock size={14} />,
  ask: <Hand size={14} />,
  full: <ShieldAlert size={14} />,
};

function PermissionPicker() {
  const ref = useRef<HTMLButtonElement>(null);
  const menu = useDisclosure();
  const permission = useApp((s) => s.permission);
  const setPermission = useApp((s) => s.setPermission);
  const current = permissionModes.find((p) => p.id === permission)!;
  return (
    <>
      <button
        ref={ref}
        className={cx("chip permission", permission)}
        onClick={menu.toggle}
        aria-haspopup="menu"
        aria-expanded={menu.open}
        aria-label={`权限：${current.label}`}
      >
        {PERMISSION_ICON[permission]}
        {current.label}
      </button>
      <Popover anchor={ref} open={menu.open} onClose={menu.close} placement="top-start" label="权限">
        <Menu
          onClose={menu.close}
          items={[
            { heading: "这次聊天中 MiniCode 可以…" },
            ...permissionModes.map((p) => ({
              id: p.id,
              label: p.label,
              desc: p.note,
              icon: PERMISSION_ICON[p.id],
              checked: p.id === permission,
              danger: p.id === "full",
              onSelect: () => setPermission(p.id),
            })),
          ]}
        />
      </Popover>
    </>
  );
}

function ModelLabel() {
  const model = useApp((s) => s.model);
  const effort = useApp((s) => s.effort);
  const m = models.find((x) => x.id === model)!;
  const e = efforts.find((x) => x.id === effort)!;
  return (
    <span className="model-label">
      {m.short}
      <span className="model-effort">{e.label}</span>
    </span>
  );
}

function ModelPicker({ onClose }: { onClose: () => void }) {
  const model = useApp((s) => s.model);
  const effort = useApp((s) => s.effort);
  const setModel = useApp((s) => s.setModel);
  const setEffort = useApp((s) => s.setEffort);
  const navigate = useApp((s) => s.navigate);
  return (
    <div className="model-picker">
      <div className="menu-heading">模型</div>
      {models.map((m) => (
        <button
          key={m.id}
          className={cx("menu-item", m.id === model && "selected")}
          role="menuitemradio"
          aria-checked={m.id === model}
          onClick={() => setModel(m.id)}
        >
          <span className="menu-text">
            <span className="menu-label">{m.name}</span>
            <span className="menu-desc">{m.note} · {m.context >= 1_000_000 ? "1M" : `${m.context / 1000}K`} 上下文</span>
          </span>
          <span className="menu-check">{m.id === model && <Check size={15} strokeWidth={2.2} />}</span>
        </button>
      ))}
      <div className="menu-divider" />
      <div className="menu-heading">推理强度</div>
      <div className="model-effort-row">
        <Segmented label="推理强度" value={effort} options={efforts.map((e) => ({ id: e.id, label: e.label }))} onChange={setEffort} />
        <div className="menu-desc effort-note">{efforts.find((e) => e.id === effort)!.note}</div>
      </div>
      <div className="menu-divider" />
      <button
        className="menu-item"
        onClick={() => {
          onClose();
          navigate({ name: "settings", section: "models" });
        }}
      >
        <span className="menu-text">
          <span className="menu-label">管理模型与 API 密钥…</span>
        </span>
      </button>
    </div>
  );
}

function ContextRing({ chat }: { chat: Chat }) {
  const model = useApp((s) => s.model);
  const ctx = models.find((m) => m.id === model)!.context;
  const chars = chat.messages.reduce(
    (n, m) => n + m.text.length + (m.role === "assistant" ? m.steps.length * 900 + (m.files?.length ?? 0) * 1600 : 0),
    0,
  );
  const used = 18_000 + chars * 3;
  const ratio = Math.min(1, used / ctx);
  const r = 7;
  const c = 2 * Math.PI * r;
  const fmt = (n: number) => (n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : `${Math.round(n / 1000)}K`);
  return (
    <span className="ctx-ring" data-tip={`上下文已用 ${Math.max(1, Math.round(ratio * 100))}% · ${fmt(used)} / ${fmt(ctx)}`} aria-label="上下文用量">
      <svg width="18" height="18" viewBox="0 0 18 18">
        <circle cx="9" cy="9" r={r} fill="none" stroke="var(--line-strong)" strokeWidth="2" />
        <circle
          cx="9"
          cy="9"
          r={r}
          fill="none"
          stroke={ratio > 0.8 ? "var(--amber)" : "var(--text-3)"}
          strokeWidth="2"
          strokeDasharray={`${Math.max(c * 0.06, ratio * c)} ${c}`}
          strokeLinecap="round"
          transform="rotate(-90 9 9)"
        />
      </svg>
    </span>
  );
}

/** 首页输入框上方的项目选择条 */
export function ProjectBar() {
  const ref = useRef<HTMLButtonElement>(null);
  const menu = useDisclosure();
  const projects = useApp((s) => s.projects);
  const homeProjectId = useApp((s) => s.homeProjectId);
  const setHomeProject = useApp((s) => s.setHomeProject);
  const project = projects.find((p) => p.id === homeProjectId);
  return (
    <>
      <button ref={ref} className="project-bar" onClick={menu.toggle} aria-haspopup="menu" aria-expanded={menu.open}>
        <FolderGit2 size={14} />
        <span>{project ? project.name : "不使用项目"}</span>
        {project && <span className="project-bar-branch">{project.branch}</span>}
        <ChevronDown size={13} className="muted" />
      </button>
      <Popover anchor={ref} open={menu.open} onClose={menu.close} label="选择项目">
        <Menu
          onClose={menu.close}
          items={[
            { heading: "在哪个项目中工作？" },
            ...projects.map((p) => ({
              id: p.id,
              label: p.name,
              desc: p.path,
              icon: <FolderGit2 size={15} />,
              checked: p.id === homeProjectId,
              onSelect: () => setHomeProject(p.id),
            })),
            "divider" as const,
            {
              id: "none",
              label: "不使用项目",
              desc: "纯聊天，不读写任何文件",
              checked: homeProjectId === null,
              onSelect: () => setHomeProject(null),
            },
          ]}
        />
      </Popover>
    </>
  );
}
