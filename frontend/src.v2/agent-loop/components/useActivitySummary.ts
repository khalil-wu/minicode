import { useEffect, useRef, useState } from "react";

/** Active headers hold for one second; a completed summary replaces them now. */
export function useActivitySummary(title: string, key: string, active: boolean) {
  const [displayed, setDisplayed] = useState({ title, key });
  const displayedAt = useRef(Date.now());
  useEffect(() => {
    if (displayed.key === key) return;
    const commit = () => {
      displayedAt.current = Date.now();
      setDisplayed({ title, key });
    };
    const remaining = active ? 1000 - (Date.now() - displayedAt.current) : 0;
    if (remaining <= 0) {
      commit();
      return;
    }
    const timer = window.setTimeout(commit, remaining);
    return () => window.clearTimeout(timer);
  }, [title, key, active, displayed.key]);
  return !active || displayed.key === key ? { title, key } : displayed;
}
