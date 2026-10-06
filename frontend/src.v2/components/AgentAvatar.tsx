import { agentIdentity, type AgentIdentityGlyph } from "../lib/agent-identity";
import "./AgentAvatar.css";

export interface AgentAvatarProps {
  identityKey: string;
  status?: "attention" | "running" | "waiting" | "completed";
  showStatus?: boolean;
  size?: "small" | "medium" | "large";
  className?: string;
}

const AgentGlyphArt = ({ glyph }: { glyph: AgentIdentityGlyph }) => {
  switch (glyph) {
    case "orbit": return <>
      <circle cx="12" cy="12" r="9.1" opacity=".7" />
      <circle cx="12" cy="12" r="7" fill="none" stroke="var(--surface-base)" strokeWidth=".7" />
      <g fill="none" stroke="var(--surface-base)" strokeWidth=".6">
        {[0, 60, 120].map((angle) => <ellipse key={angle} cx="12" cy="12" rx="4.2" ry="7" transform={`rotate(${angle} 12 12)`} />)}
      </g>
    </>;
    case "petal": return <>
      {[0, 60, 120, 180, 240, 300].map((angle, index) => <path key={angle} d="M12 10.7C9.6 9 8.8 5.7 10.2 3.8c.9-1.4 2.7-1.4 3.6 0 1.4 1.9.6 5.2-1.8 6.9Z" opacity={index % 2 ? ".65" : "1"} transform={`rotate(${angle} 12 12)`} />)}
      <circle cx="12" cy="12" r="1.6" opacity=".85" />
    </>;
    case "facets": return <>
      <circle cx="12" cy="1.7" r="1.5" opacity=".8" />
      <circle cx="12" cy="22.3" r="1.5" />
      <rect x="4.95" y="9.25" width="5.5" height="5.5" rx="1.1" transform="rotate(45 7.7 12)" opacity=".55" />
      <rect x="9.25" y="4.95" width="5.5" height="5.5" rx="1.1" transform="rotate(45 12 7.7)" opacity=".8" />
      <rect x="13.55" y="9.25" width="5.5" height="5.5" rx="1.1" transform="rotate(45 16.3 12)" opacity=".65" />
      <rect x="9.25" y="13.55" width="5.5" height="5.5" rx="1.1" transform="rotate(45 12 16.3)" />
    </>;
    case "links": return <>
      <path d="M4.8 2.6h14.4L13 12l6.2 9.4H4.8L11 12Z" opacity=".72" />
      <path d="m4.8 2.6 14.4 18.8M19.2 2.6 4.8 21.4" fill="none" stroke="currentColor" strokeWidth=".8" />
    </>;
    case "spark": return <>
      {[0, 45, 90, 135, 180, 225, 270, 315].map((angle, index) => <path key={angle} d="M12 11c-1.8-2.2-2-5.2 0-8.7 2 3.5 1.8 6.5 0 8.7Z" opacity={index % 2 ? ".6" : "1"} transform={`rotate(${angle} 12 12)`} />)}
      <circle cx="12" cy="12" r="1.6" />
    </>;
    case "bloom": return <>
      {[0, 45, 90, 135, 180, 225, 270, 315].map((angle, index) => <path key={angle} d="m12 1.8 1.4 7.3L12 11l-1.4-1.9Z" opacity={index % 2 ? ".65" : "1"} transform={`rotate(${angle} 12 12)`} />)}
      <circle cx="12" cy="12" r="3.5" fill="none" stroke="currentColor" strokeWidth="1.1" />
    </>;
  }
};

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
      data-identity-color={identity.color}
      data-glyph={identity.glyph}
      data-status={status}
      data-size={size}
      aria-hidden="true"
    >
      <svg className="mc-agent-avatar-art" viewBox="0 0 24 24" focusable="false">
        <AgentGlyphArt glyph={identity.glyph} />
      </svg>
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
