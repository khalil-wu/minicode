import { GuardianCommandDenial, GuardianReviewGlyph, GuardianToolReviews } from "./GuardianReviewCell";
import { GuardianManualApproval } from "./GuardianManualApproval";
import { Check, ChevronRight, Copy } from "../../lib/icons";
import { useContext, useEffect, useRef, useState } from "react";
import type { ExecCellState } from "./cellTypes";
import { StatusIcon } from "../../components/icons";
import {
  cellStatusTone,
  execCellStatus,
  formatCellDuration,
  isRunningCellStatus,
} from "./cellStatus";
import "./cells.css";
import { useTranscriptSearch } from "../TranscriptSearchContext";
import { toolCleanupNotice } from "../../lib/tool-call-reducer";
import { ProcessDetailReadingContext, useTranscriptReadingPreference } from "../transcriptReadingState";
import { ToolArtifactImage } from "./ActivityCell";
import { openArtifactPreview } from "../openAttachmentPreview";
import { commandActionKind, commandActionTarget } from "./activityCellHelpers";
import { ToolGlyph } from "../toolUtils";
import { useProcessDisclosure } from "../../agent-loop/components/useProcessDisclosure";
import { useI18n, type Translate } from "../../i18n";
import { useSharedSecondTick } from "../../lib/shared-tick";

/**
 * A command has one compact lifecycle row and one optional output panel. The
 * row is the canonical process projection; the panel is only mounted after
 * explicit disclosure. Command and output copy actions preserve their own
 * content, as in Codex's embedded shell block.
 */
