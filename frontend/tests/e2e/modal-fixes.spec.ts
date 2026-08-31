import { expect, test } from "@playwright/test";

test.describe("Modal routing and layering", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page.locator("textarea").first()).toBeVisible();
  });

  test("opening Settings closes the Command palette", async ({ page }) => {
    await page.keyboard.press("Control+K");
    const commandPalette = page.getByRole("dialog", { name: "命令面板" });
    await expect(commandPalette).toBeVisible();

    await page.keyboard.press("Control+,");

    await expect(page.getByRole("main", { name: "设置" })).toBeVisible();
    await expect(commandPalette).toHaveCount(0);
    await expect(page.locator(".header-bar")).toHaveCount(0);
  });

  test("only one modal overlay is mounted", async ({ page }) => {
    await page.keyboard.press("Control+K");

    await expect(page.locator(".overlay-backdrop")).toHaveCount(1);
    await expect(page.getByRole("dialog", { name: "命令面板" })).toBeVisible();
  });

  test("Escape closes the current modal", async ({ page }) => {
    await page.keyboard.press("Control+K");
    const commandPalette = page.getByRole("dialog", { name: "命令面板" });
    await expect(commandPalette).toBeVisible();

    await page.keyboard.press("Escape");

    await expect(commandPalette).toHaveCount(0);
  });

  test("the command palette layer sits above app chrome", async ({ page }) => {
    await page.keyboard.press("Control+K");

    const overlay = page.locator(".overlay-backdrop");
    const header = page.locator(".header-bar");
    const [overlayZ, headerZ] = await Promise.all([
      overlay.evaluate((element) => Number.parseInt(getComputedStyle(element).zIndex || "0", 10)),
      header.evaluate((element) => Number.parseInt(getComputedStyle(element).zIndex || "0", 10)),
    ]);

    expect(overlayZ).toBeGreaterThan(headerZ);
    await expect(page.getByRole("dialog", { name: "命令面板" })).toBeVisible();
  });
});

test.describe("Shell resilience", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/", { waitUntil: "domcontentloaded" });
  });

  test("an unrelated custom error event does not blank the workbench", async ({ page }) => {
    await page.evaluate(() => {
      window.dispatchEvent(new CustomEvent("composer-error", { detail: new Error("Test error") }));
    });

    await expect(page.locator("textarea").first()).toBeVisible();
  });

  test("an invalid URL event does not crash the workbench", async ({ page }) => {
    await page.evaluate(() => {
      window.dispatchEvent(new CustomEvent("test-invalid-url", {
        detail: { url: "not-a-valid-url://test" },
      }));
    });

    await expect(page.locator("body")).toBeVisible();
    await expect(page.locator("textarea").first()).toBeVisible();
  });
});
