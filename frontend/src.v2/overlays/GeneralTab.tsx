import type { PermissionMode } from "../stores/types";
import { Section, EFFORT_LEVELS, choiceStyle } from "./settingsShared";

const PERMISSION_CHOICES: { id: PermissionMode; label: string; title: string }[] = [
  { id: "ask_permissions", label: "Ask", title: "Ask before file and network actions" },
  { id: "auto", label: "Auto", title: "Automatically read, search, and edit workspace files" },
  { id: "bypass", label: "Full access", title: "No sandbox or approval prompts" },
];

export const GeneralTab = ({
  permissionMode,
  effortLevel,
  currentModel,
  showReasoningEffort,
  switchPermissionMode,
  setEffortLevel,
}: {
  permissionMode: PermissionMode;
  effortLevel: string;
  currentModel: string;
  showReasoningEffort: boolean;
  switchPermissionMode: (mode: PermissionMode) => Promise<void>;
  setEffortLevel: (level: "low" | "medium" | "high" | "max") => void;
}) => (
  <>
    <Section title="Permissions" description="Control how tools and edits are approved">
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {PERMISSION_CHOICES.map((mode) => (
          <button key={mode.id} onClick={() => switchPermissionMode(mode.id)} style={choiceStyle(permissionMode === mode.id)} title={mode.title}>
            {mode.label}
          </button>
        ))}
      </div>
    </Section>

    <Section title="Current Model">
      <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>{currentModel || "Not configured"}</div>
    </Section>

    {showReasoningEffort && (
      <Section title="Reasoning Effort" description="Responses-compatible reasoning models only">
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {EFFORT_LEVELS.map((level) => (
            <button key={level.id} onClick={() => setEffortLevel(level.id)} style={choiceStyle(effortLevel === level.id)} title={level.desc}>
              {level.label}
            </button>
          ))}
        </div>
      </Section>
    )}
  </>
);
