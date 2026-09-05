import { expect, Page, test } from "@playwright/test";

const workspacePath = "C:\\Desktop\\MiniCode";
const readmePath = `${workspacePath}\\README.md`;

async function mockDesktopWorkbench(page: Page) {
  await page.addInitScript(({ workspacePath, readmePath }) => {
    localStorage.clear();
    const sentMessages: string[] = [];

    (window as any).__MINICODE_RUNTIME__ = {
      apiBaseUrl: "",
      wsBaseUrl: "",
      runtimeToken: "test-token",
      desktop: {
        platformInfo: { isDesktop: true, platform: "win32", arch: "x64" },
        windowControls: {
          minimize: async () => undefined,
          maximize: async () => undefined,
          close: async () => undefined,
        },
        notify: async () => undefined,
        onDeepLink: () => () => undefined,
        pickDirectory: async () => workspacePath,
        pickWorkspaceDirectory: async () => workspacePath,
        trustWorkspace: async (path: string) => path,
        openExternal: async () => undefined,
        revealPath: async () => undefined,
        diagnostics: { export: async () => ({ ok: true }) },
        fs: {
          listTree: async (path: string) => ({
            workspaceRoot: workspacePath,
            requestedPath: path,
            entries: path === workspacePath
              ? [
                  { name: "README.md", path: readmePath, isDirectory: false, sizeBytes: 128 },
                  { name: "frontend", path: `${workspacePath}\\frontend`, isDirectory: true },
                ]
              : [],
          }),
          searchFiles: async () => [{ name: "README.md", path: readmePath, kind: "file", score: 1 }],
          searchFilesByKind: async () => [{ name: "README.md", path: readmePath, kind: "file", score: 1 }],
          readFile: async () => ({
            path: readmePath,
            content: "# MiniCode\n\nLocal AI coding workbench.\n",
            contentHash: "readme-hash",
            sizeBytes: 40,
          }),
          writeFile: async () => undefined,
          compareWriteFile: async (_path: string, _hash: string, content: string) => ({
            path: readmePath,
            content,
            contentHash: "readme-hash-next",
          }),
          createDirectory: async () => undefined,
          renamePath: async () => undefined,
          deletePath: async () => undefined,
        },
        pty: {
          spawn: async () => ({ sessionId: "pty-1", shell: "powershell", cwd: workspacePath }),
          write: async () => undefined,
          resize: async () => undefined,
          kill: async () => undefined,
          list: async () => [],
          onData: () => undefined,
          onExit: () => undefined,
        },
        env: {
          detect: async () => ({ git: true, python: true, node: true, docker: false, ollama: false, home: "C:\\Users\\ago" }),
        },
        browser: {
          discover: async () => ({ status: "connected", endpoint: "", browser: "Chrome", protocolVersion: "1", userAgent: "", webSocketDebuggerUrl: "", targets: [] }),
          captureScreenshot: async () => ({ endpoint: "", targetId: "", title: "", url: "", mimeType: "image/png", data: "", capturedAt: Date.now() }),
          navigate: async () => undefined,
          click: async () => undefined,
          type: async () => undefined,
        },
        embeddedBrowser: {
          create: async ({ id, url }: { id: string; url: string }) => ({ id, url, title: "", loading: false }),
          list: async () => [],
          activate: async () => true,
          setBounds: async () => true,
          navigate: async ({ id, url }: { id: string; url: string }) => ({ id, url, title: "", loading: false }),
          runAction: async () => true,
          inspect: async () => ({ entries: [] }),
          getSettings: async () => ({ downloadPolicy: "ask", permissions: {} }),
          setSettings: async () => ({ downloadPolicy: "ask", permissions: {} }),
          clearSiteData: async () => true,
          close: async () => true,
          onEvent: () => () => undefined,
        },
      },
    };

    class MockWebSocket extends EventTarget {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSING = 2;
      static CLOSED = 3;
      CONNECTING = 0;
      OPEN = 1;
      CLOSING = 2;
      CLOSED = 3;

      readyState = 1;
      url: string;
      protocol = "";
      extensions = "";
      bufferedAmount = 0;
      binaryType: BinaryType = "blob";
      onopen: ((ev: Event) => void) | null = null;
      onclose: ((ev: CloseEvent) => void) | null = null;
      onmessage: ((ev: MessageEvent) => void) | null = null;
      onerror: ((ev: Event) => void) | null = null;

      constructor(url: string) {
        super();
        this.url = url;
        (window as any).__mockWs = this;
        (window as any).__mockWsMessages = sentMessages;

        setTimeout(() => {
          const ev = new Event("open");
          this.onopen?.(ev);
          this.dispatchEvent(ev);
          this._receive({
            type: "conversation.list",
            conversations: [
              {
                id: "conv-workbench",
                title: "Workbench",
                updated_at: "2026-06-15T00:00:00.000Z",
              },
            ],
            active_conversation_id: "conv-workbench",
            active_conversation: {
              id: "conv-workbench",
              title: "Workbench",
              updated_at: "2026-06-15T00:00:00.000Z",
              messages: [],
            },
          });
          this._receive({
            type: "llm.model.updated",
            model: "gpt-5",
            current_model: "gpt-5",
            available_models: ["gpt-5"],
          });
        }, 20);
      }

      send(data: string) {
        sentMessages.push(data);
        const parsed = JSON.parse(data);
        if (parsed.client_command_id) {
          setTimeout(() => {
            this._receive({
              type: "client.command.ack",
              client_command_id: parsed.client_command_id,
              command_type: parsed.type,
            });
          }, 0);
        }
        if (parsed.type === "conversation.truncate") {
          setTimeout(() => {
            this._receive({
              type: "command.result",
              command: "conversation.truncate",
              level: "success",
              message: "Conversation truncated",
              data: { client_command_id: parsed.client_command_id },
            });
          }, 0);
          return;
        }
        if (parsed.type === "workspace.set") {
          setTimeout(() => {
            this._receive({
              type: "workspace.imported",
              project: { root_path: parsed.path, name: "MiniCode" },
            });
            this._receive({
              type: "conversation.switched",
              conversation_id: "conv-workbench",
              conversation: {
                id: "conv-workbench",
                title: "Workbench",
                updated_at: "2026-06-15T00:00:00.000Z",
                workspace_root: parsed.path,
                worktree_path: "",
                git_isolated: false,
                messages: [],
              },
              is_hydrating: false,
            });
          }, 10);
          return;
        }
        if (parsed.type !== "user_message") return;
        const owner = {
          conversation_id: parsed.conversation_id || "conv-workbench",
          message_id: parsed.assistant_message_id,
        };

        setTimeout(() => {
          this._receive({ type: "thinking_delta", content: "Inspecting README before editing.", ...owner });
          this._receive({
            type: "tool_call",
            id: "read-readme",
            name: "read_file",
            args: { path: "README.md" },
            input_summary: "README.md",
            started_at: Date.now(),
            ...owner,
          });
        }, 20);
        setTimeout(() => {
          this._receive({
            type: "tool_result",
            id: "read-readme",
            summary: "Read README.md",
            display_summary: "Read README.md",
            result_kind: "file",
            evidence_type: "file",
            extraction_status: "ok",
            activity_kind: "fileRead",
            ...owner,
          });
          this._receive({
            type: "tool_call",
            id: "edit-readme",
            name: "edit_file",
            args: { path: "README.md" },
            input_summary: "README.md",
            started_at: Date.now(),
            ...owner,
          });
        }, 60);
        setTimeout(() => {
          this._receive({
            type: "tool_result",
            id: "edit-readme",
            summary: "Modified README.md",
            display_summary: "Modified README.md",
            result_kind: "edit",
            evidence_type: "file",
            extraction_status: "ok",
            activity_kind: "fileChange",
            diff: {
              plus: 1,
              minus: 0,
              patch: "@@ -1,2 +1,3 @@\n # MiniCode\n+Private beta ready\n",
            },
            ...owner,
          });
          this._receive({
            type: "item.started",
            item: { id: "workbench-answer", type: "agent_message" },
            ...owner,
          });
          this._receive({
            type: "agent_message.delta",
            item_id: "workbench-answer",
            delta: "Done. I updated README.md in the selected workspace.",
            ...owner,
          });
          this._receive({
            type: "item.completed",
            item: {
              id: "workbench-answer",
              type: "agent_message",
              text: "Done. I updated README.md in the selected workspace.",
              status: "completed",
            },
            ...owner,
          });
          this._receive({
            type: "done",
            status: "completed",
            usage: {
              input_tokens: 12,
              output_tokens: 10,
              cache_creation_input_tokens: 0,
              cache_read_input_tokens: 0,
              input_includes_cache_read: false,
            },
            ...owner,
          });
        }, 100);
      }

      close() {
        this.readyState = 3;
      }

      _receive(data: object) {
        const ev = new MessageEvent("message", { data: JSON.stringify(data) });
        this.onmessage?.(ev);
        this.dispatchEvent(ev);
      }
    }

    (window as any).WebSocket = MockWebSocket;
  }, { workspacePath, readmePath });

  await page.route("**/api/workspace/git/worktree?**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        current_path: workspacePath,
        current_branch: "main",
        is_worktree: false,
        worktree_count: 0,
        worktrees: [],
      }),
    });
  });
  await page.route("**/api/**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true }),
    });
  });
}

