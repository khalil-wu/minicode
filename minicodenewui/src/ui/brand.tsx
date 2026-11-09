import { motion } from "motion/react";

/** MiniCode 标志：陶土色圆角方块里的提示符 */
export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden className="logo">
      <rect width="32" height="32" rx="9" fill="var(--accent)" />
      <path
        d="M10 11.5l5 4.5-5 4.5M17.5 21H23"
        fill="none"
        stroke="var(--accent-ink)"
        strokeWidth="2.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** 思考中的星芒，运行时缓慢旋转并呼吸 */
export function Spark({ size = 16, active = false }: { size?: number; active?: boolean }) {
  return (
    <motion.svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      aria-hidden
      className="spark"
      animate={active ? { rotate: 360, scale: [1, 0.86, 1] } : { rotate: 0, scale: 1 }}
      transition={active ? { rotate: { duration: 6, ease: "linear", repeat: Infinity }, scale: { duration: 1.6, repeat: Infinity } } : { duration: 0.3 }}
    >
      {[0, 30, 60, 90, 120, 150].map((deg) => (
        <rect key={deg} x="11" y="2" width="2" height="20" rx="1" fill="currentColor" transform={`rotate(${deg} 12 12)`} />
      ))}
    </motion.svg>
  );
}
