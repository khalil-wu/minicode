import { useLayoutEffect, useRef, useState } from "react";
import { useAppStore } from "../../stores";

/** The activity disclosure retains its body through the closing motion. */
export function useProcessDisclosure(expanded: boolean) {
  const [mounted, setMounted] = useState(expanded);
  const element = useRef<HTMLDivElement>(null);
  const previousExpanded = useRef(expanded);
  const interrupted = useRef<{ height: number; opacity: number } | null>(null);
  const reducedMotion = useAppStore(state => state.reducedMotion);

  useLayoutEffect(() => {
    if (expanded && !mounted) {
      setMounted(true);
      return;
    }
    if (!mounted) return;
    const changed = previousExpanded.current !== expanded;
    previousExpanded.current = expanded;
    const currentFrame = interrupted.current;
    interrupted.current = null;
    if (reducedMotion) {
      if (!expanded) setMounted(false);
      return;
    }
    if (!changed) {
      if (!expanded) setMounted(false);
      return;
    }
    const body = element.current!;
    const height = body.getBoundingClientRect().height;
    const motion = body.animate([
      { height: `${currentFrame?.height ?? (expanded ? 0 : height)}px`, opacity: currentFrame?.opacity ?? (expanded ? 0 : 1) },
      { height: expanded ? `${height}px` : "0px", opacity: expanded ? 1 : 0 },
    ], { duration: 300, easing: "cubic-bezier(0.19, 1, 0.22, 1)", fill: "both" });
    motion.onfinish = () => {
      if (!expanded) setMounted(false);
      else motion.cancel();
    };
    return () => {
      if (motion.playState === "running" || motion.pending) {
        interrupted.current = { height: body.getBoundingClientRect().height, opacity: Number(getComputedStyle(body).opacity) };
      }
      motion.onfinish = null;
      motion.cancel();
    };
  }, [expanded, mounted, reducedMotion]);

  return { mounted, element };
}
