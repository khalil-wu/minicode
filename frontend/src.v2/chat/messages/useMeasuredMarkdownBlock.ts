import { useEffect, useRef } from "react";

/** Keep real block geometry before allowing the browser to skip offscreen work. */
export const useMeasuredMarkdownBlock = <Element extends HTMLElement>() => {
  const ref = useRef<Element>(null);
  useEffect(() => {
    const element = ref.current!;
    let frame: number | null = null;
    let height = 0;
    const observer = new ResizeObserver(([entry]) => {
      // Hidden mounted panes retain their last measured block height.
      if (entry.contentRect.height === 0 || entry.contentRect.height === height) return;
      height = entry.contentRect.height;
      // Resize delivery follows layout. Publish its latest geometry on the
      // next frame so containment does not force another layout before paint.
      if (frame !== null) return;
      frame = requestAnimationFrame(() => {
        frame = null;
        element.style.setProperty("--markdown-block-height", `${height}px`);
        element.dataset.measured = "true";
      });
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
      if (frame !== null) cancelAnimationFrame(frame);
    };
  }, []);
  return ref;
};
