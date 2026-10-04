import { describe, expect, it } from "vitest";
import {
  initialUiPermissionMode,
  fromBackendPermissionMode,
  normalizeUiPermissionMode,
  toBackendPermissionMode,
} from "./permissions";

describe("permission mode protocol mapping", () => {
  it("exposes MiniCode UI permission modes", () => {
    expect(normalizeUiPermissionMode("confirm")).toBe("confirm");
    expect(normalizeUiPermissionMode("plan")).toBe("plan");
    expect(normalizeUiPermissionMode("auto")).toBe("auto");
    expect(normalizeUiPermissionMode("bypass")).toBe("bypass");
    expect(() => normalizeUiPermissionMode("ask_permissions")).toThrow("Unsupported permission mode");
  });

  it("rejects non-canonical frontend and backend modes", () => {
    expect(fromBackendPermissionMode("plan")).toBe("plan");
    expect(() => fromBackendPermissionMode("accept_edits")).toThrow("Unsupported permission mode");
    expect(() => normalizeUiPermissionMode("not-a-mode")).toThrow("Unsupported permission mode");
  });

  it("defaults fresh Windows private beta installs to Ask", () => {
    expect(initialUiPermissionMode(null)).toBe("confirm");
    expect(initialUiPermissionMode("")).toBe("confirm");
    expect(initialUiPermissionMode("bypass")).toBe("bypass");
  });

  it("sends only supported backend permission tokens from current UI modes", () => {
    expect(toBackendPermissionMode("confirm")).toBe("confirm");
    expect(toBackendPermissionMode("plan")).toBe("plan");
    expect(toBackendPermissionMode("auto")).toBe("auto");
    expect(toBackendPermissionMode("bypass")).toBe("bypass");

  });

});
