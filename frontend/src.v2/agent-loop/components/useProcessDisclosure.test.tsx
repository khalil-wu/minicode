/* @vitest-environment jsdom */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useAppStore } from "../../stores";
import { useProcessDisclosure } from "./useProcessDisclosure";

function Disclosure({ expanded }: { expanded: boolean }) {
  const { mounted, element } = useProcessDisclosure(expanded);
  return mounted ? <div ref={element}>Process</div> : null;
}

const previousReducedMotion = useAppStore.getState().reducedMotion;
const previousAnimate = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "animate");
afterEach(() => {
  cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks();
  if (previousAnimate) Object.defineProperty(HTMLElement.prototype, "animate", previousAnimate);
  else delete (HTMLElement.prototype as Partial<HTMLElement>).animate;
  useAppStore.setState({ reducedMotion: previousReducedMotion });
});

it("reverses an unfinished disclosure from the visible frame and retires its old completion callback", () => {
  useAppStore.setState({ reducedMotion: false });
  let renderedHeight = 100;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => ({ height: renderedHeight }) as DOMRect);
  const motions: Array<{ playState: string; pending: boolean; cancel: ReturnType<typeof vi.fn>; onfinish: (() => void) | null }> = [];
  const animate = vi.fn((..._args: unknown[]) => {
    const animation = { playState: "running", pending: false,
      cancel: vi.fn(() => { renderedHeight = 100; animation.playState = "idle"; }), onfinish: null as (() => void) | null };
    motions.push(animation);
    return animation;
  });
  Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: animate });
  const view = render(<Disclosure expanded />);
  view.rerender(<Disclosure expanded={false} />);
  renderedHeight = 35;
  view.rerender(<Disclosure expanded />);
  expect(motions[0].onfinish).toBeNull();
  expect(motions[0].cancel).toHaveBeenCalledOnce();
  expect(animate.mock.calls[1][0]).toEqual([
    expect.objectContaining({ height: "35px" }), expect.objectContaining({ height: "100px" }),
  ]);
  expect(view.container.textContent).toBe("Process");
});

it.each([{ reduced: true, system: false }, { reduced: false, system: true }])(
  "uses the resolved app preference when system reduced motion is $system",
  ({ reduced, system }) => {
    vi.stubGlobal("matchMedia", () => ({ matches: system, addEventListener() {}, removeEventListener() {} }));
    const animate = vi.fn(() => ({ cancel: vi.fn(), onfinish: null }));
    Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: animate });
    useAppStore.setState({ reducedMotion: reduced });
    const view = render(<Disclosure expanded={false} />);
    view.rerender(<Disclosure expanded />);
    expect(animate).toHaveBeenCalledTimes(reduced ? 0 : 1);
  },
);
