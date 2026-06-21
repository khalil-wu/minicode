import { useEffect, useRef, useState } from "react";

/**
 * Smoothly animates a displayed number toward a target value, frame by frame.
 * Mirrors cc's SpinnerAnimationRow token counter: instead of jumping when the
 * real token count updates, the displayed value catches up by a fraction each
 * frame (min step 1), so large jumps ease in rather than snapping.
 *
 * When not `active`, the hook snaps to the target with no animation.
 */
export function useAnimatedNumber(target: number, active: boolean): number {
  const [displayed, setDisplayed] = useState(target);
  const displayedRef = useRef(target);

  useEffect(() => {
    if (!active) {
      displayedRef.current = target;
      setDisplayed(target);
      return;
    }
    let raf = 0;
    const step = () => {
      const current = displayedRef.current;
      const diff = target - current;
      if (Math.abs(diff) < 1) {
        displayedRef.current = target;
        setDisplayed(target);
        return;
      }
      // Ease ~20% of the remaining gap per frame, with a floor of 1 so small
      // deltas still progress. Negative diff handled symmetrically.
      const move = diff > 0 ? Math.max(1, Math.round(diff * 0.2)) : Math.min(-1, Math.round(diff * 0.2));
      const next = current + move;
      displayedRef.current = next;
      setDisplayed(next);
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [target, active]);

  return displayed;
}
