/*
 * 完整交互冒烟：每个用例从全新页面开始，覆盖首页发送、审批、步骤、审阅面板、
 * 排队/停止/重试/编辑重发、提问、计划模式、失败修复、侧边栏管理、命令面板、
 * 设置、插件、自动化与附件。页面出现任何报错都会让用例失败。
 */
import { test as base, expect, type Page } from "@playwright/test";

const URL = "/?speed=20";

const test = base.extend<{ errors: string[] }>({
  errors: [
    async ({ page }, use) => {
      const errors: string[] = [];
      page.on("pageerror", (e) => errors.push(e.message));
      page.on("console", (m) => {
        if (m.type() === "error") errors.push(m.text());
      });
      await use(errors);
      expect(errors, "页面不应出现报错").toEqual([]);
    },
    { auto: true },
  ],
});

async function shot(page: Page, name: string) {
  await page.waitForTimeout(250); // 等入场动画落定
  await page.screenshot({ path: `smoke-output/${name}.png` });
}

async function send(page: Page, text: string) {
  const box = page.getByRole("textbox", { name: "消息" });
  await box.click();
  await box.fill(text);
  await box.press("Enter");
}

const lastTurn = (page: Page) => page.locator("article.turn").last();
const approvalCard = (page: Page) => page.getByRole("alertdialog", { name: "需要你的批准" });
const toast = (page: Page, text: string) => page.getByRole("status").filter({ hasText: text });

async function approve(page: Page) {
  const card = approvalCard(page);
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: /^允许/ }).click();
  await expect(card).toBeHidden();
}

async function waitDone(page: Page) {
  await expect(lastTurn(page)).toHaveAttribute("data-status", "done");
}

test("首页发送 → 审批 → 完成 → 审阅面板", async ({ page }) => {
  await page.goto(URL);
  await expect(page.getByRole("heading", { name: /今天要构建什么/ })).toBeVisible();
  await shot(page, "01-home");

  await send(page, "优化输入框的发送体验");
  await expect(page.locator("h1.title")).toHaveText("优化输入框的发送体验");
  await expect(page.locator("[data-chat-id]").filter({ hasText: "优化输入框的发送体验" })).toBeVisible();

  const card = approvalCard(page);
  await expect(card).toContainText("npm run build");
  await shot(page, "02-approval");
  await approve(page);
  await waitDone(page);

  const turn = lastTurn(page);
  await expect(turn).toContainText("已编辑 1 个文件");
  await expect(turn).toContainText("构建通过");
  await turn.getByRole("button", { name: /已处理/ }).click();
  await expect(turn.locator(".step")).toHaveCount(5);
  await shot(page, "03-turn-done");

  await turn.getByRole("button", { name: "查看变更" }).click();
  const panel = page.getByRole("complementary", { name: "审阅面板" });
  await expect(panel.getByRole("table", { name: "差异" })).toBeVisible();
  await panel.locator(".changes-head .segmented-item").nth(1).click();
  await expect(panel.getByRole("table", { name: "并排差异" })).toBeVisible();
  await shot(page, "04-panel-split");

  // 撤销文件 → 提示里恢复
  await panel.locator(".diff-file-head").getByRole("button", { name: "撤销" }).click();
  await expect(panel).toContainText("还没有改动");
  await expect(turn).toContainText("改动已全部撤销");
  await toast(page, "已撤销 Composer.tsx").getByRole("button", { name: "恢复" }).click();
  await expect(turn).toContainText("已编辑 1 个文件");

  // 文件页：从差异跳到文件，再筛选
  await panel.locator(".diff-file-head").getByRole("button", { name: "在文件中打开" }).click();
  await expect(panel.locator(".viewer-path")).toHaveText("frontend/src/composer/Composer.tsx");
  await expect(panel.locator(".viewer-head")).toContainText("已修改");
  await panel.getByRole("button", { name: "全部文件" }).click();
  await panel.getByRole("textbox", { name: "筛选文件" }).fill("App.tsx");
  await panel.getByRole("treeitem", { name: /App\.tsx/ }).click();
  await expect(panel.locator(".viewer-path")).toHaveText("frontend/src/App.tsx");

  // 预览页
  await panel.getByRole("tab", { name: /预览/ }).click();
  await panel.getByRole("button", { name: "点赞 3" }).click();
  await expect(panel.getByRole("button", { name: "点赞 4" })).toBeVisible();

  await page.keyboard.press("Control+j");
  await expect(panel).toBeHidden();
});

