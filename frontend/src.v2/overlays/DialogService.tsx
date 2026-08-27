import { createRoot } from "react-dom/client";
import { type CSSProperties, useEffect, useRef, useState } from "react";
import { useFocusTrap } from "../hooks/useFocusTrap";

interface ConfirmOptions {
  title?: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}

interface PromptOptions {
  title?: string;
  message: string;
  defaultValue?: string;
  placeholder?: string;
  confirmLabel?: string;
  cancelLabel?: string;
}

interface AlertOptions {
  title?: string;
  message: string;
  confirmLabel?: string;
}

export function showConfirm(options: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const cleanup = () => {
      root.unmount();
      container.remove();
    };
    root.render(
      <ConfirmDialog
        {...options}
        onResult={(ok) => { cleanup(); resolve(ok); }}
      />,
    );
  });
}

export function showAlert(options: AlertOptions): Promise<void> {
  return new Promise((resolve) => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const cleanup = () => {
      root.unmount();
      container.remove();
    };
    root.render(
      <AlertDialog
        {...options}
        onClose={() => { cleanup(); resolve(); }}
      />,
    );
  });
}

export function showPrompt(options: PromptOptions): Promise<string | null> {
  return new Promise((resolve) => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const cleanup = () => {
      root.unmount();
      container.remove();
    };
    root.render(
      <PromptDialog
        {...options}
        onResult={(val) => { cleanup(); resolve(val); }}
      />,
    );
  });
}

// --- Dialog Components ---

const Backdrop = ({ children, label, onDismiss }: { children: React.ReactNode; label: string; onDismiss: () => void }) => {
  const dialogRef = useFocusTrap(true);
  return (
    <div
      role="presentation"
      className="mc-dialog-backdrop"
      onClick={onDismiss}
      style={backdropStyle}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        className="mc-dialog-panel"
        onClick={(e) => e.stopPropagation()}
        style={panelStyle}
      >
        {children}
      </div>
    </div>
  );
};

const ConfirmDialog = ({
  title,
  message,
  confirmLabel = "确认",
  cancelLabel = "取消",
  danger,
  onResult,
}: ConfirmOptions & { onResult: (ok: boolean) => void }) => {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onResult(false);
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onResult]);

  return (
    <Backdrop label={title || "确认操作"} onDismiss={() => onResult(false)}>
      {title && <div style={titleStyle}>{title}</div>}
      <div style={messageStyle}>{message}</div>
      <div style={actionsStyle}>
        <button type="button" className="mc-dialog-btn" onClick={() => onResult(false)} autoFocus={Boolean(danger)} style={cancelBtnStyle}>
          {cancelLabel}
        </button>
        <button
          type="button"
          className="mc-dialog-btn"
          onClick={() => onResult(true)}
          autoFocus={!danger}
          style={{ ...confirmBtnStyle, background: danger ? "var(--state-danger)" : "var(--accent-primary)" }}
        >
          {confirmLabel}
        </button>
      </div>
    </Backdrop>
  );
};

const AlertDialog = ({
  title,
  message,
  confirmLabel = "确定",
  onClose,
}: AlertOptions & { onClose: () => void }) => {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" || e.key === "Enter") onClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  return (
    <Backdrop label={title || "提示"} onDismiss={onClose}>
      {title && <div style={titleStyle}>{title}</div>}
      <div style={messageStyle}>{message}</div>
      <div style={actionsStyle}>
        <button type="button" className="mc-dialog-btn" onClick={onClose} autoFocus style={confirmBtnStyle}>
          {confirmLabel}
        </button>
      </div>
    </Backdrop>
  );
};

const PromptDialog = ({
  title,
  message,
  defaultValue = "",
  placeholder,
  confirmLabel = "确定",
  cancelLabel = "取消",
  onResult,
}: PromptOptions & { onResult: (val: string | null) => void }) => {
  const [value, setValue] = useState(defaultValue);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const submit = () => onResult(value || null);

  return (
    <Backdrop label={title || "需要输入"} onDismiss={() => onResult(null)}>
      {title && <div style={titleStyle}>{title}</div>}
      <div style={messageStyle}>{message}</div>
      <input
        ref={inputRef}
        type="text"
        value={value}
        placeholder={placeholder}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
          if (e.key === "Escape") onResult(null);
        }}
        style={inputStyle}
      />
      <div style={actionsStyle}>
        <button type="button" className="mc-dialog-btn" onClick={() => onResult(null)} style={cancelBtnStyle}>
          {cancelLabel}
        </button>
        <button type="button" className="mc-dialog-btn" onClick={submit} style={confirmBtnStyle}>
          {confirmLabel}
        </button>
      </div>
    </Backdrop>
  );
};

// --- Styles ---

const backdropStyle: CSSProperties = {
  position: "fixed",
  inset: 0,
  zIndex: "var(--z-dialog)",
  display: "grid",
  placeItems: "center",
  background: "var(--backdrop-strong)",
  padding: 16,
};

const panelStyle: CSSProperties = {
  width: "min(380px, 100%)",
  background: "var(--surface-raised)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-lg)",
  boxShadow: "var(--shadow-strong-overlay)",
  padding: 16,
};

const titleStyle: CSSProperties = {
  fontSize: "var(--text-md)",
  color: "var(--text-primary)",
  fontWeight: "var(--fw-bold)",
  marginBottom: 6,
};

const messageStyle: CSSProperties = {
  color: "var(--text-secondary)",
  fontSize: "var(--text-sm)",
  lineHeight: 1.45,
  whiteSpace: "pre-wrap",
};

const actionsStyle: CSSProperties = {
  display: "flex",
  justifyContent: "flex-end",
  gap: 8,
  marginTop: 16,
};

const cancelBtnStyle: CSSProperties = {
  border: "1px solid var(--border-subtle)",
  background: "var(--surface-soft)",
  color: "var(--text-secondary)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: "6px 12px",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
};

const confirmBtnStyle: CSSProperties = {
  border: 0,
  background: "var(--accent-primary)",
  color: "var(--text-on-accent)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: "6px 12px",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  fontWeight: "var(--fw-bold)",
};

const inputStyle: CSSProperties = {
  width: "100%",
  marginTop: 12,
  padding: "8px 10px",
  fontSize: "var(--text-sm)",
  fontFamily: "var(--font-ui)",
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  color: "var(--text-primary)",
  outline: "none",
};
