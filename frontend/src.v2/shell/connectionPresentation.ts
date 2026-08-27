export type ConnectionPresentationKind = "connected" | "preview" | "connecting" | "warning";

interface ConnectionPresentationInput {
  isConnected: boolean;
  isDesktop: boolean;
  hasRuntimeToken: boolean;
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
}: ConnectionPresentationInput): ConnectionPresentation => {
  if (isConnected) {
    return {
      kind: "connected",
      accessibleLabel: "后端已连接",
      shortLabel: null,
      bannerMessage: null,
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
