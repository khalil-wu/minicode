import type { ConnectionPhase } from "../stores/types";

export type ConnectionPresentationKind = "connected" | "preview" | "connecting" | "reconnecting" | "warning" | "failed";

interface ConnectionPresentationInput {
  isConnected: boolean;
  isDesktop: boolean;
  hasRuntimeToken: boolean;
  connectionPhase?: ConnectionPhase;
  reconnectAttempt?: number;
  reconnectMaxAttempts?: number | null;
  connectionError?: string | null;
}

export interface ConnectionPresentation {
  kind: ConnectionPresentationKind;
  accessibleLabel: string;
  shortLabel: string | null;
  bannerMessage: string | null;
}

export const getConnectionPresentation = ({
  isConnected,
  isDesktop,
  hasRuntimeToken,
  connectionPhase,
  reconnectAttempt = 0,
  reconnectMaxAttempts = null,
  connectionError,
}: ConnectionPresentationInput): ConnectionPresentation => {
  if (isConnected) {
    return {
      kind: "connected",
      accessibleLabel: "后端已连接",
      shortLabel: null,
      bannerMessage: null,
    };
  }

  // A browser preview can still have a live backend transport (the local
  // Vite proxy is enough for that). Once that transport has entered its
  // reconnect ladder, the transport state is more specific than the static
  // "preview" capability label and must remain visible to the user.
  if (connectionPhase === "reconnecting") {
    const attempt = Number.isFinite(reconnectAttempt) && reconnectAttempt > 0
      ? Math.floor(reconnectAttempt)
      : 0;
    const max = typeof reconnectMaxAttempts === "number" && reconnectMaxAttempts > 0
      ? Math.floor(reconnectMaxAttempts)
      : null;
    const counter = attempt > 0 ? `${attempt}${max ? `/${max}` : ""}` : "";
    return {
      kind: "reconnecting",
      accessibleLabel: counter ? `正在重连 ${counter}` : "正在重连",
      shortLabel: counter ? `重连 ${counter}` : "重连中",
      bannerMessage: counter ? `正在重连 ${counter}…` : "正在重连 MiniCode 服务…",
    };
  }

  if (connectionPhase === "failed") {
    const message = String(connectionError || "连接失败，请检查 MiniCode 服务或网络后重试。").trim();
    return {
      kind: "failed",
      accessibleLabel: message,
      shortLabel: "连接失败",
      bannerMessage: message,
    };
  }

  if (!isDesktop && !hasRuntimeToken) {
    return {
      kind: "preview",
      accessibleLabel: "浏览器预览模式",
      shortLabel: "预览",
      bannerMessage: "当前为浏览器预览模式，桌面功能暂不可用。",
    };
  }

  if (isDesktop) {
    return {
      kind: "connecting",
      accessibleLabel: "正在连接 MiniCode 服务",
      shortLabel: "连接中",
      bannerMessage: "正在连接 MiniCode 服务…",
    };
  }

  return {
    kind: "warning",
    accessibleLabel: "后端不可用",
    shortLabel: "未连接",
    bannerMessage: "后端不可用。请确认 MiniCode 后端正在运行。",
  };
};
