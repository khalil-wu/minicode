import type { ReactNode } from "react";

/**
 * Canonical panel empty state: muted icon + title + hint + optional action.
 * Modeled on the browser panel's empty composition; replaces bare one-line
 * muted strings so every panel reads the same when it has nothing to show.
 */
export const EmptyState = ({
  icon,
  title,
  hint,
  action,
  compact = false,
}: {
  icon?: ReactNode;
  title: string;
  hint?: ReactNode;
  action?: ReactNode;
  compact?: boolean;
}) => (
  <div className="mc-empty-state" data-compact={compact ? "true" : undefined}>
    {icon && (
      <div className="mc-empty-state-icon" aria-hidden="true">
        {icon}
      </div>
    )}
    <div className="mc-empty-state-title">{title}</div>
    {hint && <div className="mc-empty-state-hint">{hint}</div>}
    {action && <div className="mc-empty-state-action">{action}</div>}
  </div>
);
