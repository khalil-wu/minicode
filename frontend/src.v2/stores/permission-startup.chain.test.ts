/* @vitest-environment jsdom */
import { expect, it } from "vitest";

it("opens the store with the previously persisted ask-permissions mode", async () => {
  localStorage.setItem("minicode.composer.permissionMode", "ask_permissions");
  localStorage.setItem("minicode.unrelated-draft", "Keep this draft");

  const { useAppStore } = await import("./index");

  expect(useAppStore.getState().permissionMode).toBe("confirm");
  expect(localStorage.getItem("minicode.composer.permissionMode")).toBe("confirm");
  expect(localStorage.getItem("minicode.unrelated-draft")).toBe("Keep this draft");
});
