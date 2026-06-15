const { chromium } = require("playwright");
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  await p.goto("http://localhost:5174/", { waitUntil: "networkidle" });
  await p.waitForTimeout(800);
  const r = await p.evaluate(() => {
    const body = getComputedStyle(document.body);
    const btn = document.querySelector("aside button");
    const bs = btn ? getComputedStyle(btn) : null;
    return {
      bodyFontSize: body.fontSize,
      bodyLineHeight: body.lineHeight,
      sidebarBtnFontSize: bs ? bs.fontSize : null,
    };
  });
  console.log(JSON.stringify(r, null, 2));
  await b.close();
})();
