import {
  CheckCircle2,
  Circle,
  CircleAlert,
  CircleDashed,
  Clock3,
  LoaderCircle,
} from "lucide-react";

export type StatusIconStatus =
  | "pending"
  | "pending_approval"
  | "running"
  | "success"
  | "done"
  | "failed"
  | "blocked"
  | "partial"
  | "timeout"
  | "cancelled"
  | "interrupted";

const STATUS_COLOR: Record<StatusIconStatus, string> = {
  pending: "var(--text-muted)",
  pending_approval: "var(--state-warning)",
  running: "var(--state-info)",
  success: "var(--state-success)",
  done: "var(--text-muted)",
  failed: "var(--state-danger)",
  blocked: "var(--state-warning)",
  partial: "var(--state-warning)",
  timeout: "var(--state-danger)",
  cancelled: "var(--text-muted)",
  interrupted: "var(--state-warning)",
};

export function statusIconColor(status: StatusIconStatus): string {
  return STATUS_COLOR[status] ?? "currentColor";
}

export function StatusIcon({
  status,
  size = 14,
  spinningClassName,
  className = "shrink-0",
}: {
  status: StatusIconStatus;
  size?: number;
  spinningClassName?: string;
  className?: string;
}) {
  const color = STATUS_COLOR[status] ?? "currentColor";
  const props = { size, color, className, "data-testid": `status-icon-${status}` };
  if (status === "running") {
    const runningClassName = spinningClassName || [className, "animate-spin"].filter(Boolean).join(" ");
    return <LoaderCircle {...props} className={runningClassName} />;
  }
  if (status === "success") return <CheckCircle2 {...props} />;
  if (status === "done") return <CheckCircle2 {...props} />;
  if (status === "failed" || status === "timeout") return <CircleAlert {...props} />;
  if (status === "pending_approval") return <Clock3 {...props} />;
  if (status === "blocked" || status === "partial" || status === "cancelled" || status === "interrupted") {
    return <CircleDashed {...props} />;
  }
  return <Circle {...props} />;
}

export function BrandMark({
  size = 20,
  className,
}: {
  size?: number;
  className?: string;
}) {
  return (
    <svg
      aria-hidden="true"
      className={className}
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <rect x="4" y="4" width="56" height="56" rx="15" fill="var(--text-primary)" />
      <path d="M18 42V22h5.5L32 34.2 40.5 22H46v20h-6V31.5l-8 11-8-11V42h-6Z" fill="var(--surface-base)" />
      <rect x="39" y="47" width="10" height="3" rx="1.5" fill="var(--accent-primary)" />
    </svg>
  );
}
