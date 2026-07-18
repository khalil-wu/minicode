import { useEffect, useMemo, useState } from "react";
import { Play, X } from "lucide-react";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { sendChatMessage } from "../chat/sendChatMessage";
import { useAppStore } from "../stores";
import type { PluginCommandPanelPayload } from "../stores/types";
import { pushToast } from "./ToastContainer";

type FieldKind = "text" | "textarea" | "select";

interface PromptField {
  name: string;
  label: string;
  type: FieldKind;
  placeholder?: string;
  defaultValue?: string;
  required?: boolean;
  options?: string[];
}

const toText = (value: unknown): string => (typeof value === "string" ? value.trim() : "");

const normalizeField = (field: unknown): PromptField | null => {
  if (!field || typeof field !== "object") return null;
  const payload = field as Record<string, unknown>;
  const name = toText(payload.name);
  if (!name || !/^[A-Za-z0-9_.-]{1,64}$/.test(name)) return null;
  const rawType = toText(payload.type).toLowerCase();
  const type: FieldKind = rawType === "textarea" ? "textarea" : rawType === "select" ? "select" : "text";
  const options = Array.isArray(payload.options)
    ? payload.options.map(toText).filter(Boolean).slice(0, 20)
    : undefined;
  if (type === "select" && (!options || options.length === 0)) return null;
  return {
    name,
    label: toText(payload.label) || name,
    type,
    placeholder: toText(payload.placeholder) || undefined,
    defaultValue: toText(payload.default) || toText(payload.defaultValue) || "",
    required: Boolean(payload.required),
    options,
  };
};

const normalizeFields = (payload: PluginCommandPanelPayload | null): PromptField[] => {
  const fields = Array.isArray(payload?.fields)
    ? payload.fields.map(normalizeField).filter((field): field is PromptField => Boolean(field))
    : [];
  return fields.length > 0
    ? fields
    : [{
        name: "input",
        label: "Input",
        type: "textarea",
        placeholder: "What should this command work on?",
        defaultValue: toText(payload?.arg),
      }];
};

const applyTemplate = (template: string, values: Record<string, string>): string =>
  template.replace(/\$\{([A-Za-z0-9_.-]+)\}/g, (_match, key: string) => values[key] ?? "");

const buildPrompt = (payload: PluginCommandPanelPayload, values: Record<string, string>): string => {
  const template = toText(payload.prompt_template) || toText(payload.promptTemplate);
  if (template) return applyTemplate(template, values).trim();
  return Object.entries(values)
    .map(([key, value]) => value ? `${key}: ${value}` : "")
    .filter(Boolean)
    .join("\n")
    .trim();
};

export const PluginCommandPanel = () => {
  const open = useAppStore((s) => s.pluginCommandPanelOpen);
  const payload = useAppStore((s) => s.pluginCommandPanelPayload);
  const close = useAppStore((s) => s.closePluginCommandPanel);
  const dialogRef = useFocusTrap(open);
  const fields = useMemo(() => normalizeFields(payload), [payload]);
  const initialValues = useMemo(() => {
    const values: Record<string, string> = {};
    fields.forEach((field) => {
      values[field.name] = field.defaultValue ?? "";
    });
    return values;
  }, [fields]);
  const [values, setValues] = useState<Record<string, string>>(initialValues);

  useEffect(() => {
    if (!open) return;
    setValues(initialValues);
  }, [open, initialValues]);

  if (!open || !payload) return null;

  const component = toText(payload.component);
  const title = toText(payload.title) || (payload.command ? `/${String(payload.command)}` : "Plugin command");
  const description = toText(payload.description);
  const pluginName = toText(payload.plugin_name) || toText(payload.pluginName);
  const submitLabel = toText(payload.submit_label) || toText(payload.submitLabel) || "Send";
  const unsupported = component !== "prompt-form";
  const preview = unsupported ? "" : buildPrompt(payload, values);

  const updateValue = (name: string, value: string) => {
    setValues((current) => ({ ...current, [name]: value }));
  };

  const submit = () => {
    if (unsupported) {
      pushToast(`Unsupported plugin component: ${component || "unknown"}`, "warning");
      return;
    }
    const missing = fields.find((field) => field.required && !toText(values[field.name]));
    if (missing) {
      pushToast(`${missing.label} is required`, "warning");
      return;
    }
    if (!preview) {
      pushToast("Nothing to send.", "warning");
      return;
    }
    const sent = sendChatMessage({ displayContent: preview, backendContent: preview });
    if (sent) close();
  };

  return (
    <div className="overlay-backdrop" onClick={close} style={backdropStyle}>
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        className="modal-content"
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === "Escape") close();
          if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
            event.preventDefault();
            submit();
          }
        }}
        style={modalStyle}
      >
        <div style={headerStyle}>
          <div style={{ minWidth: 0 }}>
            <h2 style={titleStyle}>{title}</h2>
            <div style={metaStyle}>
              {pluginName ? <span>{pluginName}</span> : null}
              {description ? <span>{description}</span> : null}
            </div>
          </div>
          <button type="button" onClick={close} aria-label="Close" title="Close" style={iconButtonStyle}>
            <X size={16} />
          </button>
        </div>

        <div style={bodyStyle}>
          {unsupported ? (
            <div style={emptyStyle}>This plugin component is not available in this build.</div>
          ) : (
            <>
              <div style={fieldsStyle}>
                {fields.map((field) => (
                  <label key={field.name} style={fieldWrapStyle}>
                    <span style={labelStyle}>{field.label}</span>
                    {field.type === "textarea" ? (
                      <textarea
                        value={values[field.name] ?? ""}
                        onChange={(event) => updateValue(field.name, event.target.value)}
                        placeholder={field.placeholder}
                        rows={5}
                        style={{ ...inputStyle, resize: "vertical", minHeight: 112, lineHeight: 1.45 }}
                      />
                    ) : field.type === "select" ? (
                      <select
                        value={values[field.name] ?? ""}
                        onChange={(event) => updateValue(field.name, event.target.value)}
                        style={inputStyle}
                      >
                        <option value="">Select...</option>
                        {field.options?.map((option) => (
                          <option key={option} value={option}>{option}</option>
                        ))}
                      </select>
                    ) : (
                      <input
                        value={values[field.name] ?? ""}
                        onChange={(event) => updateValue(field.name, event.target.value)}
                        placeholder={field.placeholder}
                        style={inputStyle}
                      />
                    )}
                  </label>
                ))}
              </div>
              <div style={previewStyle}>
                <div style={previewLabelStyle}>Preview</div>
                <pre style={previewTextStyle}>{preview || " "}</pre>
              </div>
            </>
          )}
        </div>

        <div style={footerStyle}>
          <button type="button" onClick={close} style={secondaryButtonStyle}>Cancel</button>
          <button type="button" onClick={submit} disabled={unsupported} style={primaryButtonStyle}>
            <Play size={14} />
            <span>{submitLabel}</span>
          </button>
        </div>
      </div>
    </div>
  );
};

const backdropStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "var(--backdrop-overlay)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  padding: 16,
  zIndex: "var(--z-modal)",
  pointerEvents: "auto",
};

const modalStyle: React.CSSProperties = {
  width: "min(640px, 100%)",
  maxHeight: "min(760px, 92vh)",
  background: "var(--surface-raised)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 12px)",
  boxShadow: "var(--shadow-strong, var(--shadow-md))",
  display: "flex",
  flexDirection: "column",
  overflow: "hidden",
};

const headerStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  justifyContent: "space-between",
  gap: 12,
  padding: "14px 16px",
  borderBottom: "1px solid var(--border-subtle)",
};

const titleStyle: React.CSSProperties = {
  margin: 0,
  color: "var(--text-primary)",
  fontSize: 15,
  lineHeight: 1.3,
  fontWeight: 700,
};

const metaStyle: React.CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 8,
  marginTop: 4,
  color: "var(--text-muted)",
  fontSize: 12,
  lineHeight: 1.4,
};

const iconButtonStyle: React.CSSProperties = {
  width: 28,
  height: 28,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  border: 0,
  borderRadius: 8,
  background: "transparent",
  color: "var(--text-muted)",
  cursor: "pointer",
};

const bodyStyle: React.CSSProperties = {
  display: "grid",
  gap: 14,
  padding: 16,
  overflowY: "auto",
};

const fieldsStyle: React.CSSProperties = {
  display: "grid",
  gap: 12,
};

const fieldWrapStyle: React.CSSProperties = {
  display: "grid",
  gap: 6,
};

const labelStyle: React.CSSProperties = {
  color: "var(--text-secondary)",
  fontSize: 12,
  fontWeight: 600,
};

const inputStyle: React.CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  border: "1px solid var(--border-subtle)",
  borderRadius: 8,
  background: "var(--surface-base)",
  color: "var(--text-primary)",
  padding: "9px 10px",
  fontSize: 13,
  outline: "none",
};

const previewStyle: React.CSSProperties = {
  border: "1px solid var(--border-subtle)",
  borderRadius: 8,
  background: "var(--surface-base)",
  overflow: "hidden",
};

const previewLabelStyle: React.CSSProperties = {
  padding: "8px 10px",
  borderBottom: "1px solid var(--border-subtle)",
  color: "var(--text-muted)",
  fontSize: 11,
  textTransform: "uppercase",
  letterSpacing: 0,
};

const previewTextStyle: React.CSSProperties = {
  margin: 0,
  padding: 10,
  maxHeight: 180,
  overflow: "auto",
  color: "var(--text-primary)",
  fontSize: 12,
  lineHeight: 1.5,
  whiteSpace: "pre-wrap",
  fontFamily: "var(--font-mono)",
};

const emptyStyle: React.CSSProperties = {
  color: "var(--text-secondary)",
  border: "1px solid var(--border-subtle)",
  borderRadius: 8,
  padding: 12,
  fontSize: 13,
};

const footerStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "flex-end",
  gap: 8,
  padding: "12px 16px",
  borderTop: "1px solid var(--border-subtle)",
};

const secondaryButtonStyle: React.CSSProperties = {
  border: "1px solid var(--border-subtle)",
  borderRadius: 8,
  background: "var(--surface-base)",
  color: "var(--text-secondary)",
  padding: "8px 12px",
  fontSize: 13,
  cursor: "pointer",
};

const primaryButtonStyle: React.CSSProperties = {
  border: "1px solid var(--accent-primary)",
  borderRadius: 8,
  background: "var(--accent-primary)",
  color: "white",
  padding: "8px 12px",
  fontSize: 13,
  cursor: "pointer",
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
};
