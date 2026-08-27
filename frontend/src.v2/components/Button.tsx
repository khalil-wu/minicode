import { forwardRef } from "react";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { LoaderCircle } from "lucide-react";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "accent" | "danger";
export type ButtonSize = "sm" | "md";

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  primary: "btn-primary",
  secondary: "btn-secondary",
  ghost: "btn-ghost",
  accent: "btn-accent",
  danger: "btn-danger",
};

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  icon?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = "secondary",
    size = "md",
    loading = false,
    icon,
    className,
    children,
    disabled,
    type = "button",
    ...rest
  },
  ref,
) {
  const classes = [
    "btn",
    VARIANT_CLASS[variant],
    size === "sm" ? "btn-sm" : undefined,
    className,
  ].filter(Boolean).join(" ");
  return (
    <button
      ref={ref}
      type={type}
      className={classes}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <LoaderCircle size={14} className="animate-spin shrink-0" aria-hidden="true" /> : icon}
      {children}
    </button>
  );
});

export type IconButtonVariant = "ghost" | "accent" | "danger";

interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: IconButtonVariant;
  compact?: boolean;
  label: string;
  children: ReactNode;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { variant = "ghost", compact = false, label, className, children, type = "button", ...rest },
  ref,
) {
  const classes = [
    "mc-icon-button",
    variant === "accent" ? "mc-icon-button-accent" : undefined,
    variant === "danger" ? "mc-icon-button-danger" : undefined,
    compact ? "mc-icon-button-compact" : undefined,
    className,
  ].filter(Boolean).join(" ");
  return (
    <button
      ref={ref}
      type={type}
      className={classes}
      aria-label={label}
      title={label}
      {...rest}
    >
      {children}
    </button>
  );
});
