import { AnimatePresence, motion } from "motion/react";
import {
  Archive,
  ArrowLeft,
  Bot,
  Check,
  Eye,
  EyeOff,
  GitBranch,
  Info,
  Keyboard,
  Palette,
  Plug,
  Plus,
  RotateCcw,
  Search,
  Settings2,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { useApp } from "../store";
import type { Accent, SettingsSection } from "../types";
import { models, permissionModes } from "../mock";
import { Dialog, Segmented, Spinner, Switch } from "../ui/primitives";
import { Logo } from "../ui/brand";
import { cx, modKey, relativeTime } from "../lib/util";

const SECTIONS: { id: SettingsSection; label: string; icon: ReactNode; group: string; keywords: string }[] = [
  { id: "general", label: "常规", icon: <Settings2 size={15} />, group: "个人", keywords: "权限 发送 通知 语言 步骤" },
  { id: "appearance", label: "外观", icon: <Palette size={15} />, group: "个人", keywords: "主题 深色 浅色 颜色 字体 字号 密度 动效" },
  { id: "personalization", label: "个性化", icon: <Sparkles size={15} />, group: "个人", keywords: "指令 记忆 偏好" },
  { id: "shortcuts", label: "键盘快捷键", icon: <Keyboard size={15} />, group: "个人", keywords: "快捷键 按键" },
  { id: "models", label: "模型与密钥", icon: <Bot size={15} />, group: "集成", keywords: "模型 api 密钥 key anthropic deepseek" },
  { id: "mcp", label: "MCP 服务器", icon: <Plug size={15} />, group: "集成", keywords: "mcp 工具 服务器" },
  { id: "git", label: "Git", icon: <GitBranch size={15} />, group: "编码", keywords: "提交 分支 git" },
  { id: "archived", label: "已归档的聊天", icon: <Archive size={15} />, group: "数据", keywords: "归档 恢复 删除" },
  { id: "about", label: "关于", icon: <Info size={15} />, group: "数据", keywords: "版本 更新" },
];

export function SettingsNav({ section }: { section: SettingsSection }) {
  const navigate = useApp((s) => s.navigate);
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const list = SECTIONS.filter((s) => !q || s.label.toLowerCase().includes(q) || s.keywords.includes(q));
  const groups = [...new Set(list.map((s) => s.group))];
  return (
    <aside className="sidebar settings-nav" aria-label="设置导航">
      <div className="sb-top">
        <button className="sb-back" onClick={() => navigate({ name: "home" })}>
          <ArrowLeft size={15} />
          返回应用
        </button>
      </div>
      <div className="settings-title">设置</div>
      <label className="search-field sb-search">
        <Search size={14} className="muted" />
        <input placeholder="搜索设置" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="搜索设置" />
      </label>
      <div className="sb-scroll">
        {groups.map((g) => (
          <section key={g} className="sb-section">
            <div className="sb-heading">{g}</div>
            {list
              .filter((s) => s.group === g)
              .map((s) => (
                <button
                  key={s.id}
                  className={cx("sb-row", section === s.id && "active")}
                  onClick={() => navigate({ name: "settings", section: s.id })}
                >
                  <span className="sb-row-icon">{s.icon}</span>
                  <span className="sb-row-label">{s.label}</span>
                </button>
              ))}
          </section>
        ))}
        {list.length === 0 && <div className="sb-empty">没有匹配的设置</div>}
      </div>
    </aside>
  );
}

export function SettingsPage({ section }: { section: SettingsSection }) {
  const meta = SECTIONS.find((s) => s.id === section)!;
  return (
    <div className="settings-page">
      <AnimatePresence mode="wait">
        <motion.div
          key={section}
          className="settings-inner"
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, transition: { duration: 0.08 } }}
          transition={{ duration: 0.2 }}
        >
          <h1 className="settings-h1">{meta.label}</h1>
          {section === "general" && <General />}
          {section === "appearance" && <Appearance />}
          {section === "personalization" && <Personalization />}
          {section === "shortcuts" && <ShortcutList />}
          {section === "models" && <Models />}
          {section === "mcp" && <Mcp />}
          {section === "git" && <Git />}
          {section === "archived" && <Archived />}
          {section === "about" && <About />}
        </motion.div>
      </AnimatePresence>
    </div>
  );
}

