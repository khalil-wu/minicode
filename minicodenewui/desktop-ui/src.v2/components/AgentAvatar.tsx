import { agentIdentity } from "../lib/agent-identity";
import "./AgentAvatar.css";

export interface AgentAvatarProps {
  identityKey: string;
  status?: "attention" | "running" | "waiting" | "completed";
  showStatus?: boolean;
  size?: "small" | "medium" | "large";
  className?: string;
}

export const AgentAvatar = ({
  identityKey,
  status = "waiting",
  showStatus = false,
  size = "medium",
  className = "",
}: AgentAvatarProps) => {
  const identity = agentIdentity(identityKey);
  return (
    <span
      className={`mc-agent-avatar ${className}`.trim()}
      data-identity-index={identity.index}
      data-status={status}
      data-size={size}
      aria-hidden="true"
    >
      <img className="mc-agent-avatar-art mc-agent-avatar-art-light" src={identity.light} alt="" draggable={false} />
      <img className="mc-agent-avatar-art mc-agent-avatar-art-dark" src={identity.dark} alt="" draggable={false} />
      {showStatus && <span className="mc-agent-avatar-status">
        <svg viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" focusable="false">
          {status === "completed" ? <path d="m2 5 2 2 4-4" />
            : status === "attention" ? <><path d="M5 2v3" /><circle cx="5" cy="7.7" r=".8" fill="currentColor" stroke="none" /></>
            : status === "waiting" ? <path d="M3.5 2.5v5m3-5v5" />
            : <circle cx="5" cy="5" r="2.6" fill="currentColor" stroke="none" />}
        </svg>
      </span>}
    </span>
  );
};
