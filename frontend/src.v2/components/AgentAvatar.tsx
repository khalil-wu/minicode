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

const AngularGlyph = () => (
  <>
    <path d="M16 2.8 22.7 9.5 16 16 9.3 9.5Z" opacity=".94" />
    <path d="m29.2 16-6.7 6.7L16 16l6.5-6.7Z" opacity=".78" />
    <path d="M16 29.2 9.3 22.5 16 16l6.7 6.5Z" opacity=".64" />
    <path d="M2.8 16 9.5 9.3 16 16l-6.5 6.7Z" opacity=".84" />
    <rect x="11.2" y="11.2" width="9.6" height="9.6" rx="2" transform="rotate(45 16 16)" fill="none" stroke="currentColor" strokeWidth="1.45" />
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

const OrbitGlyph = () => (
  <>
    <circle cx="16" cy="16" r="12" opacity=".28" />
    <circle cx="16" cy="17.5" r="9" fill="none" stroke="currentColor" strokeWidth="2" opacity=".68" />
    <circle cx="16" cy="19" r="6.3" fill="none" stroke="currentColor" strokeWidth="2" opacity=".86" />
    <circle cx="16" cy="20.5" r="3.6" opacity=".92" />
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

const DiamondGlyph = () => (
  <>
    <path d="M16 2.6 22.2 9 16 15.4 9.8 9Z" opacity=".72" />
    <path d="m29.4 16-6.2 6.4-6.2-6.4 6.2-6.4Z" opacity=".9" />
    <path d="M16 29.4 9.8 23 16 16.6l6.2 6.4Z" opacity=".56" />
    <path d="M2.6 16 8.8 9.6 15 16l-6.2 6.4Z" opacity=".82" />
    <circle cx="16" cy="16" r="3.2" />
  </>
);

const AgentGlyphArt = ({ tone }: { tone: AgentGlyphTone }) => {
  if (tone === "amber") return <PetalGlyph />;
  if (tone === "green") return <StarGlyph />;
  if (tone === "teal") return <OrbitGlyph />;
  if (tone === "rose") return <BlossomGlyph />;
  if (tone === "blue") return <GlobeGlyph />;
  if (tone === "violet") return <DiamondGlyph />;
  return <AngularGlyph />;
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
