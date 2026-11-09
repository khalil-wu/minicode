import { useEffect, useRef } from "react";

/** Keep real block geometry before allowing the browser to skip offscreen work. */
export const useMeasuredMarkdownBlock = <Element extends HTMLElement>() => {
  const ref = useRef<Element>(null);
  useEffect(() => {
    const element = ref.current!;
    const observer = new ResizeObserver(([entry]) => {
      // Hidden mounted panes retain their last measured block height.
      if (entry.contentRect.height === 0) return;
      element.style.setProperty("--markdown-block-height", `${entry.contentRect.height}px`);
      element.dataset.measured = "true";
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return ref;
};