test("排队、停止、重新生成、编辑重发", async ({ page }) => {
  await page.goto(URL);
  await send(page, "重构 store 的状态结构");
  await expect(approvalCard(page)).toBeVisible();

  // 回复进行中继续输入 → 进入队列
  const box = page.getByRole("textbox", { name: "消息" });
  await box.click();
  await box.fill("再解释一下为什么这样拆");
  await expect(page.getByRole("button", { name: "加入队列" })).toBeVisible();
  await box.press("Enter");
  const queue = page.locator(".queue");
  await expect(queue).toContainText("回复结束后依次发送 · 1");
  await shot(page, "05-queue");

  await approve(page);
  await expect(queue).toBeHidden();
  await expect(page.locator(".user-bubble").last()).toHaveText("再解释一下为什么这样拆");
  await waitDone(page);
  await expect(lastTurn(page)).toContainText("先明确目标");

  // 停止
  await send(page, "重构 store 的状态结构");
  await expect(approvalCard(page)).toBeVisible();
  await page.getByRole("button", { name: "停止" }).click();
  await expect(lastTurn(page)).toHaveAttribute("data-status", "stopped");
  await expect(lastTurn(page)).toContainText("已停止");
  await expect(approvalCard(page)).toHaveCount(0);

  // 重新生成会替换掉停止的那一轮
  await lastTurn(page).getByRole("button", { name: "重新生成" }).click();
  await approve(page);
  await waitDone(page);
  await expect(page.locator('article.turn[data-status="stopped"]')).toHaveCount(0);

  // 编辑最后一条消息并重发
  const lastUser = page.locator(".user").last();
  await lastUser.hover();
  await lastUser.getByRole("button", { name: "编辑消息" }).click();
  await page.getByRole("textbox", { name: "编辑消息" }).fill("解释一下 store 和 context 的区别");
  await page.locator(".user-edit").getByRole("button", { name: "发送" }).click();
  await expect(page.locator(".user-bubble").last()).toHaveText("解释一下 store 和 context 的区别");
  await waitDone(page);
  await expect(lastTurn(page)).toContainText("先明确目标");
  await shot(page, "06-edit-resend");
});

test("提问、计划模式、失败后修复", async ({ page }) => {
  await page.goto(URL);

  // 提问
  await send(page, "加载状态有两种方案，选哪个");
  const q = page.locator(".card.question");
  await expect(q).toBeVisible();
  await shot(page, "07-question");
  await q.getByRole("radio", { name: /抽出状态/ }).click();
  await q.getByRole("button", { name: "继续" }).click();
  await expect(page.locator(".answered")).toContainText("抽出状态");
  await approve(page);
  await waitDone(page);
  await expect(lastTurn(page)).toContainText("按你选的抽出状态方案");

  // 计划模式：斜杠命令开启
  const box = page.getByRole("textbox", { name: "消息" });
  await box.click();
  await box.fill("/pl");
  await expect(page.getByRole("listbox", { name: "命令" })).toBeVisible();
  await box.press("Enter");
  await expect(page.locator(".plan-chip")).toBeVisible();
  await send(page, "给首页加载失败加一个重试入口");
  const plan = page.locator(".card.plan");
  await expect(plan).toContainText("实施计划");
  await waitDone(page);
  await shot(page, "08-plan");
  await plan.getByRole("button", { name: "开始实施" }).click();
  await expect(plan).toContainText("已采纳");
  await expect(page.locator(".plan-chip")).toHaveCount(0);
  await approve(page);
  await waitDone(page);
  await expect(page.locator(".user-bubble").last()).toHaveText("按计划实施");

  // 失败 → 修复，「始终允许」让第二次 npm test 不再询问
  await page.keyboard.press("Control+n");
  await expect(page.getByRole("heading", { name: /今天要构建什么/ })).toBeVisible();
  await send(page, "测试报错了，帮我修复");
  const card = approvalCard(page);
  await expect(card).toContainText("npm test");
  await card.getByRole("button", { name: "本聊天中始终允许" }).click();
  await waitDone(page);
  const turn = lastTurn(page);
  await expect(turn).toContainText("测试 12 项全部通过");
  await turn.getByRole("button", { name: /已处理/ }).click();
  await expect(turn.locator(".step.failed")).toHaveCount(1);
  await expect(turn.locator(".terminal.failed")).toContainText("1 failed");
  await shot(page, "09-failing-fixed");
});

