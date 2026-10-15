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
    const artwork = Array.from(view.container.querySelectorAll(".mc-agent-avatar-art"), image => image.getAttribute("src"));
    const index = identity.getAttribute("data-identity-index");
    const markers = new Set<string>();

    for (const status of ["waiting", "running", "attention", "completed"] as const) {
      view.rerender(<AgentAvatar identityKey="agent-review-editor" status={status} showStatus />);
      expect(Array.from(view.container.querySelectorAll(".mc-agent-avatar-art"), image => image.getAttribute("src"))).toEqual(artwork);
      expect(identity.getAttribute("data-identity-index")).toBe(index);
      expect(identity.getAttribute("data-status")).toBe(status);
      markers.add(view.container.querySelector(".mc-agent-avatar-status")!.innerHTML);
    }
    expect(markers.size).toBe(4);
  });

  it("keeps one identity across compact summaries and expanded agent details", () => {
    const view = render(<AgentAvatar identityKey="agent-research" size="small" />);
    const avatar = view.container.querySelector(".mc-agent-avatar")!;
    const identity = avatar.getAttribute("data-identity-index");
    view.rerender(<AgentAvatar identityKey="agent-research" size="large" />);
    expect(avatar.getAttribute("data-identity-index")).toEqual(identity);
    expect(avatar.getAttribute("data-size")).toBe("large");
  });

  it("can identify a referenced task without suggesting an unknown execution state", () => {
    const { container } = render(<AgentAvatar identityKey="referenced-agent" />);
    expect(container.querySelector(".mc-agent-avatar-art")).toBeTruthy();
    expect(container.querySelector(".mc-agent-avatar-status")).toBeNull();
  });

  it("provides both original theme variants without changing the identity or adding a status marker", () => {
    const key = "agent-research";
    const { container } = render(<AgentAvatar identityKey={key} size="small" />);
    const identity = agentIdentity(key);
    expect(container.querySelector(".mc-agent-avatar-art-light")?.getAttribute("src")).toBe(identity.light);
    expect(container.querySelector(".mc-agent-avatar-art-dark")?.getAttribute("src")).toBe(identity.dark);
    expect(container.querySelectorAll(".mc-agent-avatar-art")).toHaveLength(2);
    expect(container.querySelector(".mc-agent-avatar-art")?.getAttribute("draggable")).toBe("false");
    expect(container.querySelector(".mc-agent-avatar-status")).toBeNull();
  });
});
