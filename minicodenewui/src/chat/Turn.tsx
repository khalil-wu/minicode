import { AnimatePresence, motion } from "motion/react";
import {
  BookOpen,
  Check,
  ChevronRight,
  Copy,
  FilePen,
  Globe,
  ListChecks,
  RotateCcw,
  Search,
  ShieldQuestion,
  Terminal,
  ThumbsDown,
  ThumbsUp,
  Undo2,
  X,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useApp } from "../store";
import type { AssistantTurn, Step } from "../types";
import { Markdown } from "./Markdown";
import { Spark } from "../ui/brand";
import { Spinner, useTick } from "../ui/primitives";
import { basename, clockTime, copyText, cx, formatDuration } from "../lib/util";

const STEP_ICON: Record<Step["kind"], ReactNode> = {
  think: <Spark size={13} />,
  read: <BookOpen size={14} />,
  search: <Search size={14} />,
  edit: <FilePen size={14} />,
  run: <Terminal size={14} />,
  web: <Globe size={14} />,
};

function stepVerb(s: Step): string {
  const running = s.status === "running";
  switch (s.kind) {
    case "think":
      return running ? "正在思考" : "思考了";
    case "read":
      return running ? "正在读取" : "读取";
    case "search":
      return running ? "正在搜索" : "搜索";
    case "edit":
      return running ? "正在编辑" : "编辑";
    case "run":
      return running ? "正在运行" : s.status === "failed" ? "运行失败" : "运行";
    case "web":
      return running ? "正在浏览" : "浏览";
  }
}

function summarize(steps: Step[]): string {
  const count = (k: Step["kind"]) => steps.filter((s) => s.kind === k).length;
  const parts: string[] = [];
  const reads = count("read") + count("search");
  if (reads) parts.push(`探索 ${reads} 次`);
  if (count("edit")) parts.push(`编辑 ${count("edit")} 个文件`);
  if (count("run")) parts.push(`运行 ${count("run")} 条命令`);
  return parts.join(" · ");
}

export function TurnView({ chatId, turn, isLast }: { chatId: string; turn: AssistantTurn; isLast: boolean }) {
  const autoExpand = useApp((s) => s.settings.autoExpandSteps);
  const running = turn.status === "running";
  const [open, setOpen] = useState(autoExpand);
  useTick(running);
  const elapsed = running ? (Date.now() - turn.startedAt) / 1000 : turn.seconds;
  const showSteps = running || open;
  const workSteps = turn.steps;

  return (
    <motion.article
      className="turn"
      data-status={turn.status}
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.24, ease: [0.2, 0.8, 0.2, 1] }}
    >
      {workSteps.length > 0 && (
        <button className="turn-summary" onClick={() => setOpen((o) => !o)} aria-expanded={showSteps} disabled={running}>
          {running ? (
            <>
              <Spark size={14} active />
              <span className="shimmer">{phaseLabel(turn)}</span>
              <span className="turn-time">{formatDuration(elapsed)}</span>
            </>
          ) : (
            <>
              <span>{turn.status === "stopped" ? "已停止" : "已处理"} {formatDuration(elapsed)}</span>
              {summarize(workSteps) && <span className="turn-summary-sub">{summarize(workSteps)}</span>}
              <ChevronRight size={14} className={cx("chev", open && "open")} />
            </>
          )}
        </button>
      )}

      <AnimatePresence initial={false}>
        {showSteps && workSteps.length > 0 && (
          <motion.ol
            className="steps"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.22, ease: [0.2, 0.8, 0.2, 1] }}
          >
            {workSteps.map((s) => (
              <StepRow key={s.id} step={s} />
            ))}
          </motion.ol>
        )}
      </AnimatePresence>

      {running && workSteps.length === 0 && (
        <div className="turn-summary static">
          <Spark size={14} active />
          <span className="shimmer">正在思考</span>
        </div>
      )}

      <AnimatePresence>
        {turn.approval && !turn.approval.decision && <ApprovalCard key={turn.approval.id} chatId={chatId} turn={turn} />}
        {turn.question && <QuestionCard key={turn.question.id} chatId={chatId} turn={turn} />}
      </AnimatePresence>

      {turn.text && <Markdown text={turn.text} streaming={running && turn.phase === "writing"} />}

      {turn.plan && <PlanCard chatId={chatId} turn={turn} />}

      {turn.files && turn.files.length > 0 && <FilesCard chatId={chatId} turn={turn} />}

      {!running && <TurnActions chatId={chatId} turn={turn} isLast={isLast} />}
    </motion.article>
  );
}

function phaseLabel(turn: AssistantTurn): string {
  if (turn.phase === "waiting") return turn.question ? "等待你的选择" : "等待你的批准";
  if (turn.phase === "writing") return "正在回复";
  const current = [...turn.steps].reverse().find((s) => s.status === "running");
  if (!current) return "正在思考";
  if (current.kind === "think") return "正在思考";
  return `${stepVerb(current)} ${current.target}`;
}

