import { useEffect, useRef } from 'react';
import type { RefObject } from 'react';

const FOCUSABLE_SELECTOR = [
  'button:not([disabled])',
  '[href]',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"]):not([disabled])',
].join(', ');

function restoreFocus(target: HTMLElement | null | undefined): boolean {
  if (!target || !document.body.contains(target)) return false;

  const existingTabIndex = target.getAttribute('tabindex');
  if (target.tabIndex < 0) {
    target.setAttribute('tabindex', '-1');
  }
  target.focus({ preventScroll: true });
  if (existingTabIndex === null) {
    target.removeAttribute('tabindex');
  } else {
    target.setAttribute('tabindex', existingTabIndex);
  }
  return document.activeElement === target;
}

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
export function useFocusTrap(isActive: boolean, fallbackFocusRef?: RefObject<HTMLElement>) {
  const containerRef = useRef<HTMLDivElement>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!isActive) return;

    // Save current focus
    previouslyFocusedRef.current = document.activeElement as HTMLElement;

    // Disable background scrolling
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    // Prefer the first real control so keyboard users can act immediately.
    requestAnimationFrame(() => {
      const container = containerRef.current;
      container?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR)?.focus();
      if (document.activeElement === previouslyFocusedRef.current) {
        container?.focus();
      }
    });

    // Focus trap handler
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return;

      const container = containerRef.current;
      if (!container) return;

      // Get all focusable elements
      const focusable = container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR);

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

      // Restore focus to the original trigger when it still exists. A nested
      // drawer may have unmounted that trigger, so fall back to explicit shell
      // UI and finally the stable application root instead of leaving focus on
      // document.body.
      const previous = previouslyFocusedRef.current;
      if (
        previous
        && previous !== document.body
        && restoreFocus(previous)
      ) {
        return;
      }
      if (restoreFocus(fallbackFocusRef?.current)) return;
      restoreFocus(
        document.querySelector<HTMLElement>('[data-focus-restore-root]')
          ?? document.getElementById('root'),
      );
    };
  }, [fallbackFocusRef, isActive]);

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
