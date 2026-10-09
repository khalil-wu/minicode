import { useLayoutEffect, type RefObject } from "react";

/** The reply viewport is the transcript space left by the actual composer. */
export const useReplyViewport = (
  scrollRef: RefObject<HTMLElement | null>,
  followLatest: () => void,
  rendered = true,
) => {
  useLayoutEffect(() => {
    const scroll = scrollRef.current;
    if (!scroll) return;
    const measure = () => {
      const style = getComputedStyle(scroll);
      const padding = Number(style.paddingTop.replace("px", "")) + Number(style.paddingBottom.replace("px", ""));
      scroll.style.setProperty("--reply-viewport-height", `${Math.max(0, scroll.clientHeight - padding)}px`);
      followLatest();
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(scroll);
    return () => observer.disconnect();
  }, [scrollRef, followLatest, rendered]);
};