test("侧边栏管理、命令面板与快捷键", async ({ page }) => {
  await page.goto(URL);
  const row = page.locator('[data-chat-id="c-stream"]');

  // 双击重命名
  await row.dblclick();
  const input = page.getByRole("textbox", { name: "重命名聊天" });
  await input.fill("流式输出链路复查");
  await input.press("Enter");
  await expect(row).toContainText("流式输出链路复查");
  await expect(page.locator("h1.title")).toHaveText("流式输出链路复查");
  await shot(page, "10-existing-chat");

  // 右键置顶
  await row.click({ button: "right" });
  await page.getByRole("menuitem", { name: "置顶" }).click();
  const pinned = page.locator(".sb-section").filter({ has: page.locator(".sb-heading", { hasText: "置顶" }) });
  await expect(pinned.locator('[data-chat-id="c-stream"]')).toBeVisible();

  // 归档 → 撤销
  await row.hover();
  await row.getByRole("button", { name: "归档" }).click();
  await expect(row).toHaveCount(0);
  await toast(page, "已归档").getByRole("button", { name: "撤销" }).click();
  await expect(row).toBeVisible();

  // 删除需确认 → 撤销
  await row.click({ button: "right" });
  await page.getByRole("menuitem", { name: "删除" }).click();
  const confirm = page.getByRole("dialog").filter({ hasText: "删除这个聊天" });
  await expect(confirm).toBeVisible();
  await shot(page, "10-confirm-delete");
  await confirm.getByRole("button", { name: "删除" }).click();
  await expect(row).toHaveCount(0);
  await toast(page, "已删除").getByRole("button", { name: "撤销" }).click();
  await expect(row).toBeVisible();

  // 命令面板按消息内容搜索
  await page.keyboard.press("Control+k");
  const search = page.getByRole("textbox", { name: "搜索", exact: true });
  await search.fill("中位数");
  await expect(page.getByRole("option").first()).toContainText("整理数据集预处理脚本");
  await shot(page, "11-palette");
  await search.press("Enter");
  await expect(page.locator("h1.title")).toHaveText("整理数据集预处理脚本");

  // 快捷键面板与侧边栏折叠
  await page.keyboard.press("Control+/");
  const keys = page.getByRole("dialog", { name: "键盘快捷键" });
  await expect(keys).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(keys).toBeHidden();
  await page.keyboard.press("Control+b");
  await expect(page.getByRole("complementary", { name: "侧边栏" })).toBeHidden();
  await page.getByRole("button", { name: "展开侧边栏" }).click();
  await expect(page.getByRole("complementary", { name: "侧边栏" })).toBeVisible();
});

