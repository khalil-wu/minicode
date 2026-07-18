import { useAnimatedNumber } from "../lib/use-animated-number";

/**
 * Compact numeric readout that eases toward its target value instead of
 * snapping (used for the +additions / -deletions pills in progress UI).
 * The first render shows the real value; only subsequent changes animate.
 */
export function RollingNumber({
  value,
  prefix = "",
  className,
  animateOnMount = false,
}: {
  value: number;
  prefix?: string;
  className?: string;
  /** Kept for callers; initial value renders immediately either way. */
  animateOnMount?: boolean;
}) {
  void animateOnMount;
  const displayed = useAnimatedNumber(value, true);
  return <span className={className}>{prefix}{displayed}</span>;
}
