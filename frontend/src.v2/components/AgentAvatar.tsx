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
      <circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" strokeWidth="1.7" />
      <ellipse cx="12" cy="12" rx="4" ry="8" fill="none" stroke="currentColor" strokeWidth="1.7" transform="rotate(35 12 12)" />
      <circle cx="5.5" cy="7.3" r="2.1" />
    </>;
    case "petal": return <>
      {[0, 90, 180, 270].map((angle) => <path key={angle} d="M12 11C6 11 5 7 6.5 4.5 10 4 13 6 12 11Z" transform={`rotate(${angle} 12 12)`} />)}
      <circle cx="12" cy="12" r="1.6" />
    </>;
    case "facets": return <>
      <path d="m12 3 7.8 4.5-7.8 4.4-7.8-4.4Z" />
      <path d="m3.8 9.3 7.2 4.2v8L3.8 17Z" opacity=".7" />
      <path d="m20.2 9.3-7.2 4.2v8l7.2-4.5Z" opacity=".85" />
    </>;
    case "links": return <g fill="none" stroke="currentColor" strokeWidth="2" transform="rotate(-35 12 12)">
      <rect x="3.5" y="7" width="11" height="10" rx="5" />
      <rect x="9.5" y="7" width="11" height="10" rx="5" />
    </g>;
    case "spark": return <>
      <path d="M12 2.5c1.2 5.8 3.7 8.3 9.5 9.5-5.8 1.2-8.3 3.7-9.5 9.5C10.8 15.7 8.3 13.2 2.5 12 8.3 10.8 10.8 8.3 12 2.5Z" />
      <circle cx="12" cy="12" r="2" fill="var(--surface-base)" />
    </>;
    case "bloom": return <>
      {[0, 60, 120, 180, 240, 300].map((angle) => <ellipse key={angle} cx="12" cy="6.3" rx="2.6" ry="3.6" transform={`rotate(${angle} 12 12)`} />)}
      <circle cx="12" cy="12" r="2.2" />
    </>;
  }
};

export const AgentAvatar = ({
  identityKey,
  status = "waiting",
  showStatus = true,
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