test("附件、权限、模型、设置、插件与自动化", async ({ page }) => {
  await page.goto(URL);

  // 附件 + 模型 + 权限
  await page.getByTestId("file-input").setInputFiles({ name: "设计稿.png", mimeType: "image/png", buffer: Buffer.from("png") });
  await expect(page.locator(".composer .attachment")).toContainText("设计稿.png");
  await page.locator(".model-btn").click();
  await page.getByRole("menuitemradio", { name: /Claude Sonnet 5/ }).click();
  await page.locator(".model-effort-row").getByRole("radio", { name: "极高" }).click();
  await expect(page.locator(".model-label")).toContainText("Sonnet 5");
  await expect(page.locator(".model-effort")).toHaveText("极高");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: /权限：/ }).click();
  await page.getByRole("menuitem", { name: /完全访问/ }).click();
  await expect(page.locator(".chip.permission")).toHaveClass(/full/);

  await send(page, "按这张设计稿调整首页");
  await expect(page.locator(".user .attachment")).toContainText("设计稿.png");
  await waitDone(page);
  await expect(approvalCard(page)).toHaveCount(0);
  await expect(lastTurn(page)).toContainText("Claude Sonnet 5");

  // 设置 → 外观
  await page.keyboard.press("Control+,");
  await expect(page.getByRole("heading", { name: "常规", level: 1 })).toBeVisible();
  const nav = page.getByRole("complementary", { name: "设置导航" });
  await nav.getByRole("button", { name: "外观" }).click();
  await page.getByRole("radio", { name: "深色", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.getByRole("radio", { name: "海蓝" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-accent", "blue");
  await page.getByRole("radio", { name: "衬线", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("data-chat-font", "serif");
  await shot(page, "12-settings-dark");

  // 模型与密钥
  await nav.getByRole("button", { name: "模型与密钥" }).click();
  const ds = page.locator(".provider").filter({ hasText: "DeepSeek" });
  await ds.getByRole("button", { name: "测试连接" }).click();
  await expect(ds).toContainText("请先填写 API 密钥");
  await ds.locator('input[type="password"]').fill("sk-test-123");
  await ds.getByRole("button", { name: "测试连接" }).click();
  await expect(ds).toContainText("连接成功");
  await expect(ds.locator(".status-pill")).toHaveText("已配置");

  // MCP
  await nav.getByRole("button", { name: "MCP 服务器" }).click();
  await page.getByRole("button", { name: "添加服务器" }).click();
  const mcp = page.getByRole("dialog", { name: "添加 MCP 服务器" });
  await mcp.getByPlaceholder("例如 github").fill("linear");
  await mcp.getByRole("radio", { name: "HTTP" }).click();
  await mcp.getByRole("button", { name: "连接" }).click();
  await expect(page.locator(".s-card")).toContainText("linear");

  // 插件
  await page.getByRole("button", { name: "返回应用" }).click();
  await page.getByRole("button", { name: "插件", exact: true }).click();
  await page.getByRole("button", { name: "安装 Figma" }).click();
  await expect(page.getByRole("button", { name: "卸载 Figma" })).toBeVisible();
  await page.getByRole("radio", { name: /已安装/ }).click();
  await expect(page.locator(".plugin-card").filter({ hasText: "Figma" })).toBeVisible();
  await shot(page, "13-plugins-dark");

  // 自动化
  await page.getByRole("button", { name: "自动化", exact: true }).click();
  await page.getByRole("button", { name: "新建自动化" }).click();
  const auto = page.getByRole("dialog", { name: "新建自动化" });
  await auto.getByPlaceholder(/每天检查依赖/).fill("每周汇总未关闭的 issue");
  await auto.getByRole("button", { name: "创建" }).click();
  await expect(page.locator(".s-card")).toContainText("每周汇总未关闭的 issue");

  // 深色主题下的聊天与审阅面板
  await page.locator('[data-chat-id="c-composer"]').click();
  await page.getByRole("button", { name: "查看变更" }).click();
  await expect(page.getByRole("complementary", { name: "审阅面板" })).toBeVisible();
  await shot(page, "14-chat-dark");
});
