/*
 * _shell.js — renders the demo content once, so all three style demos show
 * byte-identical markup and the only difference between them is CSS.
 *
 * Class names are intentionally generic (d-*) and NOT the app's real class
 * names, so nothing here can be confused with production styling.
 */

const DEMOS = [
  { file: "demo-a-terminal.html", label: "A 终端派" },
  { file: "demo-b-hybrid.html", label: "B 混排派" },
  { file: "demo-b2-refined.html", label: "B2 优化版" },
  { file: "demo-c-refined.html", label: "C 精致派" },
  { file: "demo-motion.html", label: "动效对照" },
];

const icon = (paths, size = 16) =>
  `<svg class="d-icon" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"
        aria-hidden="true">${paths}</svg>`;

const ICONS = {
  check: icon('<path d="M20 6 9 17l-5-5"/>'),
  loader: icon('<path d="M21 12a9 9 0 1 1-6.219-8.56"/>'),
  terminal: icon('<path d="m7 11 2-2-2-2"/><path d="M11 13h4"/><rect width="18" height="18" x="3" y="3" rx="2"/>'),
  file: icon('<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v5h6"/>'),
  search: icon('<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>'),
  globe: icon('<circle cx="12" cy="12" r="10"/><path d="M12 2a15 15 0 0 1 0 20a15 15 0 0 1 0-20"/><path d="M2 12h20"/>'),
  chevron: icon('<path d="m6 9 6 6 6-6"/>', 14),
  send: icon('<path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/>'),
  paperclip: icon('<path d="M13.234 20.252 21 12.3"/><path d="m16 6-8.414 8.586a2 2 0 0 0 0 2.828 2 2 0 0 0 2.828 0l8.414-8.586a4 4 0 0 0 0-5.656 4 4 0 0 0-5.656 0l-8.415 8.585a6 6 0 1 0 8.486 8.486"/>'),
  settings: icon('<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>'),
};

const buildChatSlice = (icons) => `
<div class="d-chat">
  <div class="d-turn">
    <div class="d-user">
      <div class="d-user-bubble">帮我把设置页的字体统一到主界面，顺便看下动画为什么没有效果</div>
    </div>
  </div>

  <div class="d-turn">
    <div class="d-activity">
      <div class="d-activity-row" data-state="done">
        <span class="d-activity-icon">${icons.search}</span>
        <span class="d-activity-label">Grep <code>font-family</code></span>
        <span class="d-activity-meta">26 matches</span>
      </div>
      <div class="d-activity-row" data-state="done">
        <span class="d-activity-icon">${icons.file}</span>
        <span class="d-activity-label">Read <code>settingsShared.tsx</code></span>
        <span class="d-activity-meta">353 lines</span>
      </div>
      <div class="d-activity-row" data-state="running">
        <span class="d-activity-icon"><span class="d-spinner">${icons.loader}</span></span>
        <span class="d-activity-label">Read <code>tokens.css</code></span>
        <span class="d-activity-meta">running</span>
      </div>
    </div>

    <div class="d-tool">
      <button class="d-tool-head" type="button">
        <span class="d-tool-icon">${icons.terminal}</span>
        <span class="d-tool-name">npm run check:ui-debt</span>
        <span class="d-badge" data-tone="success">exit 0</span>
        <span class="d-tool-chev">${icons.chevron}</span>
      </button>
      <pre class="d-tool-body">importantTotal: 1034
lucideDirectImportFiles: 86
hardcodedMotionValues: 0</pre>
    </div>

    <div class="d-assistant">
      <p>找到三个原因。设置页的输入框默认继承 <code>--font-mono</code>，而主界面走 <code>--font-ui</code>，所以同一屏会混排两种字体。</p>
      <p>动画那边更直接：<code>var(--transition-fast)</code> 本身已经包含缓动函数，后面再拼 <code>-out</code> 会让整条声明失效，浏览器直接丢弃。共 13 处。</p>
      <div class="d-diff">
        <div class="d-diff-head">
          <span class="d-diff-path">src.v2/styles/reset.css</span>
          <span class="d-diff-stat"><span class="d-add">+1</span> <span class="d-del">-1</span></span>
        </div>
        <pre class="d-diff-body"><span class="d-line d-line-del">-  animation: overlay-out var(--transition-fast)-in both;</span>
<span class="d-line d-line-add">+  animation: overlay-out var(--transition-fast) both;</span></pre>
      </div>
      <p>要我按这个方向把三套设置页样式系统合并成一套吗？</p>
    </div>
  </div>
</div>

<div class="d-composer">
  <div class="d-composer-box">
    <div class="d-composer-input">按这个方向改，先修动画</div>
    <div class="d-composer-foot">
      <div class="d-composer-actions">
        <button class="d-chip" type="button">${icons.paperclip}<span>附件</span></button>
        <button class="d-chip" type="button">${icons.globe}<span>联网</span></button>
      </div>
      <div class="d-composer-right">
        <span class="d-composer-hint">18,179 / 200,000 · 9%</span>
        <button class="d-send" type="button">${icons.send}</button>
      </div>
    </div>
  </div>
</div>`;

