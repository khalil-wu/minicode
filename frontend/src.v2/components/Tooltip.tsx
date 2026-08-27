import type { ReactNode } from "react";

export type TipSide = "top" | "bottom";

interface TipProps {
  content: ReactNode;
  side?: TipSide;
  children: ReactNode;
}

export function Tip({ content, side = "top", children }: TipProps) {
  return (
    <span className="mc-tip" data-side={side}>
      {children}
      <span className="mc-tip-bubble" aria-hidden="true">
        {content}
      </span>
    </span>
  );
}