function StepRow({ step }: { step: Step }) {
  const openPanel = useApp((s) => s.openPanel);
  const [open, setOpen] = useState(step.status === "failed");
  const running = step.status === "running";
  const expandable = !!(step.detail || step.output);
  const clickable = step.kind === "edit" || expandable;

  useEffect(() => {
    if (step.status === "failed") setOpen(true);
  }, [step.status]);

  const onClick = () => {
    if (step.kind === "edit" && step.path) openPanel("changes", step.path);
    else if (expandable) setOpen((o) => !o);
  };

  return (
    <motion.li
      className={cx("step", step.kind, step.status)}
      initial={{ opacity: 0, x: -4 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.18 }}
    >
      <button className="step-line" onClick={onClick} disabled={!clickable} aria-expanded={expandable ? open : undefined}>
        <span className="step-icon">{running && step.kind !== "think" ? <Spinner size={13} /> : STEP_ICON[step.kind]}</span>
        <span className={cx("step-verb", running && "shimmer")}>{stepVerb(step)}</span>
        {step.target && <span className={cx("step-target", (step.kind === "run" || step.kind === "search") && "mono")}>{step.target}</span>}
        {step.kind === "edit" && step.status !== "running" && (
          <span className="diffstat">
            <span className="add">+{step.add}</span>
            <span className="del">−{step.del}</span>
          </span>
        )}
        {expandable && <ChevronRight size={13} className={cx("chev step-chev", open && "open")} />}
      </button>
      <AnimatePresence initial={false}>
        {open && expandable && (
          <motion.div
            className="step-detail"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.18 }}
          >
            {step.detail && <div className="step-detail-text">{step.detail}</div>}
            {step.output && (
              <pre className={cx("terminal", step.status === "failed" && "failed")}>
                <span className="terminal-prompt">$ {step.target}</span>
                {"\n"}
                {step.output}
              </pre>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.li>
  );
}

function ApprovalCard({ chatId, turn }: { chatId: string; turn: AssistantTurn }) {
  const decide = useApp((s) => s.decideApproval);
  const approval = turn.approval!;
  const ref = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    ref.current?.focus({ preventScroll: true });
  }, []);
  const isCommand = approval.kind === "command";
  return (
    <motion.div
      className="card approval"
      role="alertdialog"
      aria-label="需要你的批准"
      initial={{ opacity: 0, y: 6, scale: 0.99 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -4, transition: { duration: 0.12 } }}
      transition={{ type: "spring", stiffness: 480, damping: 34 }}
    >
      <div className="card-head">
        <span className="card-icon amber">
          <ShieldQuestion size={16} />
        </span>
        <div>
          <div className="card-title">{isCommand ? "允许运行这条命令？" : "允许修改这个文件？"}</div>
          <div className="card-sub">{approval.reason}</div>
        </div>
      </div>
      <pre className="approval-command">{isCommand ? `$ ${approval.command}` : approval.command}</pre>
      <div className="card-actions">
        <button className="btn ghost" onClick={() => decide(chatId, turn.id, "deny")}>
          拒绝
        </button>
        <div className="grow" />
        {isCommand && (
          <button className="btn" onClick={() => decide(chatId, turn.id, "always")}>
            本聊天中始终允许
          </button>
        )}
        <button ref={ref} className="btn primary" onClick={() => decide(chatId, turn.id, "approve")}>
          允许 <span className="btn-kbd">↵</span>
        </button>
      </div>
    </motion.div>
  );
}

function QuestionCard({ chatId, turn }: { chatId: string; turn: AssistantTurn }) {
  const answer = useApp((s) => s.answerQuestion);
  const q = turn.question!;
  const [picked, setPicked] = useState<string | null>(null);
  const answered = q.answer !== undefined;
  const chosen = q.options.find((o) => o.id === q.answer);

  if (answered) {
    return (
      <div className="answered">
        <Check size={14} />
        {q.prompt}
        <span className="answered-value">{chosen?.label ?? q.answer}</span>
      </div>
    );
  }

  return (
    <motion.div
      className="card question"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, transition: { duration: 0.1 } }}
      onKeyDown={(e) => {
        const n = Number(e.key);
        if (n >= 1 && n <= q.options.length) setPicked(q.options[n - 1].id);
      }}
    >
      <div className="card-title">{q.prompt}</div>
      <div className="options" role="radiogroup" aria-label={q.prompt}>
        {q.options.map((o, i) => (
          <button
            key={o.id}
            role="radio"
            aria-checked={picked === o.id}
            className={cx("option", picked === o.id && "on")}
            onClick={() => setPicked(o.id)}
            onDoubleClick={() => answer(chatId, turn.id, o.id)}
            autoFocus={i === 0}
          >
            <span className="option-index">{i + 1}</span>
            <span className="option-text">
              <span className="option-label">{o.label}</span>
              <span className="option-desc">{o.desc}</span>
            </span>
            <span className="option-radio" />
          </button>
        ))}
      </div>
      <div className="card-actions">
        <span className="card-hint">按数字键选择，双击直接提交</span>
        <div className="grow" />
        <button className="btn primary" disabled={!picked} onClick={() => picked && answer(chatId, turn.id, picked)}>
          继续
        </button>
      </div>
    </motion.div>
  );
}