export function ExecCell({
  cell,
  conversationId = "",
  allowGuardianApproval = true,
}: {
  cell: ExecCellState;
  isActive?: boolean;
  conversationId?: string;
  allowGuardianApproval?: boolean;
}) {
  const { t } = useI18n();
  const guardianReviewing = cell.automaticApprovalReviews?.some(review => review.status === "in_progress") === true;
  const status = execCellStatus(cell.status);
  const statusColor = cell.status === "failed" || cell.status === "partial" ? "cancelled" : cellStatusTone(status);
  const nativeCommand = Boolean(cell.commandPresentation);
  const running = isRunningCellStatus(status) || nativeCommand && (cell.status === "pending" || cell.status === "pending_approval");
  const now = useSharedSecondTick(running && !cell.background);
  const duration = cell.background ? "" : formatCellDuration(nativeCommand
    ? running ? cell.executionStartedAt == null ? undefined : now - cell.executionStartedAt : cell.durationMs
    : running ? now - cell.createdAt : cell.durationMs);
  const shouldAutoExpand = !cell.collapsed;
  const [expansionPreference, setExpanded, userToggled] = useTranscriptReadingPreference(`exec:${cell.id}`, shouldAutoExpand);
  const expanded = useTranscriptSearch() || expansionPreference;
  const protectDetailReading = useContext(ProcessDetailReadingContext);
  const disclosure = useProcessDisclosure(expanded);
  const actionKind = commandActionKind(cell.commandActions);
  const isExploration = actionKind !== "commandExecution";
  const title = cell.guardianDeclined ? expanded ? t("命令被自动审核拒绝") : t("命令被自动审核拒绝：{command}", { command: cell.command })
    : guardianReviewing ? t("自动审核中")
    : isExploration ? explorationTitle(actionKind, nativeCommand && running ? "running" : cell.status, t)
      : nativeCommand ? nativeCommandTitle(cell, running, duration, expanded, t)
        : commandTitle(cell.status, Boolean(cell.background), duration, expanded, t);
  const target = isExploration ? commandActionTarget(cell.commandActions!) : cell.command;
  const executedCommand = cell.originalCommand ?? cell.command;
  const [copied, setCopied] = useState<"command" | "output" | null>(null);
  const [expandedCommand, setExpandedCommand] = useState<string | null>(null);
  const previousId = useRef(cell.id);
  const previousRunning = useRef(running);

  useEffect(() => {
    const changed = previousId.current !== cell.id;
    const settled = previousRunning.current && !running;
    if (changed) {
      userToggled.current = false;
      setExpanded(shouldAutoExpand);
    } else if (!userToggled.current && settled) {
      setExpanded(shouldAutoExpand);
    } else if (!userToggled.current && shouldAutoExpand) {
      setExpanded(shouldAutoExpand);
    }
    previousId.current = cell.id;
    previousRunning.current = running;
  }, [cell.collapsed, cell.id, running, shouldAutoExpand]);

  const stdout = cell.stdoutFull ?? cell.stdoutPreview.join("\n");
  const stderr = cell.stderrFull ?? cell.stderrPreview.join("\n");
  const hasOutput = Boolean(stdout.trim() || stderr.trim());
  const cleanupNotice = toolCleanupNotice(cell.cleanupReceipt);
  const commandFullyExpanded = expandedCommand === executedCommand;
  const deniedReview = cell.automaticApprovalReviews?.find(review => review.status === "denied");
  const copy = (kind: "command" | "output") => navigator.clipboard.writeText(
    kind === "command" ? executedCommand : [stdout, stderr].filter(Boolean).join("\n"),
  ).then(() => {
    setCopied(kind);
    window.setTimeout(() => setCopied(null), 1500);
  });

  return (
    <div
      className={`exec-cell ${
        cell.status === "failed"
          ? "exec-cell-failed"
          : cell.status === "pending_approval" || cell.status === "pending"
            ? "exec-cell-pending"
            : running
              ? "exec-cell-running"
              : ""
      }`}
      data-status={cell.status}
      data-activity-kind={actionKind}
      data-command-execution-id={cell.commandExecutionItemId}
      data-expanded={expanded ? "true" : "false"}
    >
      <div className="exec-cell-header-row">
        <button
          type="button"
          className="exec-cell-header-button"
          data-processing={running || guardianReviewing}
          aria-expanded={expanded}
          aria-label={t("{action}命令详情：{title} {command}", { action: t(expanded ? "收起" : "展开"), title, command: cell.command })}
          onClick={() => {
            userToggled.current = true;
            if (!expanded) protectDetailReading?.();
            setExpanded((value) => !value);
          }}
        >
          <span className={`exec-cell-status-badge exec-cell-status-${statusColor}`}>
            {cell.guardianDeclined ? <GuardianReviewGlyph warning /> : nativeCommand || cell.status === "success" || cell.status === "running" || cell.status === "failed" || cell.status === "partial" ? (
              <ToolGlyph kind={actionKind} size={nativeCommand ? 16 : 15} />
            ) : (
              <span aria-hidden="true"><StatusIcon status={cell.status} size={15} /></span>
            )}
          </span>
          <span className="exec-cell-title">{title}</span>
          {!cell.guardianDeclined && (!expanded || isExploration) && (isExploration || !nativeCommand || !running)
            && <span className="exec-cell-command-preview" title={target}>{target}</span>}
          {!cell.guardianDeclined && Boolean(cell.automaticApprovalReviews?.length) && <GuardianReviewGlyph />}
          <span className="exec-cell-toggle" aria-hidden="true">
            <ChevronRight size={14} className="mc-process-disclosure" data-expanded={expanded} />
          </span>
        </button>
      </div>
      {cleanupNotice && <div className="exec-cell-meta" role="status">{cleanupNotice}</div>}
      {disclosure.mounted && (
        <div ref={disclosure.element} className="exec-cell-expanded" role="region" aria-label={t("命令输出")} aria-hidden={!expanded} style={{ pointerEvents: expanded ? "auto" : "none", overflow: "hidden" }}>
          {!cell.guardianDeclined && <GuardianToolReviews reviews={cell.automaticApprovalReviews} />}
          <div className="exec-cell-shell-name">Shell</div>
          <div className="exec-cell-command-line">
            <button type="button" className="exec-cell-output-command" data-expanded={commandFullyExpanded}
              aria-label={t("展开完整命令：{command}", { command: executedCommand })}
              onClick={() => setExpandedCommand(executedCommand)}>
              $ {executedCommand}
            </button>
            <button type="button" className="cell-action-btn exec-cell-copy" aria-label={t(copied === "command" ? "已复制命令" : "复制命令")}
              title={t(copied === "command" ? "已复制命令" : "复制命令")} onClick={() => void copy("command")}>
              {copied === "command" ? <Check size={14} /> : <Copy size={14} />}
            </button>
          </div>
          <div className="exec-cell-output-body">
            <div className="exec-cell-output-scroll">
              <pre className="exec-cell-output-pre">
                {stdout && <span className="exec-cell-output-stdout">{stdout}</span>}
                {stderr && <span className="exec-cell-output-stderr">{stderr}</span>}
                {!cell.guardianDeclined && !hasOutput && !running && !(nativeCommand && cell.executionStartedAt == null)
                  && <span className="exec-cell-no-output">{t(cell.status === "pending_approval" || cell.status === "pending" ? "命令尚未执行" : "无输出")}</span>}
              </pre>
            </div>
            <button type="button" className="cell-action-btn exec-cell-copy" aria-label={t(copied === "output" ? "已复制命令输出" : "复制命令输出")}
              title={t(copied === "output" ? "已复制" : "复制命令输出")} onClick={() => void copy("output")}>
              {copied === "output" ? <Check size={14} /> : <Copy size={14} />}
            </button>
          </div>
          {cell.outputArtifact && <button type="button" className="activity-cell-evidence-button" onClick={() => openArtifactPreview({
            artifactId: cell.outputArtifact!.artifactId, name: t("完整命令输出"), kind: cell.outputArtifact!.kind,
            mediaType: cell.outputArtifact!.mediaType, conversationId,
          })}>{t("查看完整输出")}</button>}
          {cell.artifacts?.map((artifact) => <ToolArtifactImage key={artifact.artifactId} artifact={artifact} conversationId={conversationId} />)}
          {!cell.guardianDeclined && !running && cell.status !== "pending" && cell.status !== "pending_approval"
            && <div className="exec-cell-output-status" data-status={status}>
            {cell.status === "success" && cell.exitCode === 0 && !cell.background && <Check size={13} aria-hidden="true" />}
            <span>{cell.background && cell.status === "success" ? t("已在后台启动")
              : cell.status === "cancelled" && (!nativeCommand || cell.executionStartedAt != null) ? t("已停止")
                : cell.status === "success" && cell.exitCode === 0 ? t("成功")
                  : t("退出码 {code}", { code: cell.exitCode ?? t("未知") })}</span>
          </div>}
          {cell.guardianDeclined && <GuardianCommandDenial reviews={cell.automaticApprovalReviews} />}
          {cell.guardianDeclined && deniedReview && conversationId && allowGuardianApproval && <GuardianManualApproval conversationId={conversationId} review={deniedReview} />}
        </div>
      )}
    </div>
  );
}

