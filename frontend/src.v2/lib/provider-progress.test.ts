import { describe, expect, it } from "vitest";
import { providerProgressLabel } from "./provider-progress";

describe("providerProgressLabel", () => {
  it("uses the reconnect ladder only while the provider is reconnecting", () => {
    expect(providerProgressLabel({
      id: "provider:connection:run:iteration",
      status: "running",
      providerState: "reconnecting",
      retryAttempt: 1,
      maxRetries: 5,
      message: "连接失败，正在重连",
    })).toBe("正在重新连接 1/5");

    expect(providerProgressLabel({
      id: "provider:connection:run:iteration",
      status: "running",
      providerState: "responding",
      retryAttempt: 1,
      maxRetries: 5,
      message: "已连接，模型正在响应",
    })).toBe("模型正在响应");
  });

  it("renders typed terminal provider states", () => {
    expect(providerProgressLabel({
      id: "provider:connection:run:iteration",
      status: "completed",
      providerState: "completed",
      retryAttempt: 2,
      maxRetries: 5,
      message: "提供商响应完成",
    })).toBe("提供商响应完成（重试 2/5）");
    expect(providerProgressLabel({
      id: "provider:connection:run:iteration",
      status: "partial",
      providerState: "interrupted",
      retryAttempt: 2,
      maxRetries: 5,
      message: "提供商请求已取消",
    })).toBe("连接中断（重试 2/5）");
  });
});
