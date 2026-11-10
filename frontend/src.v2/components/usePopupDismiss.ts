import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { flushSync } from "react-dom";

/** Retain a closing popup for its CSS exit while releasing input immediately. */
export function usePopupDismiss<Args extends unknown[]>(
  surface: RefObject<HTMLElement>,
  onDismiss: (...args: Args) => void,
) {
  const closing = useRef(false);
  const cancelExit = useRef<() => void>();
  const [exiting, setExiting] = useState(false);
  useEffect(() => () => cancelExit.current?.(), []);

  const reset = () => {
    cancelExit.current?.();
    closing.current = false;
    setExiting(false);
  };

  const dismiss = (...args: Args): boolean => {
    if (closing.current) return false;
    closing.current = true;
    const element = surface.current!;
    element.dataset.state = "closed";
    element.inert = true;
    element.setAttribute("aria-hidden", "true");
    setExiting(true);
    // Computed durations use seconds. A popup without an animation can retire
    // immediately; the explicit app preference also skips the exit interval.
    const duration = Number.parseFloat(getComputedStyle(element).animationDuration) * 1000;
    if (document.documentElement.dataset.reducedMotion === "true" || !(duration > 0)) {
      onDismiss(...args);
    } else {
      const finish = () => { cancelExit.current!(); onDismiss(...args); };
      // A new interaction ends the old visual exit before its target handles
      // input. Commit the owner's closed state now so a rapid reopen cannot
      // reuse a closing popup or be dismissed by its previous timer.
      const finishOnInput = () => flushSync(finish);
      const timer = window.setTimeout(finish, duration);
      cancelExit.current = () => {
        window.clearTimeout(timer);
        window.removeEventListener("pointerdown", finishOnInput, true);
        window.removeEventListener("keydown", finishOnInput, true);
        cancelExit.current = undefined;
      };
      window.addEventListener("pointerdown", finishOnInput, true);
      window.addEventListener("keydown", finishOnInput, true);
    }
    return true;
  };
  return { dismiss, exiting, closing, reset };
}