function nativeCommandTitle(cell: ExecCellState, running: boolean, duration: string, expanded: boolean, t: Translate): string {
  if (running) return duration ? t("命令已运行 {duration}", { duration }) : t("正在运行命令");
  if (cell.background && cell.status === "success") return t("已在后台启动");
  if (cell.status === "cancelled" && cell.executionStartedAt != null) return duration
    ? t(expanded ? "运行 {duration} 后停止命令" : "运行 {duration} 后停止", { duration })
    : t(expanded ? "已停止命令" : "已停止");
  return duration ? t(expanded ? "命令已在 {duration} 内运行完成" : "已在 {duration} 内运行", { duration })
    : t(expanded ? "已运行命令" : "已运行");
}

function commandTitle(status: ExecCellState["status"], background: boolean, duration: string, expanded: boolean, t: Translate): string {
  if (status === "pending_approval") return t("等待批准运行命令");
  if (status === "pending") return t("准备运行命令");
  if (status === "running") return duration ? t("正在运行 {duration}", { duration }) : t("正在运行");
  if (background && status === "success") return t("已在后台启动");
  if (status === "partial" || status === "failed") return t("运行命令");
  if (status === "cancelled") return t("命令已取消");
  return duration ? t(expanded ? "命令已在{duration}内运行完成" : "已在{duration}内运行", { duration }) : t("已运行");
}

function explorationTitle(kind: "fileRead" | "workspaceSearch" | "workspaceList", status: ExecCellState["status"], t: Translate): string {
  const action = kind === "fileRead" ? "读取" : kind === "workspaceSearch" ? "搜索" : "查看目录";
  if (status === "pending_approval") return t("等待批准{action}", { action: t(action) });
  if (status === "pending") return t("准备{action}", { action: t(action) });
  if (status === "running") return t(kind === "fileRead" ? "正在读取" : kind === "workspaceSearch" ? "正在搜索" : "正在查看目录");
  if (status === "cancelled") return t("{action}已中断", { action: t(action) });
  return status === "success" ? t(kind === "fileRead" ? "已读取" : kind === "workspaceSearch" ? "已搜索" : "已查看目录") : t(action);
}