async function openApp(page: Page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => Boolean((window as any).__mockWs));
  await expect.poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().isConnected)).toBe(true);
  await expect.poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().currentModel || "")).toBe("gpt-5");
}

const sentPayloads = (page: Page) =>
  page.evaluate(() => ((window as any).__mockWsMessages ?? []).map((raw: string) => JSON.parse(raw)));

test.describe("Workbench workspace and chat continuity", () => {
  test.beforeEach(async ({ page }) => {
    await mockDesktopWorkbench(page);
    await openApp(page);
  });

  test("Open folder binds the active conversation before the next agent turn", async ({ page }) => {
    await page.getByRole("button", { name: "打开左侧栏" }).click();
    await page.getByRole("button", { name: "打开项目" }).click();

    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().workingDirectory)).toBe(workspacePath);
    await expect.poll(() => page.evaluate(() => (window as any).__zustandStore?.getState().appMode)).toBe("code");
    await page.getByRole("button", { name: "打开左侧栏" }).click();
    await expect(page.getByRole("tree", { name: "文件资源管理器" })).toBeVisible();

    const composer = page.locator('textarea, input[placeholder*="message" i], [contenteditable="true"]').first();
    await composer.fill("Update the README for private beta.");
    await composer.press("Enter");

    await expect(page.getByRole("region", { name: "助手回复" })).toContainText(
      /Done\. I updated .*README\.md in the selected workspace\./,
      { timeout: 5000 },
    );

    const payloads = await sentPayloads(page);
    expect(payloads).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: "workspace.set", path: workspacePath }),
    ]));

    const userMessage = payloads.find((payload: any) => payload.type === "user_message" && payload.content === "Update the README for private beta.");
    expect(userMessage).toMatchObject({
      conversation_id: "conv-workbench",
      workspace_root: workspacePath,
      permission_mode: "confirm",
    });
  });

  test("Cowork home keeps the composer readable across desktop and mobile", async ({ page }) => {
    await page.setViewportSize({ width: 2048, height: 1100 });
    await page.getByRole("tab", { name: "协作", exact: true }).click();

    const home = page.locator(".chat-pane-main");
    const composer = page.locator(".composer-container");
    await expect(home).toBeVisible();
    await expect(composer).toBeVisible();

    const [homeBox, composerBox] = await Promise.all([
      home.boundingBox(),
      composer.boundingBox(),
    ]);

    expect(homeBox).not.toBeNull();
    expect(composerBox).not.toBeNull();
    const configuredMaxWidth = await composer.evaluate((element) => {
      const value = getComputedStyle(element).getPropertyValue("--input-max-width");
      return Number.parseFloat(value) || 960;
    });
    expect(composerBox!.width).toBeLessThanOrEqual(configuredMaxWidth);
    expect(Math.abs(
      (composerBox!.x + composerBox!.width / 2) - (homeBox!.x + homeBox!.width / 2),
    )).toBeLessThanOrEqual(2);

    await page.setViewportSize({ width: 390, height: 844 });
    await expect(home).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const mobileComposerBox = await composer.boundingBox();
    expect(mobileComposerBox).not.toBeNull();
    expect(mobileComposerBox!.width).toBeLessThanOrEqual(390 - 24);
  });

  test("closing the last code tab returns to chat instead of blanking the conversation UI", async ({ page }) => {
    await page.evaluate(({ workspacePath, readmePath }) => {
      const store = (window as any).__zustandStore;
      store.setState({
        appMode: "code",
        workingDirectory: workspacePath,
        panelSlots: [
          { id: "main-chat", kind: "chat", label: "Chat", size: 1, focused: false, maximized: false },
          { id: "editor-test", kind: "editor", label: "README.md", size: 1, focused: true, maximized: false },
        ],
        editorTabs: [
          {
            path: "README.md",
            content: "# MiniCode\n",
            original: "# MiniCode\n",
            contentHash: "readme-hash",
            loading: false,
            error: null,
            largeFile: false,
            loadWarning: null,
          },
        ],
        activeTabPath: "README.md",
        activeEditorPath: "README.md",
        messages: [
          {
            id: "user-before-editor",
            role: "user",
            content: "Keep the chat visible after closing code.",
            artifacts: [],
            timestamp: Date.now(),
          },
        ],
        isStreaming: false,
      });
    }, { workspacePath, readmePath });

    const closeTab = page.getByRole("button", { name: "关闭 README.md", exact: true });
    await expect(closeTab).toBeVisible({ timeout: 5000 });
    await closeTab.click();

    await expect.poll(() =>
      page.evaluate(() => (window as any).__zustandStore?.getState().panelSlots.map((slot: any) => slot.kind)),
    ).toEqual(["chat", "editor"]);
    await expect.poll(() => page.evaluate(() => {
      const state = (window as any).__zustandStore?.getState();
      return {
        active: state.panelSlots.find((slot: any) => slot.focused)?.kind,
        tabs: state.editorTabs.length,
      };
    })).toEqual({ active: "chat", tabs: 0 });
    await expect(page.getByText("Keep the chat visible after closing code.")).toBeVisible();
    await expect(page.locator('textarea, input[placeholder*="message" i], [contenteditable="true"]').first()).toBeVisible();
  });
});
