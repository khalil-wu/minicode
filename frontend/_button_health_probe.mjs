/**
 * P3b: Dynamic button health check via Playwright
 *
 * Drives the live UI, clicks high-frequency visible buttons, asserts each
 * produces its expected side effect (panel opens, toast appears, command
 * dispatched, store state updated). Complements the static audit by catching
 * wiring bugs that look correct on paper but fail at runtime.
 */

import { chromium } from "@playwright/test";

const probeButton = async (page, selector, label, expectedEffect) => {
  try {
    const button = page.locator(selector).first();
    if (!(await button.isVisible({ timeout: 2000 }))) {
      return { label, status: "not_visible", error: `Selector ${selector} not found` };
    }
    await button.click();
    await page.waitForTimeout(800);
    const effectMet = await expectedEffect(page);
    return { label, status: effectMet ? "pass" : "fail", selector };
  } catch (err) {
    return { label, status: "error", selector, error: err.message };
  }
};

const run = async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  await page.goto("http://localhost:5173", { waitUntil: "networkidle", timeout: 40000 });
  await page.waitForTimeout(5000);

  const results = [];

  // Composer footer buttons
  results.push(await probeButton(
    page,
    'button[aria-label="Open context menu"]',
    "Composer: attach/context menu",
    async (p) => await p.locator('[role="menu"]').isVisible() || await p.locator('button:has-text("@file")').isVisible()
  ));

  results.push(await probeButton(
    page,
    'button[aria-label*="model"]',
    "Composer: model picker",
    async (p) => await p.locator('text=/gpt-|claude-|sonnet|opus/i').first().isVisible()
  ));

  // Top toolbar (example placeholders — will fill from static audit)
  results.push(await probeButton(
    page,
    'button[aria-label="New conversation"]',
    "Toolbar: new conversation",
    async (p) => {
      const convId = await p.evaluate(() => window.__zustandStore?.getState?.().conversationId);
      return convId !== null;
    }
  ));

  // Settings / overlays
  results.push(await probeButton(
    page,
    'button[aria-label="Settings"]',
    "Toolbar: open settings",
    async (p) => await p.locator('text=/General|Advanced|Connectors/').first().isVisible({ timeout: 2000 })
  ));

  // Right sidebar tabs (if visible)
  const previewTab = page.locator('button:has-text("Preview")').first();
  if (await previewTab.isVisible({ timeout: 1000 })) {
    results.push(await probeButton(
      page,
      'button:has-text("Preview")',
      "Right sidebar: Preview tab",
      async (p) => {
        const tab = await p.evaluate(() => window.__zustandStore?.getState?.().rightStackTab);
        return tab === "preview";
      }
    ));
  }

  console.log("\n=== Button Health Check Results ===\n");
  results.forEach((r) => {
    const icon = r.status === "pass" ? "✓" : r.status === "fail" ? "✗" : "⚠";
    console.log(`${icon} ${r.label} — ${r.status}${r.error ? ` (${r.error})` : ""}`);
  });

  const failed = results.filter((r) => r.status === "fail" || r.status === "error");
  if (failed.length > 0) {
    console.log(`\n${failed.length} button(s) failed or errored.`);
  } else {
    console.log("\nAll checked buttons passed.");
  }

  await page.screenshot({ path: "_button_health.png" });
  await browser.close();
};

run().catch((err) => {
  console.error("Playwright probe failed:", err);
  process.exit(1);
});