function Group({ title, children, action }: { title?: string; children: ReactNode; action?: ReactNode }) {
  return (
    <section className="s-group">
      {(title || action) && (
        <div className="s-group-head">
          {title && <h2 className="s-group-title">{title}</h2>}
          {action}
        </div>
      )}
      <div className="s-card">{children}</div>
    </section>
  );
}

function Row({ label, desc, children, stack }: { label: ReactNode; desc?: ReactNode; children?: ReactNode; stack?: boolean }) {
  return (
    <div className={cx("s-row", stack && "stack")}>
      <div className="s-row-text">
        <div className="s-row-label">{label}</div>
        {desc && <div className="s-row-desc">{desc}</div>}
      </div>
      {children && <div className="s-row-control">{children}</div>}
    </div>
  );
}

function General() {
  const s = useApp((st) => st.settings);
  const update = useApp((st) => st.updateSettings);
  const setPermission = useApp((st) => st.setPermission);
  return (
    <>
      <Group title="权限">
        <Row label="新聊天的默认权限" desc={permissionModes.find((p) => p.id === s.defaultPermission)?.note}>
          <Segmented
            label="默认权限"
            value={s.defaultPermission}
            options={permissionModes.map((p) => ({ id: p.id, label: p.label }))}
            onChange={(v) => {
              update({ defaultPermission: v });
              setPermission(v);
            }}
          />
        </Row>
      </Group>
      <Group title="输入与回复">
        <Row label="发送消息" desc={s.sendKey === "enter" ? "Enter 发送，Shift + Enter 换行" : `${modKey} + Enter 发送，Enter 换行`}>
          <Segmented
            label="发送消息"
            value={s.sendKey}
            options={[
              { id: "enter", label: "Enter" },
              { id: "mod-enter", label: `${modKey} + Enter` },
            ]}
            onChange={(v) => update({ sendKey: v })}
          />
        </Row>
        <Row label="自动展开工作步骤" desc="回复完成后仍然展开读取、编辑和命令的明细">
          <Switch label="自动展开工作步骤" checked={s.autoExpandSteps} onChange={(v) => update({ autoExpandSteps: v })} />
        </Row>
        <Row label="后台完成时提醒" desc="你在看别的聊天时，任务完成会弹出提醒">
          <Switch label="后台完成时提醒" checked={s.notifyOnDone} onChange={(v) => update({ notifyOnDone: v })} />
        </Row>
      </Group>
      <Group title="语言">
        <Row label="界面语言">
          <select className="select" value={s.language} onChange={(e) => update({ language: e.target.value as "zh-CN" | "en" })} aria-label="界面语言">
            <option value="zh-CN">简体中文</option>
            <option value="en">English</option>
          </select>
        </Row>
      </Group>
    </>
  );
}

const ACCENTS: { id: Accent; label: string; color: string }[] = [
  { id: "clay", label: "陶土", color: "#c6613f" },
  { id: "blue", label: "海蓝", color: "#0a72e8" },
  { id: "violet", label: "藤紫", color: "#7c5cd6" },
  { id: "green", label: "松绿", color: "#2f8f5b" },
  { id: "graphite", label: "石墨", color: "#3d3c39" },
];

