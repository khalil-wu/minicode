// @vitest-environment jsdom

import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Button, IconButton } from "./Button";

describe("Button", () => {
  it("renders the default secondary variant with md size", () => {
    const { container } = render(<Button>发送</Button>);
    const button = container.querySelector("button");
    expect(button?.className).toBe("btn btn-secondary");
    expect(button?.getAttribute("type")).toBe("button");
    expect(button?.textContent).toBe("发送");
  });

  it("maps variants and the sm size to their classes", () => {
    const { container } = render(<Button variant="primary" size="sm">批准</Button>);
    expect(container.querySelector("button")?.className).toBe("btn btn-primary btn-sm");
  });

  it("keeps extra classes and click handling", () => {
    const onClick = vi.fn();
    const { container } = render(
      <Button variant="ghost" className="self-center" onClick={onClick}>更早</Button>,
    );
    const button = container.querySelector("button");
    expect(button?.className).toContain("self-center");
    fireEvent.click(button as HTMLButtonElement);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("disables interaction and shows a spinner while loading", () => {
    const onClick = vi.fn();
    const { container } = render(<Button loading onClick={onClick}>保存</Button>);
    const button = container.querySelector("button");
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(button?.getAttribute("aria-busy")).toBe("true");
    expect(container.querySelector("svg.animate-spin")).toBeTruthy();
    fireEvent.click(button as HTMLButtonElement);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("respects a disabled prop without aria-busy", () => {
    const { container } = render(<Button disabled>保存</Button>);
    const button = container.querySelector("button");
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(button?.getAttribute("aria-busy")).toBeNull();
  });
});

describe("IconButton", () => {
  it("applies the icon-button class family and an accessible label", () => {
    const { container } = render(
      <IconButton label="关闭侧边对话"><span>×</span></IconButton>,
    );
    const button = container.querySelector("button");
    expect(button?.classList.contains("mc-icon-button")).toBe(true);
    expect(button?.classList.contains("mc-icon-button-accent")).toBe(false);
    expect(button?.getAttribute("aria-label")).toBe("关闭侧边对话");
    expect(button?.getAttribute("title")).toBe("关闭侧边对话");
  });

  it("maps compact and accent/danger variants", () => {
    const { container } = render(
      <IconButton label="发送" compact variant="accent"><span>↑</span></IconButton>,
    );
    const button = container.querySelector("button");
    expect(button?.classList.contains("mc-icon-button-compact")).toBe(true);
    expect(button?.classList.contains("mc-icon-button-accent")).toBe(true);
  });
});
