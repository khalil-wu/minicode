import { desktop } from "./runtime";

/** The caller supplies a confirmed celebration; enabling Toys never fires it. */
export async function fireConfetti(reducedMotion: boolean): Promise<boolean> {
  const api = desktop();
  if (!api || reducedMotion || !document.hasFocus() || !(await api.preferences.get()).confettiEnabled) return false;
  const canvas = document.createElement("canvas");
  canvas.setAttribute("aria-hidden", "true");
  Object.assign(canvas.style, { position: "fixed", inset: "0", width: "100%", height: "100%", pointerEvents: "none", zIndex: "10000" });
  const width = window.innerWidth, height = window.innerHeight;
  canvas.width = width; canvas.height = height;
  document.body.append(canvas);
  const context = canvas.getContext("2d")!;
  const colors = ["#ff6b6b", "#ffd93d", "#6bcb77", "#4d96ff", "#b983ff"];
  const pieces = Array.from({ length: 100 }, (_, index) => ({
    x: width / 2, y: height * .3, vx: (Math.random() - .5) * 18,
    vy: -8 - Math.random() * 10, rotation: Math.random() * Math.PI, spin: (Math.random() - .5) * .2, color: colors[index % colors.length],
  }));
  let started: number | undefined;
  const draw = (now: number) => {
    started ??= now;
    if (now - started >= 2400) { canvas.remove(); return; }
    context.clearRect(0, 0, width, height);
    context.globalAlpha = Math.min(1, (2400 - now + started) / 500);
    for (const piece of pieces) {
      piece.x += piece.vx; piece.y += piece.vy; piece.vy += .22; piece.vx *= .99; piece.rotation += piece.spin;
      context.save(); context.translate(piece.x, piece.y); context.rotate(piece.rotation); context.fillStyle = piece.color;
      context.fillRect(-4, -2, 8, 4); context.restore();
    }
    requestAnimationFrame(draw);
  };
  requestAnimationFrame(draw);
  return true;
}
