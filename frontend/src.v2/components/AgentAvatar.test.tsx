// @vitest-environment jsdom

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AgentAvatar } from "./AgentAvatar";
import { agentIdentity } from "../lib/agent-identity";

describe("AgentAvatar identity", () => {
  afterEach(cleanup);

  it("keeps the same artwork and color through every execution state", () => {
    const view = render(<AgentAvatar identityKey="agent-review-editor" status="running" showStatus />);
    const identity = view.container.querySelector(".mc-agent-avatar")!;
    const artwork = view.container.querySelector(".mc-agent-avatar-art")!.innerHTML;
    const color = identity.getAttribute("data-identity-color");
    const markers = new Set<string>();

    for (const status of ["waiting", "running", "attention", "completed"] as const) {
      view.rerender(<AgentAvatar identityKey="agent-review-editor" status={status} showStatus />);
      expect(view.container.querySelector(".mc-agent-avatar-art")!.innerHTML).toBe(artwork);
      expect(identity.getAttribute("data-identity-color")).toBe(color);
      expect(identity.getAttribute("data-status")).toBe(status);
      markers.add(view.container.querySelector(".mc-agent-avatar-status")!.innerHTML);
    }
    expect(markers.size).toBe(4);
  });

  it("keeps one identity across compact summaries and expanded agent details", () => {
    const view = render(<AgentAvatar identityKey="agent-research" size="small" />);
    const avatar = view.container.querySelector(".mc-agent-avatar")!;
    const identity = [avatar.getAttribute("data-glyph"), avatar.getAttribute("data-identity-color")];
    view.rerender(<AgentAvatar identityKey="agent-research" size="large" />);
    expect([avatar.getAttribute("data-glyph"), avatar.getAttribute("data-identity-color")]).toEqual(identity);
    expect(avatar.getAttribute("data-size")).toBe("large");
  });

  it("can identify a referenced task without suggesting an unknown execution state", () => {
    const { container } = render(<AgentAvatar identityKey="referenced-agent" />);
    expect(container.querySelector(".mc-agent-avatar-art")).toBeTruthy();
    expect(container.querySelector(".mc-agent-avatar-status")).toBeNull();
  });

  it("uses the reference flower's four petals and four outer dots without a status marker", () => {
    const key = Array.from({ length: 64 }, (_, index) => `reference-${index}`)
      .find((candidate) => agentIdentity(candidate).glyph === "petal")!;
    const { container } = render(<AgentAvatar identityKey={key} size="small" />);
    const artwork = container.querySelector(".mc-agent-avatar-art")!;
    expect(artwork.querySelectorAll("path")).toHaveLength(4);
    expect(artwork.querySelectorAll("circle[transform]")).toHaveLength(4);
    expect(container.querySelector(".mc-agent-avatar-status")).toBeNull();
  });
});
