import type { AgentGlyphTone } from "../lib/agent-view-model";
import "./AgentAvatar.css";

export interface AgentAvatarProps {
  tone?: AgentGlyphTone;
  status?: "attention" | "running" | "waiting" | "completed";
  size?: "small" | "medium" | "large";
  className?: string;
}

const PetalGlyph = () => (
  <>
    {Array.from({ length: 8 }, (_, index) => (
      <ellipse
        key={index}
        cx="16"
        cy="7.2"
        rx="3.15"
        ry="6.15"
        transform={`rotate(${index * 45} 16 16)`}
        opacity={0.62 + (index % 3) * 0.16}
      />
    ))}
    <circle cx="16" cy="16" r="3.2" opacity="0.96" />
  </>
);

const StarGlyph = () => (
  <>
    {Array.from({ length: 8 }, (_, index) => (
      <path
        key={index}
        d="M16 15.7 12.2 4.1 16 1.9l3.8 2.2Z"
        transform={`rotate(${index * 45} 16 16)`}
        opacity={0.55 + (index % 4) * 0.13}
      />
    ))}
    <circle cx="16" cy="16" r="3" />
  </>
);

const BlossomGlyph = () => (
  <>
    {Array.from({ length: 6 }, (_, index) => (
      <circle
        key={index}
        cx="16"
        cy="7.7"
        r="5.3"
        transform={`rotate(${index * 60} 16 16)`}
        opacity={0.46 + (index % 3) * 0.18}
      />
    ))}
    <circle cx="16" cy="16" r="4.1" opacity=".9" />
  </>
);

const GlobeGlyph = () => (
  <>
    <circle cx="16" cy="16" r="12.2" opacity=".28" />
    <circle cx="16" cy="16" r="11.2" fill="none" stroke="currentColor" strokeWidth="1.5" opacity=".84" />
    <ellipse cx="16" cy="16" rx="5.2" ry="11.2" fill="none" stroke="currentColor" strokeWidth="1.5" opacity=".82" />
    <path d="M4.8 16h22.4M16 4.8v22.4" fill="none" stroke="currentColor" strokeWidth="1.5" opacity=".88" />
  </>
);

const AgentGlyphArt = ({ tone }: { tone: AgentGlyphTone }) => {
  if (tone === "amber") return <PetalGlyph />;
  if (tone === "green") return <StarGlyph />;
  if (tone === "rose") return <BlossomGlyph />;
  return <GlobeGlyph />;
};

export const AgentAvatar = ({
  tone = "blue",
  status = "waiting",
  size = "medium",
  className = "",
}: AgentAvatarProps) => (
  <span
    className={`mc-agent-avatar ${className}`.trim()}
    data-tone={tone}
    data-status={status}
    data-size={size}
    aria-hidden="true"
  >
    <svg className="mc-agent-avatar-art" viewBox="0 0 32 32" focusable="false">
      <AgentGlyphArt tone={tone} />
    </svg>
  </span>
);