function Appearance() {
  const s = useApp((st) => st.settings);
  const update = useApp((st) => st.updateSettings);
  const reset = useApp((st) => st.resetSettings);
  return (
    <>
      <Group title="主题">
        <div className="theme-cards" role="radiogroup" aria-label="主题">
          {(
            [
              ["light", "浅色"],
              ["dark", "深色"],
              ["system", "跟随系统"],
            ] as const
          ).map(([id, label]) => (
            <button key={id} role="radio" aria-checked={s.theme === id} className={cx("theme-card", s.theme === id && "on")} onClick={() => update({ theme: id })}>
              <span className={cx("theme-thumb", id)}>
                <span className="tt-side" />
                <span className="tt-main">
                  <span className="tt-line" />
                  <span className="tt-line short" />
                  <span className="tt-box" />
                </span>
              </span>
              <span className="theme-label">
                {s.theme === id && <Check size={13} />}
                {label}
              </span>
            </button>
          ))}
        </div>
      </Group>
      <Group title="界面">
        <Row label="强调色" desc="按钮、选中态与链接使用的颜色">
          <div className="swatches" role="radiogroup" aria-label="强调色">
            {ACCENTS.map((a) => (
              <button
                key={a.id}
                role="radio"
                aria-checked={s.accent === a.id}
                aria-label={a.label}
                data-tip={a.label}
                className={cx("swatch", s.accent === a.id && "on")}
                style={{ background: a.color }}
                onClick={() => update({ accent: a.id })}
              />
            ))}
          </div>
        </Row>
        <Row label="聊天字体" desc="衬线体更适合长篇阅读">
          <Segmented
            label="聊天字体"
            value={s.chatFont}
            options={[
              { id: "sans", label: "无衬线" },
              { id: "serif", label: <span style={{ fontFamily: "var(--font-serif)" }}>衬线</span> },
            ]}
            onChange={(v) => update({ chatFont: v })}
          />
        </Row>
        <Row label="聊天字号" desc={`${s.fontSize} px`}>
          <input
            type="range"
            className="range"
            min={13}
            max={18}
            step={1}
            value={s.fontSize}
            aria-label="聊天字号"
            onChange={(e) => update({ fontSize: Number(e.target.value) })}
          />
        </Row>
        <Row label="界面密度">
          <Segmented
            label="界面密度"
            value={s.density}
            options={[
              { id: "comfortable", label: "舒适" },
              { id: "compact", label: "紧凑" },
            ]}
            onChange={(v) => update({ density: v })}
          />
        </Row>
        <Row label="减少动效" desc="关闭弹性动画和过渡">
          <Switch label="减少动效" checked={s.reduceMotion} onChange={(v) => update({ reduceMotion: v })} />
        </Row>
      </Group>
      <div className="s-footer">
        <button className="btn ghost" onClick={reset}>
          <RotateCcw size={14} /> 恢复默认外观
        </button>
      </div>
    </>
  );
}

function Personalization() {
  const s = useApp((st) => st.settings);
  const update = useApp((st) => st.updateSettings);
  const toast = useApp((st) => st.toast);
  const [text, setText] = useState(s.customInstructions);
  const [memories, setMemories] = useState([
    "回复使用简体中文，代码注释也用中文",
    "改代码前先读调用链，结论要实测",
    "偏好简洁实现，不要加多余的防御代码",
  ]);
  const dirty = text !== s.customInstructions;
  return (
    <>
      <Group title="自定义指令">
        <div className="s-pad">
          <textarea
            className="textarea"
            rows={5}
            maxLength={1500}
            placeholder="告诉 MiniCode 你希望它如何工作，例如：先给结论再给细节；修改前先说明计划。"
            value={text}
            onChange={(e) => setText(e.target.value)}
            aria-label="自定义指令"
          />
          <div className="s-pad-foot">
            <span className="muted small">{text.length} / 1500</span>
            <div className="grow" />
            <button className="btn sm ghost" disabled={!dirty} onClick={() => setText(s.customInstructions)}>
              取消
            </button>
            <button
              className="btn sm primary"
              disabled={!dirty}
              onClick={() => {
                update({ customInstructions: text });
                toast({ text: "已保存自定义指令", tone: "success" });
              }}
            >
              保存
            </button>
          </div>
        </div>
      </Group>
      <Group title="记忆">
        <Row label="使用记忆" desc="让 MiniCode 记住你的偏好和项目约定，跨聊天生效">
          <Switch label="使用记忆" checked={s.memory} onChange={(v) => update({ memory: v })} />
        </Row>
        {s.memory &&
          memories.map((m) => (
            <Row key={m} label={<span className="memory">{m}</span>}>
              <button className="icon-btn xs" aria-label="删除记忆" onClick={() => setMemories((list) => list.filter((x) => x !== m))}>
                <Trash2 size={14} />
              </button>
            </Row>
          ))}
        {s.memory && memories.length === 0 && <Row label={<span className="muted">还没有记忆</span>} />}
      </Group>
    </>
  );
}

export const SHORTCUTS: { group: string; items: [string, string][] }[] = [
  {
    group: "通用",
    items: [
      ["新聊天", `${modKey} N`],
      ["搜索与命令", `${modKey} K`],
      ["打开设置", `${modKey} ,`],
      ["键盘快捷键", `${modKey} /`],
    ],
  },
  {
    group: "布局",
    items: [
      ["显示/隐藏侧边栏", `${modKey} B`],
      ["显示/隐藏审阅面板", `${modKey} J`],
    ],
  },
  {
    group: "输入框",
    items: [
      ["发送", "Enter"],
      ["换行", "Shift Enter"],
      ["停止回复", "Esc"],
      ["命令", "/"],
      ["引用文件", "@"],
      ["聚焦输入框", `${modKey} L`],
    ],
  },
];

