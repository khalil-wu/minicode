// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { fireConfetti } from "./confetti";

const preferences = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("./runtime", () => ({ desktop: () => ({ preferences }) }));
beforeEach(() => { vi.clearAllMocks(); vi.spyOn(document, "hasFocus").mockReturnValue(true); preferences.get.mockResolvedValue({ confettiEnabled: false }); });
afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); });

it("admits no animation when Toys is off or reduced motion is enabled", async () => {
  expect(await fireConfetti(false)).toBe(false);
  preferences.get.mockResolvedValue({ confettiEnabled: true });
  expect(await fireConfetti(true)).toBe(false);
  expect(document.querySelector("canvas")).toBeNull();
});

it("renders and removes the actual cannon only after the preference admits a confirmed celebration", async () => {
  preferences.get.mockResolvedValue({ confettiEnabled: true });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ clearRect() {}, save() {}, translate() {}, rotate() {}, fillRect() {}, restore() {} } as never);
  const frames: FrameRequestCallback[] = [];
  vi.spyOn(window, "requestAnimationFrame").mockImplementation(callback => { frames.push(callback); return frames.length; });
  expect(await fireConfetti(false)).toBe(true);
  expect(document.querySelectorAll("canvas")).toHaveLength(1);
  frames[0](0); frames[1](2500);
  expect(document.querySelector("canvas")).toBeNull();
});
