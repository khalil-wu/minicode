import { useEffect, useRef, useState, useCallback } from "react";

export interface VirtualItem {
  index: number;
  start: number;
  size: number;
  end: number;
}

export interface UseVirtualScrollOptions {
  itemCount: number;
  estimateSize: number;
  overscan?: number;
  scrollingDelay?: number;
}

export interface UseVirtualScrollReturn {
  virtualItems: VirtualItem[];
  totalSize: number;
  scrollToIndex: (index: number, options?: { align?: "start" | "center" | "end" }) => void;
  isScrolling: boolean;
}

/**
 * Lightweight virtual scrolling hook for large lists
 * Optimized for message lists with variable heights
 */
export function useVirtualScroll(
  parentRef: React.RefObject<HTMLElement>,
  options: UseVirtualScrollOptions
): UseVirtualScrollReturn {
  const { itemCount, estimateSize, overscan = 3, scrollingDelay = 150 } = options;

  const [scrollTop, setScrollTop] = useState(0);
  const [scrollHeight, setScrollHeight] = useState(0);
  const [isScrolling, setIsScrolling] = useState(false);
  const scrollTimeoutRef = useRef<ReturnType<typeof setTimeout>>();
  const measurementsRef = useRef<Map<number, number>>(new Map());

  // Measure actual item sizes
  const measureItem = useCallback((index: number, element: HTMLElement) => {
    const height = element.getBoundingClientRect().height;
    if (measurementsRef.current.get(index) !== height) {
      measurementsRef.current.set(index, height);
    }
  }, []);

  // Get size for an item
  const getItemSize = useCallback((index: number): number => {
    return measurementsRef.current.get(index) ?? estimateSize;
  }, [estimateSize]);

  // Calculate total size
  const totalSize = Array.from({ length: itemCount }, (_, i) => getItemSize(i)).reduce((a, b) => a + b, 0);

  // Calculate visible range
  const { startIndex, endIndex, virtualItems } = (() => {
    const viewportHeight = scrollHeight;
    let currentOffset = 0;
    let start = 0;
    let end = itemCount - 1;

    // Find start index
    for (let i = 0; i < itemCount; i++) {
      const size = getItemSize(i);
      if (currentOffset + size >= scrollTop) {
        start = Math.max(0, i - overscan);
        break;
      }
      currentOffset += size;
    }

    // Find end index
    currentOffset = 0;
    for (let i = 0; i < itemCount; i++) {
      const size = getItemSize(i);
      currentOffset += size;
      if (currentOffset >= scrollTop + viewportHeight) {
        end = Math.min(itemCount - 1, i + overscan);
        break;
      }
    }

    // Build virtual items
    const items: VirtualItem[] = [];
    let offset = 0;
    for (let i = 0; i < itemCount; i++) {
      const size = getItemSize(i);
      if (i >= start && i <= end) {
        items.push({
          index: i,
          start: offset,
          size,
          end: offset + size,
        });
      }
      offset += size;
    }

    return { startIndex: start, endIndex: end, virtualItems: items };
  })();

  // Handle scroll
  useEffect(() => {
    const element = parentRef.current;
    if (!element) return;

    const handleScroll = () => {
      setScrollTop(element.scrollTop);
      setScrollHeight(element.clientHeight);

      setIsScrolling(true);
      if (scrollTimeoutRef.current) {
        clearTimeout(scrollTimeoutRef.current);
      }
      scrollTimeoutRef.current = setTimeout(() => {
        setIsScrolling(false);
      }, scrollingDelay);
    };

    element.addEventListener("scroll", handleScroll, { passive: true });
    handleScroll(); // Initial measurement

    return () => {
      element.removeEventListener("scroll", handleScroll);
      if (scrollTimeoutRef.current) {
        clearTimeout(scrollTimeoutRef.current);
      }
    };
  }, [parentRef, scrollingDelay]);

  // Scroll to index
  const scrollToIndex = useCallback(
    (index: number, opts?: { align?: "start" | "center" | "end" }) => {
      const element = parentRef.current;
      if (!element) return;

      let offset = 0;
      for (let i = 0; i < index; i++) {
        offset += getItemSize(i);
      }

      const itemSize = getItemSize(index);
      const viewportHeight = element.clientHeight;

      let scrollTo = offset;
      if (opts?.align === "center") {
        scrollTo = offset - (viewportHeight - itemSize) / 2;
      } else if (opts?.align === "end") {
        scrollTo = offset - viewportHeight + itemSize;
      }

      element.scrollTop = Math.max(0, scrollTo);
    },
    [parentRef, getItemSize]
  );

  return {
    virtualItems,
    totalSize,
    scrollToIndex,
    isScrolling,
  };
}

/**
 * Hook to register item measurements
 */
export function useVirtualItem(ref: React.RefObject<HTMLElement>, onMeasure: (height: number) => void) {
  useEffect(() => {
    const element = ref.current;
    if (!element) return;

    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        onMeasure(entry.contentRect.height);
      }
    });

    observer.observe(element);
    return () => observer.disconnect();
  }, [ref, onMeasure]);
}
