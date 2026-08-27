import { useMemo } from "react";
import { ShieldAlert } from "lucide-react";
import type { ToolCallRecord } from "../../../lib/tool-call-reducer";
import { safeJsonParse } from "../../../lib/safe-parse";

// The backend appends a "[sandbox] ..." paragraph to a failed sandboxed command
// so the model knows it can retry with escalated permissions. That text is
// model-facing guidance, not user output, so the renderer surfaces a compact badge.
function extractSandboxHint(text: string): { text: string; sandboxBlocked: boolean } {
  const marker = "[sandbox]";
  const idx = text.indexOf(marker);
  if (idx === -1) return { text, sandboxBlocked: false };
  const before = text.slice(0, idx);
  const after = text.slice(idx + marker.length);
  const breakIdx = after.indexOf("\n\n");
  const rest = breakIdx === -1 ? "" : after.slice(breakIdx + 2);
  return { text: `${before}${rest}`.trim(), sandboxBlocked: true };
}

function parseCommandSummary(summary: string): {
  output: string;
  stderr: string;
  exitCode: string | null;
  timedOut: boolean;
  sandboxBlocked: boolean;
} {
  const trimmed = summary.trim();
  if (!trimmed) return { output: "", stderr: "", exitCode: null, timedOut: false, sandboxBlocked: false };
  try {
    const parsed = safeJsonParse<Record<string, unknown> | null>(trimmed, null);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("not an object");
    const stdout = parsed.stdout ?? parsed.output ?? parsed.result ?? parsed.summary;
    const stderr = parsed.stderr ?? parsed.error;
    const exit = parsed.exit_code ?? parsed.exitCode ?? parsed.code;
    const stdoutText = typeof stdout === "string" ? stdout : "";
    const sandbox = extractSandboxHint(stdoutText);
    return {
      output: sandbox.text,
      stderr: typeof stderr === "string" ? stderr : "",
      exitCode: typeof exit === "number" || typeof exit === "string" ? String(exit) : null,
      timedOut: parsed.timed_out === true || parsed.timeout === true,
      sandboxBlocked: sandbox.sandboxBlocked,
    };
  } catch {
    const sandbox = extractSandboxHint(trimmed);
    return { output: sandbox.text, stderr: "", exitCode: null, timedOut: false, sandboxBlocked: sandbox.sandboxBlocked };
  }
}

export const CommandResultView = ({
  command,
  summary,
  fallback,
}: {
  command: string | null;
  summary: string;
  fallback: string;
}) => {
  const parsed = useMemo(() => parseCommandSummary(summary), [summary]);
  const output = parsed.output || fallback;
  return (
    <div className="grid gap-1.5">
      {command && (
        <div className="grid grid-cols-[12px_minmax(0,1fr)] gap-1.5 px-2 py-1.5 border border-[var(--border-subtle)] rounded bg-[var(--surface-soft)] text-[var(--text-secondary)] overflow-hidden">
          <span className="text-[var(--accent-primary)] font-bold">$</span>
          <span>{command}</span>
        </div>
      )}
      {output && (
        <pre className="m-0 px-2 py-[7px] max-h-[220px] overflow-auto border border-[var(--border-subtle)] rounded bg-[var(--surface-base)] text-[var(--text-secondary)] font-mono text-xs leading-normal whitespace-pre-wrap break-words">
          {output}
        </pre>
      )}
      {parsed.stderr && (
        <pre className="m-0 px-2 py-[7px] max-h-[220px] overflow-auto border border-[var(--border-subtle)] rounded bg-[var(--surface-base)] text-[var(--state-danger)] font-mono text-xs leading-normal whitespace-pre-wrap break-words">
          {parsed.stderr}
        </pre>
      )}
      {(parsed.exitCode || parsed.timedOut || parsed.sandboxBlocked) && (
        <div className="flex gap-2 items-center text-[var(--text-muted)] text-xs font-mono">
          {parsed.exitCode && <span>exit {parsed.exitCode}</span>}
          {parsed.timedOut && <span>超时</span>}
          {parsed.sandboxBlocked && (
            <span
              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border border-[color-mix(in_oklch,var(--state-warning)_40%,var(--border-subtle))] bg-[color-mix(in_oklch,var(--state-warning)_12%,var(--surface-base))] text-[var(--text-secondary)] font-sans"
              title="此命令受到当前沙箱策略限制。Agent 可申请更高权限后重试，但需要你的批准。"
            >
              <ShieldAlert size={14} />
              沙箱策略已阻止
            </span>
          )}
        </div>
      )}
    </div>
  );
};

export const CommandToolRenderer = ({ record, resultSummary = "" }: {
  record: ToolCallRecord;
  resultSummary?: string;
}) => (
  <CommandResultView
    command={typeof (record.args.command ?? record.args.cmd) === "string" ? String(record.args.command ?? record.args.cmd) : null}
    summary={record.summary ?? ""}
    fallback={resultSummary}
  />
);
