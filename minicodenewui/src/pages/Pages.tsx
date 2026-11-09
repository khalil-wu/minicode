import { motion } from "motion/react";
import { Check, Clock3, Plus, Search } from "lucide-react";
import { useState } from "react";
import { useApp } from "../store";
import { plugins } from "../mock";
import { Dialog, Segmented, Switch } from "../ui/primitives";
import { cx } from "../lib/util";

export function PluginsPage() {
  const installed = useApp((s) => s.installedPlugins);
  const toggle = useApp((s) => s.togglePlugin);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<"all" | "installed">("all");
  const q = query.trim().toLowerCase();
  const list = plugins.filter(
    (p) => (filter === "all" || installed[p.id]) && (!q || p.name.toLowerCase().includes(q) || p.desc.includes(q)),
  );
  return (
    <div className="page">
      <div className="page-inner">
        <div className="page-head">
          <div>
            <h1 className="page-title">插件</h1>
            <p className="page-sub">给 MiniCode 接上你常用的工具。插件只在你允许的聊天里生效。</p>
          </div>
        </div>
        <div className="page-toolbar">
          <label className="search-field grow">
            <Search size={14} className="muted" />
            <input placeholder="搜索插件" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="搜索插件" />
          </label>
          <Segmented
            label="插件筛选"
            value={filter}
            onChange={setFilter}
            options={[
              { id: "all", label: "全部" },
              { id: "installed", label: `已安装 ${Object.values(installed).filter(Boolean).length}` },
            ]}
          />
        </div>
        <div className="plugin-grid">
          {list.map((p, i) => (
            <motion.div
              key={p.id}
              className="plugin-card"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: i * 0.025, duration: 0.24 }}
            >
              <span className="plugin-icon" style={{ background: p.tint }}>
                {p.icon}
              </span>
              <div className="plugin-text">
                <div className="plugin-name">{p.name}</div>
                <div className="plugin-desc">{p.desc}</div>
              </div>
              <button
                className={cx("btn sm", installed[p.id] ? "installed" : "primary")}
                onClick={() => toggle(p.id)}
                aria-label={installed[p.id] ? `卸载 ${p.name}` : `安装 ${p.name}`}
              >
                {installed[p.id] ? (
                  <>
                    <Check size={13} /> <span className="installed-label">已安装</span>
                    <span className="uninstall-label">卸载</span>
                  </>
                ) : (
                  "安装"
                )}
              </button>
            </motion.div>
          ))}
          {list.length === 0 && <div className="panel-empty-sub">没有匹配的插件</div>}
        </div>
      </div>
    </div>
  );
}

export function AutomationsPage() {
  const automations = useApp((s) => s.automations);
  const toggle = useApp((s) => s.toggleAutomation);
  const add = useApp((s) => s.addAutomation);
  const projects = useApp((s) => s.projects);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [schedule, setSchedule] = useState("每天 09:00");
  const [project, setProject] = useState(projects[0]?.name ?? "");
  return (
    <div className="page">
      <div className="page-inner">
        <div className="page-head">
          <div>
            <h1 className="page-title">自动化</h1>
            <p className="page-sub">按计划自动运行的任务，结果会作为新聊天出现在侧边栏。</p>
          </div>
          <button className="btn primary" onClick={() => setOpen(true)}>
            <Plus size={14} /> 新建自动化
          </button>
        </div>
        <div className="s-card">
          {automations.map((a) => (
            <div key={a.id} className={cx("s-row", !a.enabled && "dim")}>
              <span className="auto-icon">
                <Clock3 size={16} />
              </span>
              <div className="s-row-text">
                <div className="s-row-label">{a.name}</div>
                <div className="s-row-desc">
                  {a.schedule} · {a.project} · {a.last}
                </div>
              </div>
              <div className="s-row-control">
                <Switch label={`启用 ${a.name}`} checked={a.enabled} onChange={() => toggle(a.id)} />
              </div>
            </div>
          ))}
        </div>
      </div>
      <Dialog
        open={open}
        onClose={() => setOpen(false)}
        title="新建自动化"
        footer={
          <>
            <button className="btn" onClick={() => setOpen(false)}>
              取消
            </button>
            <button
              className="btn primary"
              disabled={!name.trim()}
              onClick={() => {
                add({ name: name.trim(), schedule, project, enabled: true });
                setName("");
                setOpen(false);
              }}
            >
              创建
            </button>
          </>
        }
      >
        <label className="field">
          <span className="field-label">要做什么</span>
          <input className="input" autoFocus placeholder="例如：每天检查依赖是否有安全漏洞" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="field">
          <span className="field-label">频率</span>
          <select className="select" value={schedule} onChange={(e) => setSchedule(e.target.value)}>
            <option>每天 09:00</option>
            <option>每周一 10:00</option>
            <option>每周五 17:30</option>
            <option>每月 1 日</option>
          </select>
        </label>
        <label className="field">
          <span className="field-label">项目</span>
          <select className="select" value={project} onChange={(e) => setProject(e.target.value)}>
            {projects.map((p) => (
              <option key={p.id}>{p.name}</option>
            ))}
          </select>
        </label>
      </Dialog>
    </div>
  );
}