function ShortcutList() {
  return (
    <>
      {SHORTCUTS.map((g) => (
        <Group key={g.group} title={g.group}>
          {g.items.map(([label, keys]) => (
            <Row key={label} label={label}>
              <span className="keys">
                {keys.split(" ").map((k) => (
                  <kbd key={k} className="kbd">
                    {k}
                  </kbd>
                ))}
              </span>
            </Row>
          ))}
        </Group>
      ))}
    </>
  );
}

function ApiKeyRow({ provider, label, base }: { provider: string; label: string; base: string }) {
  const key = useApp((s) => s.settings.apiKeys[provider] ?? "");
  const apiKeys = useApp((s) => s.settings.apiKeys);
  const update = useApp((s) => s.updateSettings);
  const [show, setShow] = useState(false);
  const [state, setState] = useState<"idle" | "testing" | "ok" | "error">("idle");
  const test = () => {
    setState("testing");
    setTimeout(() => setState(key.trim() ? "ok" : "error"), 900);
  };
  return (
    <div className="s-row stack provider">
      <div className="provider-head">
        <div className="s-row-label">{label}</div>
        <span className={cx("status-pill", key ? "on" : "off")}>{key ? "已配置" : "未配置"}</span>
      </div>
      <div className="provider-fields">
        <label className="field">
          <span className="field-label">API 密钥</span>
          <span className="input-group">
            <input
              className="input mono"
              type={show ? "text" : "password"}
              placeholder="粘贴 API 密钥"
              value={key}
              onChange={(e) => {
                setState("idle");
                update({ apiKeys: { ...apiKeys, [provider]: e.target.value } });
              }}
              aria-label={`${label} API 密钥`}
            />
            <button className="icon-btn xs" aria-label={show ? "隐藏密钥" : "显示密钥"} onClick={() => setShow(!show)}>
              {show ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </span>
        </label>
        <label className="field">
          <span className="field-label">接口地址</span>
          <input className="input mono" defaultValue={base} aria-label={`${label} 接口地址`} />
        </label>
      </div>
      <div className="provider-foot">
        <AnimatePresence mode="wait">
          {state === "ok" && (
            <motion.span key="ok" className="test-result ok" initial={{ opacity: 0, x: -4 }} animate={{ opacity: 1, x: 0 }}>
              <Check size={13} /> 连接成功 · 312 ms
            </motion.span>
          )}
          {state === "error" && (
            <motion.span key="err" className="test-result error" initial={{ opacity: 0, x: -4 }} animate={{ opacity: 1, x: 0 }}>
              请先填写 API 密钥
            </motion.span>
          )}
        </AnimatePresence>
        <div className="grow" />
        <button className="btn sm" onClick={test} disabled={state === "testing"}>
          {state === "testing" ? <Spinner size={12} /> : null}
          {state === "testing" ? "正在测试…" : "测试连接"}
        </button>
      </div>
    </div>
  );
}

function Models() {
  const model = useApp((s) => s.model);
  const setModel = useApp((s) => s.setModel);
  return (
    <>
      <Group title="默认模型">
        <Row label="新聊天使用的模型" desc={models.find((m) => m.id === model)?.note}>
          <select className="select" value={model} onChange={(e) => setModel(e.target.value)} aria-label="默认模型">
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </select>
        </Row>
      </Group>
      <Group title="服务商">
        <ApiKeyRow provider="anthropic" label="Anthropic" base="https://api.anthropic.com" />
        <ApiKeyRow provider="deepseek" label="DeepSeek（OpenAI 兼容）" base="https://api.deepseek.com/v1" />
      </Group>
    </>
  );
}

function Mcp() {
  const mcp = useApp((s) => s.mcp);
  const toggle = useApp((s) => s.toggleMcp);
  const add = useApp((s) => s.addMcp);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [transport, setTransport] = useState("stdio");
  return (
    <>
      <Group
        title="已连接的服务器"
        action={
          <button className="btn sm" onClick={() => setOpen(true)}>
            <Plus size={13} /> 添加服务器
          </button>
        }
      >
        {mcp.map((m) => (
          <Row
            key={m.id}
            label={
              <span className="mcp-name">
                <span className={cx("status-dot", m.status === "connected" ? "green" : m.status === "error" ? "red" : "gray")} />
                <span className="mono">{m.name}</span>
              </span>
            }
            desc={`${m.transport} · ${m.status === "error" ? "连接失败：握手超时，点击重试" : m.status === "disabled" ? "已停用" : `${m.tools} 个工具`}`}
          >
            <Switch label={`启用 ${m.name}`} checked={m.enabled} onChange={() => toggle(m.id)} />
          </Row>
        ))}
      </Group>
      <Dialog
        open={open}
        onClose={() => setOpen(false)}
        title="添加 MCP 服务器"
        footer={
          <>
            <button className="btn" onClick={() => setOpen(false)}>
              取消
            </button>
            <button
              className="btn primary"
              disabled={!name.trim()}
              onClick={() => {
                add(name.trim(), transport);
                setName("");
                setOpen(false);
              }}
            >
              连接
            </button>
          </>
        }
      >
        <label className="field">
          <span className="field-label">名称</span>
          <input className="input mono" autoFocus placeholder="例如 github" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="field">
          <span className="field-label">传输方式</span>
          <Segmented
            label="传输方式"
            value={transport}
            onChange={setTransport}
            options={[
              { id: "stdio", label: "stdio" },
              { id: "http", label: "HTTP" },
              { id: "sse", label: "SSE" },
            ]}
          />
        </label>
        <label className="field">
          <span className="field-label">{transport === "stdio" ? "启动命令" : "地址"}</span>
          <input className="input mono" placeholder={transport === "stdio" ? "npx -y @modelcontextprotocol/server-github" : "https://mcp.example.com/mcp"} />
        </label>
      </Dialog>
    </>
  );
}

function Git() {
  const s = useApp((st) => st.settings);
  const update = useApp((st) => st.updateSettings);
  return (
    <Group title="提交">
      <Row label="提交信息前缀" desc="例如 feat:、fix:，留空则由模型决定">
        <input className="input sm mono" value={s.commitPrefix} placeholder="无" onChange={(e) => update({ commitPrefix: e.target.value })} aria-label="提交信息前缀" />
      </Row>
      <Row label="为每个聊天新建分支" desc="在 minicode/ 前缀下创建分支，主分支保持干净">
        <Switch label="为每个聊天新建分支" checked={s.autoBranch} onChange={(v) => update({ autoBranch: v })} />
      </Row>
    </Group>
  );
}

function Archived() {
  const chats = useApp((s) => s.chats).filter((c) => c.archived);
  const restore = useApp((s) => s.restoreChat);
  const del = useApp((s) => s.deleteChat);
  const askConfirm = useApp((s) => s.askConfirm);
  if (chats.length === 0)
    return (
      <Group>
        <Row label={<span className="muted">没有已归档的聊天</span>} />
      </Group>
    );
  return (
    <Group>
      {chats.map((c) => (
        <Row key={c.id} label={c.title} desc={`${relativeTime(c.updatedAt)}前 · ${c.messages.length} 条消息`}>
          <button className="btn sm" onClick={() => restore(c.id)}>
            恢复
          </button>
          <button
            className="icon-btn xs"
            aria-label={`删除 ${c.title}`}
            onClick={() =>
              askConfirm({ title: "永久删除？", body: `「${c.title}」将被删除。`, confirmLabel: "删除", danger: true, onConfirm: () => del(c.id) })
            }
          >
            <Trash2 size={14} />
          </button>
        </Row>
      ))}
    </Group>
  );
}

function About() {
  const toast = useApp((s) => s.toast);
  const [checking, setChecking] = useState(false);
  return (
    <>
      <div className="about">
        <Logo size={56} />
        <div className="about-name">MiniCode</div>
        <div className="muted">版本 0.3.0 · 新界面演示</div>
      </div>
      <Group>
        <Row label="检查更新" desc="上次检查：刚刚">
          <button
            className="btn sm"
            disabled={checking}
            onClick={() => {
              setChecking(true);
              setTimeout(() => {
                setChecking(false);
                toast({ text: "已是最新版本", tone: "success" });
              }, 900);
            }}
          >
            {checking && <Spinner size={12} />}
            {checking ? "检查中…" : "检查更新"}
          </button>
        </Row>
        <Row label="说明" desc="这是一个只在本地运行的界面演示，所有回复与改动都是模拟数据，不会连接后端或修改磁盘文件。" />
      </Group>
    </>
  );
}
