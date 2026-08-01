import { chromium } from "@playwright/test";
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1240, height: 1000 }, deviceScaleFactor: 2 });
const errs = [];
p.on("pageerror", (e) => errs.push(String(e)));
p.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });
for (const n of ["a-terminal", "b-hybrid", "c-refined", "motion"]) {
  const f = n === "motion" ? "demo-motion.html" : `demo-${n}.html`;
  await p.goto(`http://localhost:4399/demo/${f}`, { waitUntil: "load" });
  for (const theme of ["dark", "light"]) {
    await p.evaluate((t) => document.documentElement.setAttribute("data-theme", t), theme);
    await p.waitForTimeout(700);
    const suffix = theme === "dark" ? "" : "-light";
    await p.screenshot({ path: `.demo-shot-${n}${suffix}.png`, fullPage: true });
    console.log(n, theme, "shot");
  }
}
console.log(errs.length ? "ERRORS:\n" + errs.join("\n") : "no console errors");
await b.close();
