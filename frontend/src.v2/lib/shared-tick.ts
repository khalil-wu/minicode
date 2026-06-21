import { useEffect, useState } from "react";

/**
 * Module-level shared 1-second tick.
 *
 * Many running tool cells/cards need a per-second clock to render elapsed
 * timers. Giving each its own setInterval means N independent timers (one per
 * running tool) firing at desync'd phases, each triggering its own re-render
 * scattered across the second. Instead, a single interval drives all
 * subscribers: it starts when the first subscriber joins and stops when the
 * last leaves. Mirrors cc's single-animation-clock pattern (SpinnerAnimationRow
 * / useBlink: "all instances derive state from the same clock").
 */
type SecondTickListener = (now: number) => void;

const listeners = new Set<SecondTickListener>();
let intervalId: ReturnType<typeof setInterval> | null = null;

function ensureRunning(): void {
  if (intervalId !== null) return;
  intervalId = setInterval(() => {
    const now = Date.now();
    for (const fn of listeners) fn(now);
  }, 1000);
}

function maybeStop(): void {
  if (listeners.size === 0 && intervalId !== null) {
    clearInterval(intervalId);
    intervalId = null;
  }
}

/** Subscribe to the shared 1s tick. Returns an unsubscribe function. */
export function subscribeSecondTick(fn: SecondTickListener): () => void {
  listeners.add(fn);
  ensureRunning();
  return () => {
    listeners.delete(fn);
    maybeStop();
  };
}

/**
 * Returns a `now` timestamp that updates every second while `active` is true.
 * Subscribes to the shared tick only while active, so the interval stops when
 * nothing needs it.
 */
export function useSharedSecondTick(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    return subscribeSecondTick(setNow);
  }, [active]);
  return now;
}
