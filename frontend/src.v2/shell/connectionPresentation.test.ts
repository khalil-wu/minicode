import { describe, expect, it } from "vitest";
import { getConnectionPresentation } from "./connectionPresentation";

describe("getConnectionPresentation", () => {
  it("distinguishes healthy, preview, connecting, and unavailable states", () => {
    expect(getConnectionPresentation({
      isConnected: true,
      isDesktop: true,
      hasRuntimeToken: true,
    })).toEqual({
      kind: "connected",
      accessibleLabel: "后端已连接",
      shortLabel: null,
      bannerMessage: null,
    });

    expect(getConnectionPresentation({
      isConnected: false,
      isDesktop: false,
      hasRuntimeToken: false,
    })).toEqual({
      kind: "preview",
      accessibleLabel: "浏览器预览模式",
      shortLabel: "预览",
      bannerMessage: "当前为浏览器预览模式，桌面功能暂不可用。",
    });

    expect(getConnectionPresentation({
      isConnected: false,
      isDesktop: true,
      hasRuntimeToken: true,
    }).kind).toBe("connecting");

    expect(getConnectionPresentation({
      isConnected: false,
      isDesktop: false,
      hasRuntimeToken: true,
    }).kind).toBe("warning");
  });

  it("renders a monotonic reconnect counter when transport supplies one", () => {
    expect(getConnectionPresentation({
      isConnected: false,
      isDesktop: true,
      hasRuntimeToken: true,
      connectionPhase: "reconnecting",
      reconnectAttempt: 1,
      reconnectMaxAttempts: 5,
    })).toEqual({
      kind: "reconnecting",
      accessibleLabel: "正在重连 1/5",
      shortLabel: "重连 1/5",
      bannerMessage: "正在重连 1/5…",
    });
  });

  it("keeps reconnect progress visible in browser preview mode", () => {
    expect(getConnectionPresentation({
      isConnected: false,
      isDesktop: false,
      hasRuntimeToken: false,
      connectionPhase: "reconnecting",
      reconnectAttempt: 4,
      reconnectMaxAttempts: 5,
    })).toEqual({
      kind: "reconnecting",
      accessibleLabel: "正在重连 4/5",
      shortLabel: "重连 4/5",
      bannerMessage: "正在重连 4/5…",
    });
  });

  it("keeps a terminal transport error visible instead of falling back to connecting", () => {
    expect(getConnectionPresentation({
      isConnected: false,
      isDesktop: true,
      hasRuntimeToken: true,
      connectionPhase: "failed",
      connectionError: "连接认证已失效，请重新登录。",
    })).toEqual({
      kind: "failed",
      accessibleLabel: "连接认证已失效，请重新登录。",
      shortLabel: "连接失败",
      bannerMessage: "连接认证已失效，请重新登录。",
    });
  });

  it("keeps a terminal transport error visible in browser preview mode", () => {
    expect(getConnectionPresentation({
      isConnected: false,
      isDesktop: false,
      hasRuntimeToken: false,
      connectionPhase: "failed",
      connectionError: "预览连接已断开。",
    })).toEqual({
      kind: "failed",
      accessibleLabel: "预览连接已断开。",
      shortLabel: "连接失败",
      bannerMessage: "预览连接已断开。",
    });
  });
});
