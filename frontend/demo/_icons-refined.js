/*
 * _icons-refined.js — 精细化图标集（B2 用）
 *
 * 与原版的差别，就是让图标「优雅」的三件事：
 *   1. 描边 1.75 → 1.5：16px 尺寸下 1.75 显得笨重，1.5 才有细节感
 *   2. 几何收敛：放大有效面积、对齐 16 网格、去掉多余节点
 *      （原版 settings 用的 lucide 齿轮有 30+ 节点，16px 下糊成一团，
 *        这里换成 sliders，语义同样是「外观设置」但形体干净）
 *   3. 圆角端点 + 统一视觉重心，避免同屏图标粗细不一
 *
 * 送出按钮改用上箭头（Codex / ChatGPT 的做法），比纸飞机更利落。
 */

const icon = (paths, size = 16) =>
  `<svg class="d-icon" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"
        aria-hidden="true">${paths}</svg>`;

export const REFINED_ICONS = {
  // 圆更大、手柄更短，16px 下比 lucide 原版清晰
  search: icon('<circle cx="10.5" cy="10.5" r="6.75"/><path d="m15.6 15.6 4.15 4.15"/>'),

  // 折角文档：折角画成实际的两笔，而不是叠一个方块
  file: icon('<path d="M14 2.75H6.5A1.75 1.75 0 0 0 4.75 4.5v15A1.75 1.75 0 0 0 6.5 21.25h11a1.75 1.75 0 0 0 1.75-1.75V8z"/><path d="M14 2.75V8h5.25"/>'),

  // 终端：提示符 + 输入行
  terminal: icon('<rect x="2.75" y="4" width="18.5" height="16" rx="2.25"/><path d="m7.5 10.25 2 1.75-2 1.75"/><path d="M12.5 14h4"/>'),

  // 单段圆弧，转起来比整圈虚线优雅
  loader: icon('<path d="M21 12a9 9 0 1 1-6.22-8.56"/>'),

  chevron: icon('<path d="m5.5 8.75 6.5 6.5 6.5-6.5"/>', 14),

  // 上箭头取代纸飞机
  send: icon('<path d="M12 19.5V5.25"/><path d="m5.75 11.5 6.25-6.25 6.25 6.25"/>'),

  // 回形针：一笔成形
  paperclip: icon('<path d="M20.5 11.5l-8.25 8.25a5.25 5.25 0 0 1-7.43-7.43l8.6-8.6a3.5 3.5 0 0 1 4.95 4.95l-8.6 8.6a1.75 1.75 0 0 1-2.48-2.48l7.78-7.78"/>'),

  // 地球：两条经线一条纬线，去掉 lucide 的双弧重叠
  globe: icon('<circle cx="12" cy="12" r="9.25"/><path d="M2.75 12h18.5"/><path d="M12 2.75c2.4 2.5 3.6 5.58 3.6 9.25s-1.2 6.75-3.6 9.25c-2.4-2.5-3.6-5.58-3.6-9.25s1.2-6.75 3.6-9.25z"/>'),

  // 外观设置：滑杆取代齿轮
  settings: icon('<path d="M4 7.5h6"/><path d="M14 7.5h6"/><path d="M4 16.5h10"/><path d="M18 16.5h2"/><circle cx="12" cy="7.5" r="2.25"/><circle cx="16" cy="16.5" r="2.25"/>'),

  // 特性开关：旗标
  flag: icon('<path d="M5 21V4.5"/><path d="M5 5.25h11.5l-1.75 3.5 1.75 3.5H5"/>'),

  // 诊断：脉冲线
  pulse: icon('<path d="M2.75 12.5h4l2-5 3 9.5 2.5-6 1.75 3h5.25"/>'),

  // 连接器：插头
  plug: icon('<path d="M9 2.75v5"/><path d="M15 2.75v5"/><path d="M6.5 7.75h11v3.5a5.5 5.5 0 0 1-11 0z"/><path d="M12 16.75v4.5"/>'),
};
