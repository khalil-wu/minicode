import { useAppStore } from "../stores";
import { AgentProgressTrace } from "./AgentProgressTrace";

export const PlanPanel = () => {
  const plan = useAppStore((s) => s.plan);
  const permissionMode = useAppStore((s) => s.permissionMode);

  if (!plan) {
    return (
      <div style={{ padding: 12, display: "grid", gap: 10, color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
        <AgentProgressTrace mode="compact" />
        <div>
          No proposed plan in this session.
          {permissionMode === "plan" && (
            <div style={{ marginTop: 6, fontSize: "var(--text-xs)" }}>
              Plan mode is read-only: the agent can inspect and outline, but cannot edit files or execute commands.
            </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div style={{ padding: 12, fontSize: "var(--text-sm)", overflow: "auto", display: "grid", gap: 10 }}>
      <AgentProgressTrace mode="compact" />
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
          <span style={{ fontWeight: 600, color: "var(--text-primary)", flex: 1 }}>Plan</span>
          <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>{plan.status}</span>
        </div>

        <ol style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 8 }}>
          {plan.steps.map((step, i) => (
            <li
              key={step.id}
              style={{
                color:
                  step.status === "done"
                    ? "var(--text-muted)"
                    : i === plan.currentStep
                      ? "var(--accent-primary)"
                      : "var(--text-primary)",
                textDecoration: step.status === "done" ? "line-through" : "none",
              }}
            >
              <div style={{ fontWeight: i === plan.currentStep ? 600 : 400 }}>{step.title}</div>
              {step.detail && (
                <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", marginTop: 2 }}>
                  {step.detail}
                </div>
              )}
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
};