const buildSettingsSlice = (icons) => `
<div class="d-settings">
  <aside class="d-settings-nav">
    <div class="d-settings-nav-group">
      <span class="d-settings-nav-label">常规</span>
      <button class="d-settings-tab" aria-current="page" type="button">${icons.settings}<span>外观</span></button>
      <button class="d-settings-tab" type="button">${icons.globe}<span>模型服务</span></button>
      <button class="d-settings-tab" type="button">${icons.terminal}<span>连接器</span></button>
    </div>
    <div class="d-settings-nav-group">
      <span class="d-settings-nav-label">高级</span>
      <button class="d-settings-tab" type="button">${icons.file}<span>特性开关</span></button>
      <button class="d-settings-tab" type="button">${icons.search}<span>诊断</span></button>
    </div>
  </aside>

  <div class="d-settings-content">
    <header class="d-settings-heading">
      <h2>外观</h2>
      <p>主题、字体与界面密度。这一页的字体现在应该和聊天区完全一致。</p>
    </header>

    <section class="d-card">
      <div class="d-row">
        <div class="d-row-copy">
          <div class="d-row-title">主题</div>
          <div class="d-row-desc">跟随系统或强制固定。</div>
        </div>
        <div class="d-row-control">
          <div class="d-segmented">
            <button class="d-seg" type="button">浅色</button>
            <button class="d-seg" aria-pressed="true" type="button">深色</button>
            <button class="d-seg" type="button">系统</button>
          </div>
        </div>
      </div>

      <div class="d-row">
        <div class="d-row-copy">
          <div class="d-row-title">界面字号</div>
          <div class="d-row-desc">缩放全部界面文字。修复前这个滑块没有任何效果。</div>
        </div>
        <div class="d-row-control">
          <input class="d-range" type="range" min="85" max="130" value="100" aria-label="界面字号">
          <span class="d-row-value">100%</span>
        </div>
      </div>

      <div class="d-row">
        <div class="d-row-copy">
          <div class="d-row-title">工作目录</div>
          <div class="d-row-desc">路径类字段保留等宽字体。</div>
        </div>
        <div class="d-row-control">
          <input class="d-input d-input-mono" value="C:\\Desktop\\MiniCode" aria-label="工作目录">
        </div>
      </div>

      <div class="d-row">
        <div class="d-row-copy">
          <div class="d-row-title">显示名称</div>
          <div class="d-row-desc">普通文本字段使用界面字体。</div>
        </div>
        <div class="d-row-control">
          <input class="d-input" value="MiniCode" aria-label="显示名称">
        </div>
      </div>

      <div class="d-row">
        <div class="d-row-copy">
          <div class="d-row-title">流式输出</div>
          <div class="d-row-desc">边生成边显示回复。</div>
        </div>
        <div class="d-row-control">
          <button class="d-switch" role="switch" aria-checked="true" type="button"><span></span></button>
        </div>
      </div>
    </section>

    <div class="d-actions">
      <button class="d-btn" type="button">恢复默认</button>
      <button class="d-btn d-btn-primary" type="button">保存</button>
    </div>
  </div>
</div>`;

export function renderDemo(title, note, customIcons = null) {
  const icons = customIcons || ICONS;
  const current = location.pathname.split("/").pop();
  const links = DEMOS.map(
    (d) =>
      `<a href="${d.file}"${d.file === current ? ' aria-current="page"' : ""}>${d.label}</a>`,
  ).join("");

  document.body.innerHTML = `
    <div class="demo-bar">
      <strong>${title}</strong>
      <span>${note}</span>
      <nav>${links}<button id="demo-theme" type="button">明/暗</button></nav>
    </div>
    <div class="demo-stage">
      <p class="demo-section-label">聊天界面</p>
      <div class="demo-frame">${buildChatSlice(icons)}</div>
      <p class="demo-section-label">设置页</p>
      <div class="demo-frame">${buildSettingsSlice(icons)}</div>
    </div>`;

  document.getElementById("demo-theme").addEventListener("click", () => {
    const root = document.documentElement;
    root.dataset.theme = root.dataset.theme === "light" ? "dark" : "light";
  });

  for (const head of document.querySelectorAll(".d-tool-head")) {
    head.addEventListener("click", () => {
      const tool = head.closest(".d-tool");
      tool.classList.toggle("is-collapsed");
    });
  }

  for (const seg of document.querySelectorAll(".d-seg, .d-settings-tab")) {
    seg.addEventListener("click", () => {
      const scope = seg.classList.contains("d-seg") ? ".d-seg" : ".d-settings-tab";
      const container = seg.closest(scope === ".d-seg" ? ".d-segmented" : ".d-settings-nav");
      for (const peer of container.querySelectorAll(scope)) {
        peer.removeAttribute("aria-pressed");
        peer.removeAttribute("aria-current");
      }
      if (scope === ".d-seg") seg.setAttribute("aria-pressed", "true");
      else seg.setAttribute("aria-current", "page");
    });
  }

  for (const sw of document.querySelectorAll(".d-switch")) {
    sw.addEventListener("click", () => {
      sw.setAttribute("aria-checked", sw.getAttribute("aria-checked") === "true" ? "false" : "true");
    });
  }

  const range = document.querySelector(".d-range");
  range?.addEventListener("input", () => {
    range.parentElement.querySelector(".d-row-value").textContent = `${range.value}%`;
  });
}

export { ICONS };
