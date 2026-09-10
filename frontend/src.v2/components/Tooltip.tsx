import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

export type TipSide = "top" | "bottom";

interface TipProps {
  content: ReactNode;
  side?: TipSide;
  children: ReactNode;
}

export function Tip({ content, side = "top", children }: TipProps) {
  const anchor = useRef<HTMLSpanElement>(null);
  const bubble = useRef<HTMLSpanElement>(null);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  const open = hovered || focused;

  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const target = anchor.current!.getBoundingClientRect();
      const hint = bubble.current!.getBoundingClientRect();
      const above = target.top - hint.height - 6;
      const below = target.bottom + 6;
      const preferred = side === "top" ? above : below;
      const alternate = side === "top" ? below : above;
      const top = preferred < 8 || preferred + hint.height > window.innerHeight - 8 ? alternate : preferred;
      setPosition({
        left: Math.max(8, Math.min(target.left + (target.width - hint.width) / 2, window.innerWidth - hint.width - 8)),
        top: Math.max(8, Math.min(top, window.innerHeight - hint.height - 8)),
      });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, content, side]);

  return (
    <span ref={anchor} className="mc-tip"
      onPointerEnter={() => setHovered(true)} onPointerLeave={() => setHovered(false)}
      onFocus={() => setFocused(true)} onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false);
      }}
      onKeyDown={(event) => { if (event.key === "Escape") { setHovered(false); setFocused(false); } }}>
      {children}
      {open && createPortal(<span ref={bubble} className="mc-tip-bubble" data-focused={focused} style={position} aria-hidden="true">
        {content}
      </span>, document.body)}
    </span>
  );
}
