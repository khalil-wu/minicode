import { useEffect, useRef } from 'react';

/**
 * useFocusTrap - Trap keyboard focus within a container (for modals, dialogs)
 *
 * @param isActive - Whether the focus trap is active
 * @returns containerRef - Ref to attach to the container element
 *
 * Features:
 * - Prevents Tab from leaving the container
 * - Restores focus when trap is deactivated
 * - Disables background scrolling
 * - Supports Shift+Tab for reverse navigation
 *
 * Usage:
 * ```tsx
 * const containerRef = useFocusTrap(isOpen);
 * return <div ref={containerRef} tabIndex={-1}>...</div>
 * ```
 */
export function useFocusTrap(isActive: boolean) {
  const containerRef = useRef<HTMLDivElement>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!isActive) return;

    // Save current focus
    previouslyFocusedRef.current = document.activeElement as HTMLElement;

    // Disable background scrolling
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    // Focus the container
    requestAnimationFrame(() => {
      containerRef.current?.focus();
    });

    // Focus trap handler
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return;

      const container = containerRef.current;
      if (!container) return;

      // Get all focusable elements
      const focusable = container.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"]):not([disabled])'
      );

      if (focusable.length === 0) {
        e.preventDefault();
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;

      // Shift+Tab on first element -> go to last
      if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      }
      // Tab on last element -> go to first
      else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', handleKeyDown);

    // Cleanup
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener('keydown', handleKeyDown);

      // Restore focus
      if (previouslyFocusedRef.current && document.body.contains(previouslyFocusedRef.current)) {
        previouslyFocusedRef.current.focus();
      }
    };
  }, [isActive]);

  return containerRef;
}

/**
 * useEscapeKey - Call a function when Escape key is pressed
 *
 * @param callback - Function to call on Escape
 * @param isActive - Whether the handler is active (default: true)
 *
 * Usage:
 * ```tsx
 * useEscapeKey(() => closeModal(), isModalOpen);
 * ```
 */
export function useEscapeKey(callback: () => void, isActive = true) {
  useEffect(() => {
    if (!isActive) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        callback();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [callback, isActive]);
}

/**
 * usePreventScroll - Prevent body scrolling when active
 *
 * @param isActive - Whether to prevent scrolling
 *
 * Usage:
 * ```tsx
 * usePreventScroll(isModalOpen);
 * ```
 */
export function usePreventScroll(isActive: boolean) {
  useEffect(() => {
    if (!isActive) return;

    const previousOverflow = document.body.style.overflow;
    const previousPaddingRight = document.body.style.paddingRight;

    // Prevent scroll and compensate for scrollbar width
    const scrollbarWidth = window.innerWidth - document.documentElement.clientWidth;
    document.body.style.overflow = 'hidden';
    if (scrollbarWidth > 0) {
      document.body.style.paddingRight = `${scrollbarWidth}px`;
    }

    return () => {
      document.body.style.overflow = previousOverflow;
      document.body.style.paddingRight = previousPaddingRight;
    };
  }, [isActive]);
}
