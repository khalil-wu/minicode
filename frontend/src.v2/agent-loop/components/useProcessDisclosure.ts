import { useLayoutEffect, useRef, useState } from "react";
import { useAppStore } from "../../stores";

/** The activity disclosure retains its body through the closing motion. */
export function useProcessDisclosure(expanded: boolean) {
  const [mounted, setMounted] = useState(expanded);
  const [body, setBody] = useState<HTMLDivElement | null>(null);
  const previousExpanded = useRef(expanded);
  const interrupted = useRef<{ height: number; opacity: number } | null>(null);
  const reducedMotion = useAppStore(state => state.reducedMotion);

  useLayoutEffect(() => {
    if (expanded && !mounted) {
      setMounted(true);
      return;
    }
    // Some activity records have no detail body. Start the transition when
    // React attaches the actual element, including when output arrives later.
    if (!mounted || body === null) return;
    const changed = previousExpanded.current !== expanded;
    previousExpanded.current = expanded;
    const currentFrame = interrupted.current;
    interrupted.current = null;
    body.inert = !expanded;
    if (reducedMotion) {
      if (!expanded) setMounted(false);
      return;
    }
    if (!changed) {
      if (!expanded) setMounted(false);
      return;
    }
    const height = body.getBoundingClientRect().height;
    const style = getComputedStyle(body);
    const motion = body.animate([
      { height: `${currentFrame?.height ?? (expanded ? 0 : height)}px`, opacity: currentFrame?.opacity ?? (expanded ? 0 : 1) },
      { height: expanded ? `${height}px` : "0px", opacity: expanded ? 1 : 0 },
    ], { duration: Number.parseFloat(style.getPropertyValue("--duration-base")),
      easing: style.getPropertyValue("--easing-enter").trim(), fill: "both" });
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
  }, [expanded, mounted, reducedMotion, body]);

  return { mounted, element: setBody };
}
