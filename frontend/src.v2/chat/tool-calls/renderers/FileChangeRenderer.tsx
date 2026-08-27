import { shortToolPath } from "../../toolUtils";
import type { ToolCallRecord } from "../../../lib/tool-call-reducer";

function stringArg(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function filePathForRecord(record: ToolCallRecord, inputSummary?: string | null): string {
  const args = record.args ?? {};
  return (
    stringArg(args.file_path) ||
    stringArg(args.path) ||
    stringArg(args.target) ||
    stringArg(args.filename) ||
    stringArg(inputSummary) ||
    ""
  );
}

export function compactText(value: string): string {
  const trimmed = value.trim();
  if (trimmed.length <= 900) return trimmed;
  return `${trimmed.slice(0, 900)}\n[内容已截断，展开工具详情查看完整内容]`;
}

export const FileChangeToolRenderer = ({
  record,
  inputSummary,
  resultSummary = "",
}: {
  record: ToolCallRecord;
  inputSummary?: string | null;
  resultSummary?: string;
}) => {
  const filePath = filePathForRecord(record, inputSummary);
  const diff = record.diff;
  const preview = compactText(record.contentPreview || record.displaySummary || resultSummary);

  return (
    <div className="grid gap-2">
      <div className="grid gap-1.5 px-2 py-1.5 border border-[var(--border-subtle)] rounded bg-[var(--surface-soft)] text-[var(--text-secondary)]">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-[var(--text-muted)] text-3xs font-semibold tracking-normal">文件更改</span>
          <span className="text-[var(--accent-primary)] font-semibold">{record.displayHint || "已更改"}</span>
          {filePath && (
            <span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap font-mono" title={filePath}>
              {shortToolPath(filePath)}
            </span>
          )}
        </div>
        {diff && (diff.plus > 0 || diff.minus > 0) && (
          <div className="flex items-center gap-2 text-xs font-mono">
            {diff.plus > 0 && <span className="text-[var(--state-success)]">+{diff.plus}</span>}
            {diff.minus > 0 && <span className="text-[var(--state-danger)]">-{diff.minus}</span>}
          </div>
        )}
      </div>
      {preview && (
        <div className="text-[var(--text-secondary)] whitespace-pre-wrap break-words">
          {preview}
        </div>
      )}
    </div>
  );
};
