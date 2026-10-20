// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { PetSprite, petAnimationFrames, petLookFrame } from "./PetSprite";

const codex = { id: "codex", displayName: "Codex", description: "", assetRef: "codex", spriteVersionNumber: 2, updatedAt: 0 };
beforeEach(() => { vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} })); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it("uses the Source frame cadence and all sixteen clockwise look cells", () => {
  const running = petAnimationFrames("running");
  expect(running.frames[0]).toEqual({ row: 7, column: 0, duration: 120 });
  expect(running.loopStart).toBe(18);
  expect(petAnimationFrames("idle").frames.map(frame => frame.duration)).toEqual([1680, 660, 660, 840, 840, 1920]);
  expect(petLookFrame(0)).toEqual({ row: 9, column: 0 });
  expect(petLookFrame(180)).toEqual({ row: 10, column: 0 });
  expect(petLookFrame(337.5)).toEqual({ row: 10, column: 7 });
  expect(petLookFrame(360)).toEqual({ row: 9, column: 0 });
});

it("animates the real atlas and freezes the first state frame for reduced motion", () => {
  vi.useFakeTimers();
  const view = render(<PetSprite pet={codex} />);
  const pet = screen.getByRole("img", { name: "Codex" });
  expect(pet.getAttribute("data-pet-column")).toBe("0");
  act(() => vi.advanceTimersByTime(1680));
  expect(pet.getAttribute("data-pet-column")).toBe("1");
  expect((pet as HTMLElement).style.backgroundImage).toContain("pets/codex.webp");
  view.rerender(<PetSprite pet={codex} state="waiting" reducedMotion />);
  expect(pet.getAttribute("data-pet-row")).toBe("6");
  expect(pet.getAttribute("data-pet-column")).toBe("0");
  expect(vi.getTimerCount()).toBe(0);
});
