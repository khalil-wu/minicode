import { useLayoutEffect, useRef, useState } from "react";

/** Codex's activity disclosure retains its body through the closing motion. */
export function useProcessDisclosure(expanded: boolean) {
  const [mounted, setMounted] = useState(expanded);
  const element = useRef<HTMLDivElement>(null);
  const previousExpanded = useRef(expanded);

  useLayoutEffect(() => {
    if (expanded && !mounted) {
      setMounted(true);
      return;
    }
    if (!mounted || previousExpanded.current === expanded) return;
    previousExpanded.current = expanded;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      if (!expanded) setMounted(false);
      return;
    }
    const body = element.current!;
    const height = body.getBoundingClientRect().height;
    const motion = body.animate([
      { height: expanded ? "0px" : `${height}px`, opacity: expanded ? 0 : 1 },
      { height: expanded ? `${height}px` : "0px", opacity: expanded ? 1 : 0 },
    ], { duration: 300, easing: "cubic-bezier(0.19, 1, 0.22, 1)", fill: "both" });
    motion.onfinish = () => {
      if (!expanded) setMounted(false);
      else motion.cancel();
    };
    return () => motion.cancel();
  }, [expanded, mounted]);

  return { mounted, element };
}
