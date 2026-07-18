import { useEffect, useRef, useState } from "react";

export function RollingNumber({
  value,
  prefix = "",
  className = "",
  animateOnMount = false,
}: {
  value: number;
  prefix?: string;
  className?: string;
  animateOnMount?: boolean;
}) {
  const lastValue = useRef(value);
  const [previous, setPrevious] = useState<number | null>(null);
  const [sequence, setSequence] = useState(0);

  useEffect(() => {
    if (!animateOnMount || value === 0) return;
    setPrevious(0);
    setSequence((current) => current + 1);
  }, []);

  useEffect(() => {
    if (value === lastValue.current) return;
    setPrevious(lastValue.current);
    lastValue.current = value;
    setSequence((current) => current + 1);
  }, [value]);

  useEffect(() => {
    if (previous == null) return undefined;
    const timer = window.setTimeout(() => setPrevious(null), 240);
    return () => window.clearTimeout(timer);
  }, [previous, sequence]);

  const direction = previous == null || value >= previous ? "up" : "down";
  const text = `${prefix}${value.toLocaleString()}`;
  const previousText = previous == null ? "" : previous === 0 ? "0" : `${prefix}${previous.toLocaleString()}`;

  return (
    <span
      className={["rolling-number", className].filter(Boolean).join(" ")}
      data-direction={direction}
      data-animating={previous != null ? "true" : "false"}
      aria-label={text}
    >
      {previous != null && (
        <span
          key={`old-${sequence}-${previous}`}
          className="rolling-number-value rolling-number-old"
          aria-hidden="true"
          onAnimationEnd={() => setPrevious(null)}
        >
          {previousText}
        </span>
      )}
      <span
        key={`new-${sequence}-${value}`}
        className="rolling-number-value rolling-number-new"
      >
        {text}
      </span>
    </span>
  );
}