function PlanCard({ chatId, turn }: { chatId: string; turn: AssistantTurn }) {
  const accept = useApp((s) => s.acceptPlan);
  const setDraft = useApp((s) => s.setDraft);
  const plan = turn.plan!;
  const accepted = plan.state === "accepted";
  return (
    <div className={cx("card plan", accepted && "accepted")}>
      <div className="card-head">
        <span className="card-icon accent">
          <ListChecks size={16} />
        </span>
        <div className="card-title">实施计划</div>
        {accepted && <span className="badge success">已采纳</span>}
      </div>
      <Markdown text={plan.markdown} />
      {!accepted && (
        <div className="card-actions">
          <button
            className="btn ghost"
            onClick={() => {
              setDraft(chatId, { text: "计划需要调整：" });
              window.dispatchEvent(new Event("minicode:focus-composer"));
            }}
          >
            继续完善
          </button>
          <div className="grow" />
          <button className="btn primary" onClick={() => accept(chatId, turn.id)}>
            开始实施
          </button>
        </div>
      )}
    </div>
  );
}

function FilesCard({ chatId, turn }: { chatId: string; turn: AssistantTurn }) {
  const openPanel = useApp((s) => s.openPanel);
  const revertTurn = useApp((s) => s.revertTurn);
  const revertFile = useApp((s) => s.revertFile);
  const files = turn.files!;
  const live = files.filter((f) => !f.reverted);
  const add = live.reduce((n, f) => n + f.add, 0);
  const del = live.reduce((n, f) => n + f.del, 0);
  return (
    <div className="card files">
      <div className="files-head">
        <FilePen size={15} className="muted" />
        <span className="files-title">{live.length ? `已编辑 ${live.length} 个文件` : "改动已全部撤销"}</span>
        {live.length > 0 && (
          <span className="diffstat">
            <span className="add">+{add}</span>
            <span className="del">−{del}</span>
          </span>
        )}
        <div className="grow" />
        {live.length > 0 && (
          <button className="btn sm ghost" onClick={() => revertTurn(chatId, turn.id)}>
            <Undo2 size={13} /> 撤销
          </button>
        )}
        <button className="btn sm" onClick={() => openPanel("changes", live[0]?.path ?? files[0].path)}>
          查看变更
        </button>
      </div>
      <ul className="files-list">
        {files.map((f) => (
          <li key={f.path} className={cx("file-row", f.reverted && "reverted")}>
            <button className="file-row-main" onClick={() => openPanel("changes", f.path)} disabled={f.reverted}>
              <span className="file-dir">{f.path.slice(0, f.path.length - basename(f.path).length)}</span>
              <span className="file-name">{basename(f.path)}</span>
            </button>
            {f.reverted ? (
              <span className="badge">已撤销</span>
            ) : (
              <>
                <span className="diffstat">
                  <span className="add">+{f.add}</span>
                  <span className="del">−{f.del}</span>
                </span>
                <button className="icon-btn xs hover-only" aria-label={`撤销 ${basename(f.path)}`} data-tip="撤销这个文件" onClick={() => revertFile(chatId, turn.id, f.path)}>
                  <Undo2 size={13} />
                </button>
              </>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function TurnActions({ chatId, turn, isLast }: { chatId: string; turn: AssistantTurn; isLast: boolean }) {
  const retry = useApp((s) => s.retry);
  const toast = useApp((s) => s.toast);
  const [copied, setCopied] = useState(false);
  const [vote, setVote] = useState<"up" | "down" | null>(null);
  return (
    <div className={cx("turn-actions", isLast && "visible")}>
      <button
        className="icon-btn xs"
        aria-label="复制回复"
        data-tip={copied ? "已复制" : "复制"}
        onClick={async () => {
          await copyText(turn.text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1400);
        }}
      >
        {copied ? <Check size={14} /> : <Copy size={14} />}
      </button>
      <button className="icon-btn xs" aria-label="重新生成" data-tip="重新生成" onClick={() => retry(chatId, turn.id)}>
        <RotateCcw size={14} />
      </button>
      <button
        className={cx("icon-btn xs", vote === "up" && "on")}
        aria-label="有帮助"
        data-tip="有帮助"
        onClick={() => {
          setVote(vote === "up" ? null : "up");
          if (vote !== "up") toast({ text: "感谢反馈" });
        }}
      >
        <ThumbsUp size={14} />
      </button>
      <button
        className={cx("icon-btn xs", vote === "down" && "on")}
        aria-label="没帮助"
        data-tip="没帮助"
        onClick={() => {
          setVote(vote === "down" ? null : "down");
          if (vote !== "down") toast({ text: "已记录，我们会改进" });
        }}
      >
        <ThumbsDown size={14} />
      </button>
      <span className="turn-meta">
        {clockTime(turn.startedAt)} · {turn.model}
        {turn.status === "stopped" && (
          <span className="badge danger">
            <X size={11} /> 已停止
          </span>
        )}
      </span>
    </div>
  );
}
