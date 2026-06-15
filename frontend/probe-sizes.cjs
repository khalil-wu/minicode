const { chromium } = require("playwright");
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  await p.goto("http://localhost:5174/", { waitUntil: "networkidle" }).catch(()=>{});
  await p.waitForTimeout(800);
  const data = await p.evaluate(() => {
    const cs = (sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const s = getComputedStyle(el);
      return { fs: s.fontSize, lh: s.lineHeight, fw: s.fontWeight, text: (el.textContent||"").trim().slice(0,24) };
    };
    return {
      body: cs("body"),
      htmlFontSize: getComputedStyle(document.documentElement).fontSize,
      // sidebar items
      sidebarBtn: cs('[class*="sidebar"] button, aside button'),
    };
  });
  console.log(JSON.stringify(data, null, 2));
  await b.close();
})();
