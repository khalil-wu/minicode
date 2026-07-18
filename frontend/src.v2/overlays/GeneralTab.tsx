import { useEffect, useState } from "react";
import type { EffortLevel, PermissionMode, RemoteImagePolicy } from "../stores/types";
import { Section, EFFORT_LEVELS, choiceStyle } from "./settingsShared";
import { desktop, isDesktop } from "../desktop/runtime";
import { ModelProviderIcon } from "../components/ModelProviderIcon";

const PERMISSION_CHOICES: { id: PermissionMode; label: string; title: string }[] = [
  { id: "ask_permissions", label: "Ask", title: "Ask before file and network actions" },
  { id: "plan", label: "Plan", title: "Read and research only until the plan is approved" },
  { id: "auto", label: "Auto", title: "Automatically read, search, and edit workspace files" },
  { id: "bypass", label: "Full access", title: "No sandbox or approval prompts" },
];

export const GeneralTab = ({
  permissionMode,
  effortLevel,
  currentModel,
  showReasoningEffort,
  effortOptions,
  switchPermissionMode,
  setEffortLevel,
  remoteImagePolicy,
  setRemoteImagePolicy,
}: {
  permissionMode: PermissionMode;
  effortLevel: string;
  currentModel: string;
  showReasoningEffort: boolean;
  effortOptions?: EffortLevel[];
  switchPermissionMode: (mode: PermissionMode) => void;
  setEffortLevel: (level: EffortLevel) => void;
  remoteImagePolicy: RemoteImagePolicy;
  setRemoteImagePolicy: (policy: RemoteImagePolicy) => void;
}) => {
  const selectedEffort = effortOptions?.includes(effortLevel as EffortLevel)
    ? effortLevel
    : effortLevel === "max" && effortOptions?.includes("xhigh")
      ? "xhigh"
      : effortOptions?.[0];

  return (
  <>
    <Section title="Permissions">
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {PERMISSION_CHOICES.map((mode) => (
          <button key={mode.id} onClick={() => switchPermissionMode(mode.id)} style={choiceStyle(permissionMode === mode.id)} title={mode.title}>
            {mode.label}
          </button>
        ))}
      </div>
    </Section>

    <Section title="Current Model">
      <div style={{ display: "flex", alignItems: "center", gap: 9, fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
        <ModelProviderIcon model={currentModel} size={18} framed />
        <span>{currentModel || "Not configured"}</span>
      </div>
    </Section>

    <Section title="Remote Markdown Images">
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {([
          { id: "ask", label: "Ask", title: "Require explicit permission before loading remote images" },
          { id: "allow", label: "Always allow", title: "Load remote Markdown images automatically" },
          { id: "block", label: "Always block", title: "Never load remote Markdown images" },
        ] as const).map((choice) => (
          <button key={choice.id} onClick={() => setRemoteImagePolicy(choice.id)} style={choiceStyle(remoteImagePolicy === choice.id)} title={choice.title}>
            {choice.label}
          </button>
        ))}
      </div>
    </Section>

    {showReasoningEffort && (
      <Section title="Reasoning Effort">
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {EFFORT_LEVELS.filter((level) => !effortOptions || effortOptions.includes(level.id)).map((level) => (
            <button key={level.id} onClick={() => setEffortLevel(level.id)} style={choiceStyle(selectedEffort === level.id)} title={level.desc}>
              {level.label}
            </button>
          ))}
        </div>
      </Section>
    )}
    {isDesktop() && <DesktopUpdates />}
    </>
  );
};

const DesktopUpdates = () => {
  const [status, setStatus] = useState("idle");
  const [version, setVersion] = useState("");
  const [percent, setPercent] = useState<number | null>(null);
  useEffect(() => desktop()?.updates.onStatus((payload) => {
    setStatus(payload.status);
    setVersion(payload.version || "");
    setPercent(typeof payload.percent === "number" ? payload.percent : null);
  }), []);
  const label = status === "checking" ? "Checking…"
    : status === "available" ? `Version ${version || "available"}`
      : status === "downloading" ? `Downloading${percent == null ? "…" : ` ${Math.round(percent)}%`}`
        : status === "ready" ? `Version ${version || "update"} ready`
          : status === "current" ? "MiniCode is up to date"
            : status === "error" ? "Update check failed"
              : "Updates are checked through the configured release feed.";
  return (
    <Section title="Desktop Updates">
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <button style={choiceStyle(false)} onClick={() => void desktop()?.updates.check()} disabled={status === "checking" || status === "downloading"}>
          Check for updates
        </button>
        {status === "available" && <button style={choiceStyle(true)} onClick={() => void desktop()?.updates.download()}>Download</button>}
        {status === "ready" && <button style={choiceStyle(true)} onClick={() => void desktop()?.updates.install()}>Restart and install</button>}
        <span style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>{label}</span>
      </div>
    </Section>
  );
};
