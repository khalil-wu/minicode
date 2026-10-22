import assert from "node:assert/strict";

export async function auditCustomThemeColors({ page, capture, checks }) {
  const prefix = "codex-theme-v1:";
  for (const { label, variant, contrast, expected } of [
    { label: "深色", variant: "dark", contrast: 85, expected: { info: [157, 187, 218], sidebar: [13, 13, 13], panel: [31, 31, 31] } },
    { label: "浅色", variant: "light", contrast: 30, expected: { info: [36, 104, 172], sidebar: [20, 20, 20], panel: [59, 59, 59] } },
  ]) {
    await page.locator(".settings-center-tab").filter({ hasText: "外观" }).click();
    await page.getByRole("radio", { name: label, exact: true }).click();
    await page.getByRole("button", { name: `导入${label}主题`, exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "导入主题", exact: true });
    const input = dialog.getByRole("textbox");
    const shared = JSON.parse((await input.getAttribute("placeholder")).slice(prefix.length));
    shared.theme = { ...shared.theme, accentSource: "custom", accent: "#2468ac", surface: "#101010", ink: "#d0d0d0", contrast };
    await input.fill(`${prefix}${JSON.stringify(shared)}`);
    await dialog.getByRole("button", { name: "导入主题", exact: true }).click();
    await dialog.waitFor({ state: "hidden" });
    await page.locator(".settings-center-tab").filter({ hasText: "常规" }).click();
    const link = page.locator(".settings-full-access-learn-more");
    await link.waitFor();
    const actual = await link.evaluate(element => {
      const style = getComputedStyle(element), context = document.createElement("canvas").getContext("2d");
      const color = property => {
        context.clearRect(0, 0, 1, 1);
        context.fillStyle = style.getPropertyValue(property);
        context.fillRect(0, 0, 1, 1);
        return [...context.getImageData(0, 0, 1, 1).data].slice(0, 3);
      };
      return { info: color("color"), sidebar: color("--surface-sidebar"), panel: color("--surface-panel") };
    });
    assert.deepEqual(actual, expected);
    await capture(`settings-01-${variant}-custom-contrast`, { variant, contrast, actual });
    checks.push({ scenario: "Actual local theme import follows Source custom text-info, normalized contrast and authored surface-under contrast", variant, contrast, actual });
  }
}
