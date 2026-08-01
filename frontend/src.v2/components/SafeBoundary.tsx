import React, { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
  name?: string;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class SafeBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(
      `[SafeBoundary:${this.props.name ?? "unknown"}]`,
      error,
      info.componentStack,
    );
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;
      return (
        <div style={{ padding: 16, color: "var(--text-danger, #e55)", fontSize: "var(--text-chrome)" }}>
          <p style={{ fontWeight: 600, marginBottom: 4 }}>
            {this.props.name ?? "Component"} encountered an error
          </p>
          <pre style={{ opacity: 0.7, fontSize: "var(--text-2xs)", whiteSpace: "pre-wrap" }}>
            {this.state.error?.message}
          </pre>
          <button
            onClick={() => this.setState({ hasError: false, error: null })}
            style={{
              marginTop: 8,
              padding: "4px 12px",
              borderRadius: 4,
              border: "1px solid var(--border-default, #333)",
              background: "var(--surface-2, #1a1a1a)",
              color: "var(--text-default, #ccc)",
              cursor: "pointer",
              fontSize: "var(--text-xxs)",
            }}
          >
            Retry
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
