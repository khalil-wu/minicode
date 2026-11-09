import { defineConfig, devices } from "@playwright/test";

// 冒烟测试：启动本地 dev server，用 ?speed=20 加速模拟 agent，截图存到 smoke-output/
export default defineConfig({
  testDir: "tests",
  outputDir: "test-results",
  timeout: 60_000,
  expect: { timeout: 8_000 },
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:5199",
    locale: "zh-CN",
    colorScheme: "light",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      // Windows 自带 Edge，免去下载 Playwright 浏览器；其他系统可改成 channel: "chrome"
      use: { ...devices["Desktop Edge"], channel: "msedge", viewport: { width: 1440, height: 900 } },
    },
  ],
  webServer: {
    command: "npx vite --port 5199 --strictPort",
    url: "http://localhost:5199",
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
