import { Check, Circle, LoaderCircle } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useAppStore } from "../../stores";
import type { TurnPlanStep } from "../../protocol/events";
import type { ChatMessage, PlanState } from "../../stores/types";
import { visiblePlanStepStatus } from "../../lib/planVisibility";
import "./turn-plan-progress.css";

interface TurnPlanProgressProps {
  wide?: boolean;
}

/**
 * MiniCode-shaped current-turn plan surface.
 *
 * The pill is intentionally owned by the canonical update_plan snapshot. It
 * never falls back to todos, subagents, background conversations, or generic
 * progress records. Child agents have their own transcript/detail surface.
 */
export function TurnPlanProgress({ wide = false }: TurnPlanProgressProps = {}) {
  const plan = useAppStore((state) => state.plan);
  const messages = useAppStore((state) => state.messages);
  const isStreaming = useAppStore((state) => state.isStreaming);
  const conversationId = useAppStore((state) => state.conversationId);
  const [expanded, setExpanded] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const listId = useId();

  const currentTurnId = useMemo(() => streamingTurnId(messages), [messages]);
  const livePlan = isStreaming && planBelongsToCurrentTurn(plan, conversationId, currentTurnId)
    ? plan
    : null;
  const steps = livePlan?.plan ?? [];
  const explanation = livePlan?.explanation?.trim() ?? "";
  const progress = steps.length > 0 ? planProgress(steps) : null;
  const visible = Boolean(progress);

  useEffect(() => {
    if (!visible) setExpanded(false);
  }, [visible, livePlan?.turnId]);

  useEffect(() => {
    if (!expanded) return;
    const closeOnOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setExpanded(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setExpanded(false);
    };
    document.addEventListener("pointerdown", closeOnOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [expanded]);

  if (!visible) return null;

  const ariaLabel = [
    progress ? `第 ${progress.activeIndex} / ${progress.total} 步` : "",
  ].filter(Boolean).join(" · ");

  return (
    <div
      ref={rootRef}
      className="turn-plan-progress"
      data-status={progress?.status ?? "running"}
      style={{ maxWidth: wide ? "min(560px, calc(100% - 32px))" : "min(480px, calc(100% - 32px))" }}
    >
      <span className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {ariaLabel}
      </span>
      <button
        type="button"
        className="turn-plan-pill"
        aria-label={ariaLabel}
        aria-controls={expanded ? listId : undefined}
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        <span className="turn-plan-pill-icon" aria-hidden="true">
          {progress?.status === "completed"
            ? <Check size={14} />
            : <LoaderCircle size={14} className="turn-plan-spinner" />}
        </span>
        {progress && (
          <span className="turn-plan-pill-count">
            第 {progress.activeIndex} / {progress.total} 步
          </span>
        )}
      </button>
      {expanded && steps.length > 0 && (
        <div
          id={listId}
          className="turn-plan-popover"
          role="dialog"
          aria-label="当前计划"
        >
          {explanation && (
            <div className="turn-plan-popover-explanation">{explanation}</div>
          )}
          <div className="turn-plan-popover-list">
            {steps.map((step, index) => (
              <PlanStepRow
                key={`${index}-${step.step}`}
                step={step}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function PlanStepRow({ step }: { step: TurnPlanStep }) {
  const status = visiblePlanStepStatus(step);
  const isActive = status === "in_progress";
  const Icon = status === "completed"
    ? Check
    : isActive
        ? LoaderCircle
        : Circle;
  return (
    <div
      className="turn-plan-step"
      data-status={status}
      data-active={isActive ? "true" : "false"}
    >
      <Icon
        size={16}
        aria-hidden="true"
        className={isActive ? "turn-plan-step-spinner" : undefined}
      />
      <span>{step.step}</span>
    </div>
  );
}

function streamingTurnId(messages: ChatMessage[]): string | undefined {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === "assistant" && message.isStreaming && message.turnId?.trim()) {
      return message.turnId.trim();
    }
  }
  return undefined;
}

function planBelongsToCurrentTurn(
  plan: PlanState | null | undefined,
  conversationId: string | null,
  currentTurnId?: string,
): plan is PlanState {
  if (!plan || !conversationId || !currentTurnId) return false;
  return plan.threadId === conversationId && plan.turnId === currentTurnId;
}

function planProgress(steps: TurnPlanStep[]): {
  total: number;
  activeIndex: number;
  status: "running" | "completed";
} {
  const active = steps.findIndex((step) => visiblePlanStepStatus(step) === "in_progress");
  const pending = steps.findIndex((step) => visiblePlanStepStatus(step) === "pending");
  const completed = steps.filter((step) => visiblePlanStepStatus(step) === "completed").length;
  const allCompleted = completed === steps.length;
  const index = active >= 0 ? active : pending >= 0 ? pending : Math.max(0, steps.length - 1);
  return {
    total: steps.length,
    activeIndex: allCompleted ? steps.length : index + 1,
    status: allCompleted ? "completed" : "running",
  };
}

export type { TurnPlanProgressProps };
