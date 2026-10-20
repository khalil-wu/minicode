import { useEffect, useState } from "react";
import { desktop, type DesktopPet } from "../desktop/runtime";
import animations from "../../public/pets/animations.json";
import "./PetSprite.css";

export type PetAnimation = "idle" | keyof typeof animations.states;
type Frame = { row: number; column: number; duration: number };

export function petAnimationFrames(state: PetAnimation, reducedMotion = false, loop = false): { frames: Frame[]; loopStart: number | null } {
  const idle = animations.idleDurations.map((duration, column) => ({ row: 0, column, duration: duration * animations.idleHoldMultiplier }));
  const sequence = state === "idle" ? idle : Array.from({ length: animations.states[state][1] }, (_, column) => ({
    row: animations.states[state][0], column, duration: animations.states[state][column === animations.states[state][1] - 1 ? 3 : 2],
  }));
  if (reducedMotion) return { frames: [sequence[0]], loopStart: null };
  if (state === "idle" || loop) return { frames: sequence, loopStart: 0 };
  const repeated = [...sequence, ...sequence, ...sequence];
  return { frames: [...repeated, ...idle], loopStart: repeated.length };
}

export function petLookFrame(direction: number) {
  const index = ((Math.round(direction / 22.5) % 16) + 16) % 16;
  return { row: 9 + Math.floor(index / 8), column: index % 8 };
}

export function PetSprite({ pet, state = "idle", size = 64, reducedMotion = false, active = true, lookDirection, loop = false }: {
  pet: DesktopPet; state?: PetAnimation; size?: number; reducedMotion?: boolean; active?: boolean; lookDirection?: number; loop?: boolean;
}) {
  const [spriteUrl, setSpriteUrl] = useState("");
  const [error, setError] = useState("");
  const [frame, setFrame] = useState({ row: 0, column: 0 });
  const [systemReducedMotion, setSystemReducedMotion] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const changed = () => setSystemReducedMotion(media.matches);
    media.addEventListener("change", changed);
    return () => media.removeEventListener("change", changed);
  }, []);
  useEffect(() => {
    if (!active) return;
    setError("");
    if (pet.assetRef) { setSpriteUrl(`${import.meta.env.BASE_URL}pets/${pet.assetRef}.webp`); return; }
    let current = true;
    setSpriteUrl("");
    void desktop()!.pets.getSprite(pet.id).then(url => { if (current) setSpriteUrl(url); }).catch(reason => { if (current) setError(String(reason)); });
    return () => { current = false; };
  }, [active, pet.id, pet.assetRef, pet.updatedAt]);
  useEffect(() => {
    if (!active) return;
    if (lookDirection !== undefined && pet.spriteVersionNumber === 2 && ["idle", "running", "waving"].includes(state)) {
      setFrame(petLookFrame(lookDirection)); return;
    }
    const sequence = petAnimationFrames(state, reducedMotion || systemReducedMotion, loop);
    let index = 0;
    let timer: ReturnType<typeof setTimeout>;
    const next = () => {
      setFrame(sequence.frames[index]);
      if (sequence.loopStart === null) return;
      timer = setTimeout(() => { index = index + 1 < sequence.frames.length ? index + 1 : sequence.loopStart!; next(); }, sequence.frames[index].duration);
    };
    next();
    return () => clearTimeout(timer);
  }, [active, state, reducedMotion, systemReducedMotion, lookDirection, loop, pet.spriteVersionNumber]);
  const rows = pet.spriteVersionNumber === 2 ? 11 : 9;
  return <span className="mc-pet-sprite" role="img" aria-label={pet.displayName} title={error || pet.displayName}
    data-pet-id={pet.id} data-pet-state={state} data-pet-row={frame.row} data-pet-column={frame.column}
    style={{ width: size, height: size * animations.cellHeight / animations.cellWidth,
      backgroundImage: spriteUrl ? `url("${spriteUrl}")` : undefined, backgroundSize: `800% ${rows * 100}%`,
      backgroundPosition: `${frame.column / 7 * 100}% ${frame.row / (rows - 1) * 100}%` }}>
    {error && <span className="mc-pet-sprite-error">{error}</span>}
  </span>;
}
