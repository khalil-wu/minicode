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
    let viewportHeight = 0;
    const measure = (height: number) => {
      // Hidden mounted panes have no viewport. Retain their last measured
      // reply height so returning does not collapse and re-expand the reply.
      if (height === 0) return;
      if (height !== viewportHeight) {
        viewportHeight = height;
        scroll.style.setProperty("--reply-viewport-height", `${height}px`);
      }
      followLatest();
    };
    const style = getComputedStyle(scroll);
    const padding = Number(style.paddingTop.replace("px", "")) + Number(style.paddingBottom.replace("px", ""));
    measure(Math.max(0, scroll.clientHeight - padding));
    const observer = new ResizeObserver(([entry]) => measure(entry.contentRect.height));
    observer.observe(scroll);
    return () => observer.disconnect();
  }, [scrollRef, followLatest, rendered]);
};
