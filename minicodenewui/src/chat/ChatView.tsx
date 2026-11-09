import { AnimatePresence, motion } from "motion/react";
import { ArrowDown, Check, Copy, CornerDownLeft, FileText, ImageIcon, Pencil, Send, X } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useApp } from "../store";
import type { Chat, UserMessage } from "../types";
import { Composer, ProjectBar } from "../composer/Composer";
import { TurnView } from "./Turn";
import { Logo } from "../ui/brand";
import { suggestions } from "../mock";
import { copyText, cx } from "../lib/util";

export function ChatView({ chat }: { chat: Chat }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const last = chat.messages[chat.messages.length - 1];
  const lastAssistantIndex = chat.messages.map((m) => m.role).lastIndexOf("assistant");

  // 切换聊天时直接到底部
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    stick.current = true;
    setAtBottom(true);
  }, [chat.id]);

  // 内容增长时，如果用户本来就在底部，跟随到底部
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  });

  const onScroll = () => {
    const el = scrollRef.current!;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    stick.current = near;
    if (near !== atBottom) setAtBottom(near);
  };

  // 新发送的消息总是滚到底
  const count = chat.messages.length;
  useEffect(() => {
    stick.current = true;
  }, [count]);

  const toBottom = () => {
    const el = scrollRef.current;
    el?.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    stick.current = true;
  };

  return (
    <div className="chat-view">
      <div className="chat-scroll" ref={scrollRef} onScroll={onScroll} data-testid="chat-scroll">
        <div className="chat-column">
          {chat.messages.length === 0 && <div className="chat-empty">这个聊天还没有消息。</div>}
          {chat.messages.map((m, i) =>
            m.role === "user" ? (
              <UserBubble key={m.id} chatId={chat.id} message={m} />
            ) : (
              <TurnView key={m.id} chatId={chat.id} turn={m} isLast={i === lastAssistantIndex} />
            ),
          )}
          <div className="chat-spacer" />
        </div>
      </div>

      <div className="chat-dock">
        <AnimatePresence>
          {!atBottom && (
            <motion.button
              className="to-bottom"
              aria-label="滚动到底部"
              initial={{ opacity: 0, y: 6, scale: 0.9 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 6, scale: 0.9 }}
              onClick={toBottom}
            >
              <ArrowDown size={15} />
            </motion.button>
          )}
        </AnimatePresence>
        <div className="chat-column">
          <QueueList chat={chat} />
          <Composer draftKey={chat.id} chat={chat} autoFocus={last?.role !== "assistant" || last.status !== "running" || !last.approval} />
        </div>
      </div>
    </div>
  );
}

function UserBubble({ chatId, message }: { chatId: string; message: UserMessage }) {
  const editAndResend = useApp((s) => s.editAndResend);
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(message.text);
  const [copied, setCopied] = useState(false);

  if (editing) {
    return (
      <div className="user-edit">
        <textarea
          className="user-edit-input"
          autoFocus
          value={value}
          aria-label="编辑消息"
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              setEditing(false);
              editAndResend(chatId, message.id, value);
            }
            if (e.key === "Escape") setEditing(false);
          }}
        />
        <div className="user-edit-actions">
          <span className="card-hint">重新发送会丢弃这条消息之后的内容</span>
          <div className="grow" />
          <button className="btn sm ghost" onClick={() => setEditing(false)}>
            取消
          </button>
          <button
            className="btn sm primary"
            disabled={!value.trim()}
            onClick={() => {
              setEditing(false);
              editAndResend(chatId, message.id, value);
            }}
          >
            <Send size={13} /> 发送
          </button>
        </div>
      </div>
    );
  }

  return (
    <motion.div className="user" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.22 }}>
      {message.attachments && message.attachments.length > 0 && (
        <div className="user-attachments">
          {message.attachments.map((a) => (
            <span key={a.id} className="attachment small">
              <span className={cx("attachment-thumb", a.kind)}>{a.kind === "image" ? <ImageIcon size={13} /> : <FileText size={13} />}</span>
              <span className="attachment-name">{a.name}</span>
            </span>
          ))}
        </div>
      )}
      {message.text && <div className="user-bubble">{message.text}</div>}
      <div className="user-actions">
        <button
          className="icon-btn xs"
          aria-label="复制消息"
          data-tip={copied ? "已复制" : "复制"}
          onClick={async () => {
            await copyText(message.text);
            setCopied(true);
            setTimeout(() => setCopied(false), 1400);
          }}
        >
          {copied ? <Check size={13} /> : <Copy size={13} />}
        </button>
        <button
          className="icon-btn xs"
          aria-label="编辑消息"
          data-tip="编辑后重新发送"
          onClick={() => {
            setValue(message.text);
            setEditing(true);
          }}
        >
          <Pencil size={13} />
        </button>
      </div>
    </motion.div>
  );
}

function QueueList({ chat }: { chat: Chat }) {
  const removeQueued = useApp((s) => s.removeQueued);
  const sendQueuedNow = useApp((s) => s.sendQueuedNow);
  return (
    <AnimatePresence initial={false}>
      {chat.queue.length > 0 && (
        <motion.div
          className="queue"
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: "auto" }}
          exit={{ opacity: 0, height: 0 }}
          aria-label="待发送"
        >
          <div className="queue-head">回复结束后依次发送 · {chat.queue.length}</div>
          {chat.queue.map((q, i) => (
            <div key={`${i}-${q}`} className="queue-item">
              <CornerDownLeft size={13} className="muted" />
              <span className="queue-text">{q}</span>
              <button className="btn sm ghost" onClick={() => sendQueuedNow(chat.id, i)} data-tip="打断当前回复，立即发送">
                立即发送
              </button>
              <button className="icon-btn xs" aria-label="移除" onClick={() => removeQueued(chat.id, i)}>
                <X size={13} />
              </button>
            </div>
          ))}
        </motion.div>
      )}
    </AnimatePresence>
  );
}

export function HomeView() {
  const setDraft = useApp((s) => s.setDraft);
  const projects = useApp((s) => s.projects);
  const homeProjectId = useApp((s) => s.homeProjectId);
  const project = projects.find((p) => p.id === homeProjectId);
  const hour = new Date().getHours();
  const greeting = hour < 6 ? "夜深了" : hour < 12 ? "早上好" : hour < 18 ? "下午好" : "晚上好";
  return (
    <div className="home">
      <div className="home-inner">
        <motion.div
          className="home-hero"
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
        >
          <Logo size={40} />
          <h1 className="home-title">
            {greeting}，今天要构建什么？
          </h1>
          <p className="home-sub">{project ? `在 ${project.name} 中工作 · ${project.branch}` : "纯聊天模式，不会读写文件"}</p>
        </motion.div>
        <motion.div
          className="home-composer"
          initial={{ opacity: 0, y: 14 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.45, delay: 0.05, ease: [0.16, 1, 0.3, 1] }}
        >
          <ProjectBar />
          <Composer draftKey="home" autoFocus />
        </motion.div>
        <motion.div
          className="home-suggestions"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.4, delay: 0.15 }}
        >
          {suggestions.map((s) => (
            <button
              key={s.title}
              className="suggestion"
              onClick={() => {
                setDraft("home", { text: s.prompt });
                window.dispatchEvent(new Event("minicode:focus-composer"));
              }}
            >
              {s.title}
            </button>
          ))}
        </motion.div>
      </div>
    </div>
  );
}
