import type { Page } from "@playwright/test";

export async function mockWorkspaceEnvironment(page: Page) {
  await page.route("**/api/workspace/local-environment**", route => route.fulfill({ json: {
    config_path: "", revision: "", environment: null, actions: [],
  } }));
}
