import { useMemo } from "react";
import { FolderOpen, GitBranch, Settings2, Sparkles } from "lucide-react";
import { useAppStore } from "../stores";
import { Composer } from "../composer/Composer";
import { workspaceDisplayName } from "../lib/workspace-display";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";

interface SuggestedTask {
  id: string;
  label: string;
  prompt: string;
  icon: React.ReactNode;
}

const SUGGESTED_TASKS: SuggestedTask[] = [
  {
    id: "review-workspace",
    label: "Review this workspace",
    prompt: "Review this workspace and surface the highest-risk issues first.",
    icon: <GitBranch size={15} />,
  },
  {
    id: "plan-next-step",
    label: "Plan the next step",
    prompt: "Turn the current project state into a short, actionable implementation plan.",
    icon: <Sparkles size={15} />,
  },
];

export const CoworkHome = () => {
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const currentModel = useAppStore((s) => s.currentModel);
  const settingsOpen = useAppStore((s) => s.settingsOpen);
  const toggleSettings = useAppStore((s) => s.toggleSettings);
  const setDraft = useAppStore((s) => s.setDraft);

  const projectLabel = useMemo(
    () => workspaceDisplayName(workingDirectory, "No workspace"),
    [workingDirectory],
  );

  const openModelSettings = () => {
    if (!settingsOpen) toggleSettings();
    window.setTimeout(() => {
      window.dispatchEvent(new CustomEvent("minicode:settings-tab", { detail: "provider" }));
    }, 0);
  };

  return (
    <div className="workbench-home">
      <div className="workbench-home-layout">
        <section className="workbench-home-main">
          <div className="workbench-empty-brand">
            <div className="workbench-empty-mark" aria-hidden="true">
              <Sparkles size={20} />
            </div>
            <div className="workbench-empty-copy-block">
              <div className="workbench-empty-kicker">Cowork</div>
              <h1 className="workbench-empty-title">What needs attention?</h1>
              {workingDirectory && (
                <p className="workbench-empty-copy">
                  {projectLabel}
                </p>
              )}
            </div>
          </div>

          <div className="workbench-home-composer">
            <Composer minimal />
          </div>

          <div className="workbench-home-actions">
            <HomeAction
              icon={<FolderOpen size={15} />}
              label={workingDirectory ? "Switch workspace" : "Open workspace"}
              onClick={() => void openWorkspaceFolder()}
            />
            <HomeAction
              icon={<Settings2 size={15} />}
              label={currentModel ? "Model settings" : "Select model"}
              onClick={openModelSettings}
            />
          </div>

          <div className="workbench-home-prompts" aria-label="Suggested prompts">
            {SUGGESTED_TASKS.map((task) => (
              <button
                key={task.id}
                type="button"
                className="workbench-home-suggestion"
                onClick={() => setDraft(task.prompt)}
              >
                <span className="workbench-home-suggestion-icon" aria-hidden="true">{task.icon}</span>
                <span>{task.label}</span>
              </button>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
};

const HomeAction = ({
  icon,
  label,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
}) => (
  <button type="button" className="workbench-home-action" onClick={onClick}>
    <span aria-hidden="true">{icon}</span>
    <span>{label}</span>
  </button>
);
