import React from "react";

interface ChunkErrorBoundaryProps {
  children: React.ReactNode;
  fallback?: React.ReactNode;
}

interface ChunkErrorBoundaryState {
  error: Error | null;
}

const isChunkLoadError = (error: Error | null): boolean => {
  const message = String(error?.message || "");
  return /Failed to fetch dynamically imported module|Loading chunk|ChunkLoadError|Importing a module script failed/i.test(message);
};

export class ChunkErrorBoundary extends React.Component<ChunkErrorBoundaryProps, ChunkErrorBoundaryState> {
  state: ChunkErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ChunkErrorBoundaryState {
    return { error };
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (this.props.fallback) return this.props.fallback;

    const chunkError = isChunkLoadError(error);
    return (
      <div style={fallbackStyle}>
        <div style={fallbackTitleStyle}>
          {chunkError ? "App assets changed" : "Panel failed to render"}
        </div>
        <div style={fallbackTextStyle}>
          {chunkError
            ? "This window is using an older frontend bundle. Reload to pick up the latest build."
            : error.message || "An unexpected UI error occurred."}
        </div>
        <button type="button" onClick={() => window.location.reload()} style={reloadButtonStyle}>
          Reload
        </button>
      </div>
    );
  }
}

export const SafeBoundary = ChunkErrorBoundary;

const fallbackStyle: React.CSSProperties = {
  display: "grid",
  alignContent: "center",
  justifyItems: "center",
  gap: 8,
  minHeight: 160,
  padding: 18,
  color: "var(--text-secondary)",
  background: "var(--surface-base)",
  fontSize: "var(--text-sm)",
  textAlign: "center",
};

const fallbackTitleStyle: React.CSSProperties = {
  color: "var(--text-primary)",
  fontWeight: 650,
};

const fallbackTextStyle: React.CSSProperties = {
  maxWidth: 360,
  color: "var(--text-muted)",
  lineHeight: 1.5,
};

const reloadButtonStyle: React.CSSProperties = {
  height: 26,
  padding: "0 10px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-page)",
  color: "var(--text-primary)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
};
