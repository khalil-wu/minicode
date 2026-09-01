import { _electron as electron, expect, test } from "@playwright/test";
import { createServer } from "node:net";
import { createRequire } from "node:module";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const apiKey = process.env.MINICODE_REAL_E2E_API_KEY?.trim() ?? "";
const baseUrl = process.env.MINICODE_REAL_E2E_BASE_URL?.trim() ?? "";
const model = process.env.MINICODE_REAL_E2E_MODEL?.trim() ?? "";
const wireApi = process.env.MINICODE_REAL_E2E_WIRE_API?.trim() || "chat";
const reasoningEffort = process.env.MINICODE_REAL_E2E_REASONING_EFFORT?.trim() ?? "";
const requestedSubagents = Math.min(
  8,
  Math.max(3, Number(process.env.MINICODE_REAL_E2E_SUBAGENTS ?? "3") || 3),
);
// Codex multi-agent v2 defaults to four concurrent child threads per session.
// A single Task call may contain more work, but the remainder must stay queued.
const expectedParallelConcurrency = Math.min(4, requestedSubagents);
const requireProcessText = process.env.MINICODE_REAL_E2E_REQUIRE_PROCESS_TEXT === "1";
const requireCacheRead = process.env.MINICODE_REAL_E2E_REQUIRE_CACHE_READ === "1";

const repoRoot = path.resolve(import.meta.dirname, "../../..");
const desktopEntry = path.join(repoRoot, "desktop");
const desktopMain = path.join(desktopEntry, "main.js");
const electronExecutable = createRequire(path.join(desktopEntry, "package.json"))("electron") as string;
const frontendPort = Number(process.env.MINICODE_E2E_PORT ?? "43173");
const evidenceRoot = path.join(repoRoot, "artifacts", "real-provider-e2e");

function realProviderEnv({
  userDataDir,
  backendPort,
  frontendPort,
}: {
  userDataDir: string;
  backendPort: number;
  frontendPort: number;
}) {
  const { ELECTRON_RUN_AS_NODE: _electronRunAsNode, ...cleanEnv } = process.env;
  return {
    ...cleanEnv,
    MINICODE_FRONTEND_URL: `http://127.0.0.1:${frontendPort}/`,
    MINICODE_BACKEND_PORT: String(backendPort),
    MINICODE_USER_DATA_DIR: userDataDir,
    MINICODE_DISABLE_HARDWARE_ACCELERATION: "1",
    LLM_PROVIDER: "custom",
    CUSTOM_API_KEY: apiKey,
    CUSTOM_BASE_URL: baseUrl,
    CUSTOM_MODEL: model,
    CUSTOM_WIRE_API: wireApi,
    CUSTOM_REASONING_EFFORT: reasoningEffort,
    CUSTOM_SMALL_FAST_MODEL: "",
    CUSTOM_RESPONSES_REASONING_SUMMARY: "off",
    CUSTOM_PROMPT_CACHE_RETENTION: process.env.MINICODE_REAL_E2E_PROMPT_CACHE_RETENTION ?? "off",
    CUSTOM_MAX_TOKENS: process.env.MINICODE_REAL_E2E_MAX_TOKENS ?? "0",
    CUSTOM_THINKING_BUDGET: "0",
    OPENAI_API_KEY: "",
    OPENAI_BASE_URL: "",
    OPENAI_MODEL: "",
    OPENAI_WIRE_API: "",
    OPENAI_REASONING_EFFORT: "",
  };
}

async function launchRealProvider(userDataDir: string, backendPort: number) {
  return electron.launch({
    executablePath: electronExecutable,
    args: process.platform === "win32"
      ? [desktopMain]
      : ["--disable-gpu", "--no-sandbox", desktopMain],
    cwd: desktopEntry,
    env: realProviderEnv({ userDataDir, backendPort, frontendPort }),
  });
}

async function prepareRealWorkspace(testRoot: string, name: string) {
  const userDataDir = path.join(testRoot, "state");
  const dataDir = path.join(userDataDir, "data");
  const workspace = path.join(testRoot, name);
  await mkdir(dataDir, { recursive: true });
  await mkdir(workspace, { recursive: true });
  await writeFile(
    path.join(dataDir, "trusted_workspaces.json"),
    JSON.stringify({ version: 1, roots: [workspace] }),
    "utf8",
  );
  await writeFile(
    path.join(dataDir, "active_workspace.json"),
    JSON.stringify({ root: workspace }),
    "utf8",
  );
  return { userDataDir, dataDir, workspace };
}

async function initializeRealSession(window: Awaited<ReturnType<Awaited<ReturnType<typeof electron.launch>>["firstWindow"]>>, workspace: string) {
  await expect.poll(
    () => window.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)),
    { timeout: 60_000 },
  ).toBe(true);
  await window.evaluate(({ workspace }) => {
    const store = (window as any).__zustandStore;
    store.getState().setWorkingDirectory(workspace);
    store.getState().createConversation({
      bindWorkspace: true,
      workspaceRoot: workspace,
      appMode: "code",
    });
  }, { workspace });
  await expect.poll(
    () => window.evaluate(() => (window as any).__zustandStore?.getState().conversationId ?? ""),
    { timeout: 30_000 },
  ).not.toBe("");
}

function realComposer(window: Awaited<ReturnType<Awaited<ReturnType<typeof electron.launch>>["firstWindow"]>>) {
  return window.locator('textarea[placeholder*="任务"], textarea[placeholder*="问题"], textarea[placeholder*="指令"]').first();
}

async function availablePort(): Promise<number> {
  const server = createServer();
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close((error) => error ? reject(error) : resolve(port));
    });
  });
}

async function approveExpectedRealTaskRequests(
  window: Awaited<ReturnType<Awaited<ReturnType<typeof electron.launch>>["firstWindow"]>>,
): Promise<number> {
  let approved = 0;
  for (let attempt = 0; attempt < 1_200; attempt += 1) {
    const fileChange = window.getByRole("button", { name: "允许文件更改" }).first();
    if (await fileChange.isVisible().catch(() => false)) {
      await fileChange.click();
      approved += 1;
      continue;
    }
    const toolUse = window.getByRole("button", { name: "允许使用工具" }).first();
    if (await toolUse.isVisible().catch(() => false)) {
      await toolUse.click();
      approved += 1;
      continue;
    }
    if (await window.evaluate(() => !Boolean((window as any).__zustandStore?.getState().isStreaming))) {
      break;
    }
    await window.waitForTimeout(250);
  }
  return approved;
}

test.describe("real provider desktop multi-agent path", () => {
  test.skip(
    !apiKey || !baseUrl || !model,
    "Set MINICODE_REAL_E2E_API_KEY, MINICODE_REAL_E2E_BASE_URL, and MINICODE_REAL_E2E_MODEL to run this opt-in test.",
  );

  test("streams model process text and 3-8 real subagents through Electron", async () => {
    test.setTimeout(Number(process.env.MINICODE_REAL_E2E_TIMEOUT_MS ?? "900000"));
    const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-real-provider-e2e-"));
    const userDataDir = path.join(testRoot, "state");
    const dataDir = path.join(userDataDir, "data");
    const workspace = path.join(testRoot, "workspace");
    await mkdir(dataDir, { recursive: true });
    await mkdir(workspace, { recursive: true });
    for (let index = 1; index <= requestedSubagents; index += 1) {
      await writeFile(
        path.join(workspace, `fact-${index}.txt`),
        `FACT_${index}=value-${index * 17}\n`,
        "utf8",
      );
    }
    // Exercise the same persisted workspace approval/restoration boundary as
    // a folder previously chosen through Electron's native picker. Without
    // this ledger the backend can still execute the conversation, but the
    // desktop file tree correctly rejects the renderer-only path as untrusted,
    // which would make the visual evidence less representative of a real run.
    await writeFile(
      path.join(dataDir, "trusted_workspaces.json"),
      JSON.stringify({ version: 1, roots: [workspace] }),
      "utf8",
    );
    await writeFile(
      path.join(dataDir, "active_workspace.json"),
      JSON.stringify({ root: workspace }),
      "utf8",
    );

    const backendPort = await availablePort();
    const app = await electron.launch({
      executablePath: electronExecutable,
      args: [desktopMain],
      cwd: desktopEntry,
      env: realProviderEnv({ userDataDir, backendPort, frontendPort }),
    });

    try {
      const window = await app.firstWindow();
      await expect.poll(
        () => window.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)),
        { timeout: 60_000 },
      ).toBe(true);

      await window.evaluate(({ workspace }) => {
        const store = (window as any).__zustandStore;
        store.getState().setWorkingDirectory(workspace);
        store.getState().createConversation({
          bindWorkspace: true,
          workspaceRoot: workspace,
          appMode: "code",
        });
        (window as any).__realProviderEvidence = {
          peakRunningSubagents: 0,
          samples: [],
          collaborationDomSamples: [],
          streamingTransitions: [{ value: Boolean(store.getState().isStreaming), at: Date.now() }],
        };
        const sampleCollaborationDom = () => {
          const evidence = (window as any).__realProviderEvidence;
          const cells = Array.from(document.querySelectorAll<HTMLElement>(".collaboration-cell"))
            .map((element) => ({
              status: element.dataset.status ?? "",
              text: element.textContent?.trim() ?? "",
            }));
          const sample = {
            at: Date.now(),
            streaming: Boolean(store.getState().isStreaming),
            cells,
          };
          const previous = evidence.collaborationDomSamples.at(-1);
          if (JSON.stringify(previous?.cells) !== JSON.stringify(cells)
            || previous?.streaming !== sample.streaming) {
            evidence.collaborationDomSamples.push(sample);
          }
        };
        const collaborationObserver = new MutationObserver(sampleCollaborationDom);
        collaborationObserver.observe(document.body, {
          subtree: true,
          childList: true,
          attributes: true,
          attributeFilter: ["data-status"],
        });
        (window as any).__realProviderCollaborationObserver = collaborationObserver;
        sampleCollaborationDom();
        store.subscribe((state: any) => {
          const subagents = Array.isArray(state.subagents) ? state.subagents : [];
          const running = subagents.filter((item: any) => item.status === "running").length;
          const evidence = (window as any).__realProviderEvidence;
          evidence.peakRunningSubagents = Math.max(evidence.peakRunningSubagents, running);
          const sample = subagents.map((item: any) => ({
            id: item.id,
            role: item.role,
            status: item.status,
            objective: item.objective,
            resultAvailable: item.resultAvailable,
          }));
          if (JSON.stringify(sample) !== JSON.stringify(evidence.samples.at(-1))) {
            evidence.samples.push(sample);
          }
          const streaming = Boolean(state.isStreaming);
          if (evidence.streamingTransitions.at(-1)?.value !== streaming) {
            evidence.streamingTransitions.push({ value: streaming, at: Date.now() });
          }
          sampleCollaborationDom();
        });
      }, { workspace });

      await expect.poll(
        () => window.evaluate(() => (window as any).__zustandStore?.getState().conversationId ?? ""),
        { timeout: 30_000 },
      ).not.toBe("");

      const assignments = Array.from({ length: requestedSubagents }, (_, offset) => {
        const index = offset + 1;
        return `${index}. one subagent reads only fact-${index}.txt and returns its exact FACT_${index} line`;
      }).join("\n");
      const prompt = [
        `Use exactly ${requestedSubagents} subagents in one parallel task call.`,
        "Before that tool call, emit one short public progress sentence describing the delegation.",
        "Do not read the files in the parent agent and do not replace delegation with direct tools.",
        assignments,
        "Wait for every result, then reply with all exact FACT lines and the final marker REAL_PROVIDER_E2E_OK.",
      ].join("\n");

      // The composer follows the v2 UI copy (and may use the slash-command
      // placeholder while focused).  Anchor on the semantic textarea rather
      // than the removed legacy "随心输入" string so the real-provider path
      // exercises the same production composer as the regular Electron E2E.
      const composer = window.locator('textarea[placeholder*="任务"], textarea[placeholder*="问题"], textarea[placeholder*="指令"]').first();
      await expect(composer).toBeVisible({ timeout: 30_000 });
      await composer.fill(prompt);
      await composer.press("Enter");

      await expect.poll(
        () => window.evaluate(() => (window as any).__zustandStore?.getState().isStreaming),
        { timeout: 30_000 },
      ).toBe(true);

      // The current Codex-shaped surface keeps the plan pill scoped to the
      // canonical update_plan snapshot. Collaboration is rendered in the
      // agent work area and the context card instead of being synthesized into
      // that plan surface. Verify the real collaboration event is visible and
      // the delegated prompt is not copied into the composer.
      await expect.poll(
        () => window.evaluate((count) => {
          const samples = (window as any).__realProviderEvidence?.collaborationDomSamples ?? [];
          return samples.some((sample: any) => sample.streaming === true
            && sample.cells.some((cell: any) => (
              /^(running|success)$/.test(cell.status)
              && cell.text.includes(`${count} 个智能体`)
            )));
        }, requestedSubagents),
        { timeout: 120_000 },
      ).toBe(true);
      await expect(composer).toHaveValue("");
      const liveTaskPreviewText = await window.evaluate(() => {
        const samples = (window as any).__realProviderEvidence?.collaborationDomSamples ?? [];
        return samples.flatMap((sample: any) => sample.cells).at(-1)?.text ?? "";
      });
      expect(liveTaskPreviewText).not.toContain("Return only that exact line");
      await window.evaluate((previewText) => {
        (window as any).__realProviderEvidence.composerTaskPreview = previewText;
      }, liveTaskPreviewText);

      await expect.poll(
        () => window.evaluate(() => (window as any).__zustandStore?.getState().isStreaming),
        { timeout: Number(process.env.MINICODE_REAL_E2E_TIMEOUT_MS ?? "900000") - 30_000 },
      ).toBe(false);

      // Codex replays a parent-owned child through the ordinary chat renderer.
      // Exercise that production path after completion so durable replay,
      // default expansion, and the direct-input fence are all real-provider evidence.
      await window.getByRole("button", { name: "打开右侧栏" }).click();
      await window.getByRole("button", { name: "添加面板" }).click();
      await window.getByRole("navigation", { name: "面板选择" })
        .getByRole("button", { name: "子智能体" })
        .click();
      const firstChildRow = window.locator(".subagents-row").first();
      await expect(firstChildRow).toBeVisible({ timeout: 30_000 });
      await firstChildRow.click();
      const childDetail = window.locator(".subagents-detail");
      await expect(childDetail).toBeVisible();
      const childTranscript = childDetail.locator('[aria-label="子智能体工作记录"]');
      await expect(childTranscript).toBeVisible({ timeout: 30_000 });
      await expect(childTranscript.locator(".chat-turn").first()).toBeVisible();
      await expect(childDetail.locator("textarea")).toHaveCount(0);
      await expect(childDetail.locator("details")).toHaveCount(0);
      const childProcess = childDetail.locator('.chat-turn-process[data-collapsed="false"]').first();
      await expect(childProcess).toBeVisible();
      const childReplayEvidence = await childDetail.evaluate((element) => ({
        text: element.textContent?.trim() ?? "",
        chatTurnCount: element.querySelectorAll(".chat-turn").length,
        expandedProcessCount: element.querySelectorAll('.chat-turn-process[data-collapsed="false"]').length,
        textareaCount: element.querySelectorAll("textarea").length,
        detailsCount: element.querySelectorAll("details").length,
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
        bodyFitsViewport: (() => {
          const body = element.querySelector<HTMLElement>(".subagents-detail-body");
          return Boolean(body && body.scrollWidth <= body.clientWidth + 1);
        })(),
        overflowing: Array.from(element.querySelectorAll<HTMLElement>("*"))
          .filter((node) => node.scrollWidth > node.clientWidth + 1)
          .slice(0, 8)
          .map((node) => ({
            tag: node.tagName,
            className: node.className,
            clientWidth: node.clientWidth,
            scrollWidth: node.scrollWidth,
          })),
        fitsViewport: element.scrollWidth <= element.clientWidth + 1,
      }));

      // Completed Agent turns collapse their work trace by default. Expand the
      // real production surface before checking rendered process narration.
      const collapsedProcessToggles = window.locator('button[aria-label="展开处理步骤"]');
      for (let index = 0, count = await collapsedProcessToggles.count(); index < count; index += 1) {
        // A responsive right drawer may intentionally cover the conversation.
        // Trigger the same button handler in-page so evidence collection does
        // not depend on an unrelated drawer being open or closed.
        await collapsedProcessToggles.nth(index).evaluate((button: HTMLButtonElement) => button.click());
      }
      const collapsedCollaborationToggles = window.locator('.collaboration-cell-summary[aria-expanded="false"]');
      for (let index = 0, count = await collapsedCollaborationToggles.count(); index < count; index += 1) {
        await collapsedCollaborationToggles.nth(index).evaluate((button: HTMLButtonElement) => button.click());
      }
      const completedCollaboration = window.locator('.collaboration-cell[data-status="success"]').first();
      await expect(completedCollaboration).toBeVisible();
      await expect(completedCollaboration.locator(".collaboration-cell-summary"))
        .toContainText(`已发送消息 ${requestedSubagents} 个智能体`);

      const evidence = await window.evaluate(() => {
        const state = (window as any).__zustandStore.getState();
        const messages = state.messages.map((message: any) => ({
          id: message.id,
          role: message.role,
          content: message.content,
          blocks: message.blocks,
          completionStatus: message.completionStatus,
        }));
        const publicProcessSources = new Set(["commentary", "model_preamble", "post_tool", "runtime"]);
        const processText = messages
          .flatMap((message: any) => Array.isArray(message.blocks) ? message.blocks : [])
          .filter((block: any) => (
            block.type === "process"
            || block.type === "process_text"
            || ((block.type === "text" || block.type === "thinking") && publicProcessSources.has(String(block.source ?? "")))
          ))
          .map((block: any) => String(block.content ?? block.text ?? block.summary ?? ""))
          .filter(Boolean);
        const renderedProcessText = Array.from(document.querySelectorAll(".agent-loop-process-note, .thinking-cell-process"))
          .map((element) => element.textContent?.trim() ?? "")
          .filter(Boolean);
        const assistantText = messages
          .filter((message: any) => message.role === "assistant")
          .flatMap((message: any) => {
            const blockText = (Array.isArray(message.blocks) ? message.blocks : [])
              .filter((block: any) => block.type === "text")
              .map((block: any) => String(block.content ?? ""))
              .filter(Boolean);
            return blockText.length ? blockText : [String(message.content ?? "")].filter(Boolean);
          })
          .join("\n");
        return {
          model: state.currentModel,
          messages,
          assistantText,
          processText: renderedProcessText.length ? renderedProcessText : processText,
          toolCalls: messages
            .flatMap((message: any) => Array.isArray(message.blocks) ? message.blocks : [])
            .filter((block: any) => block.type === "tool_call")
            .map((block: any) => block.record),
          subagents: state.subagents,
          runtime: (window as any).__realProviderEvidence,
          bodyText: document.body.innerText,
          renderedToolCardCount: document.querySelectorAll('[data-testid^="tool-call-"]').length,
          renderedToolActivityCount: document.querySelectorAll(".activity-cell").length,
          renderedSubagentSummaryCount: Array.from(document.querySelectorAll(
            ".collaboration-cell, .inline-task-collaboration-preview-row, .subagents-row",
          )).filter((element) => /subagent|subtask|智能体|子任务/i.test(element.textContent ?? "")).length,
          collaborationCells: Array.from(document.querySelectorAll<HTMLElement>(".collaboration-cell"))
            .map((element) => ({
              action: element.dataset.action ?? "",
              summary: element.querySelector(".collaboration-cell-summary")?.textContent?.trim() ?? "",
              details: Array.from(element.querySelectorAll(".collaboration-cell-detail"))
                .map((detail) => detail.textContent?.trim() ?? "")
                .filter(Boolean),
              fitsViewport: element.scrollWidth <= element.clientWidth + 1,
            })),
          errorToasts: Array.from(document.querySelectorAll('.toast-card[data-type="error"]'))
            .map((element) => element.textContent?.trim() ?? "")
            .filter(Boolean),
        };
      });

      const terminalSubagents = evidence.subagents.filter((item: any) =>
        ["done", "partial", "cancelled", "error"].includes(item.status),
      );
      const successfulSubagents = evidence.subagents.filter((item: any) => item.status === "done");
      const cacheReadInputTokens = evidence.messages
        .flatMap((message: any) => Array.isArray(message.blocks) ? message.blocks : [])
        .map((block: any) => Number(block.providerRaw?.usage?.cache_read_input_tokens ?? 0))
        .filter((value: number) => Number.isFinite(value) && value > 0)
        .reduce((total: number, value: number) => total + value, 0);
      const evidencePayload = {
        capturedAt: new Date().toISOString(),
        baseHost: new URL(baseUrl).host,
        model,
        wireApi,
        requestedSubagents,
        modelProcessTextProduced: evidence.processText.length > 0,
        cacheReadInputTokens,
        assistantText: evidence.assistantText,
        processText: evidence.processText,
        messages: evidence.messages,
        toolCalls: evidence.toolCalls,
        subagents: evidence.subagents,
        runtime: evidence.runtime,
        composerTaskPreview: evidence.runtime.composerTaskPreview ?? "",
        renderedToolCardCount: evidence.renderedToolCardCount,
        renderedToolActivityCount: evidence.renderedToolActivityCount,
        renderedSubagentSummaryCount: evidence.renderedSubagentSummaryCount,
        collaborationCells: evidence.collaborationCells,
        childReplay: childReplayEvidence,
        errorToasts: evidence.errorToasts,
      };

      // A secret leak must fail before any evidence artifact is persisted.
      expect(evidence.bodyText).not.toContain(apiKey);
      expect(JSON.stringify(evidencePayload)).not.toContain(apiKey);
      await mkdir(evidenceRoot, { recursive: true });
      await writeFile(
        path.join(evidenceRoot, "last-run.json"),
        JSON.stringify(evidencePayload, null, 2),
        "utf8",
      );
      await window.screenshot({
        path: path.join(evidenceRoot, "last-run.png"),
        fullPage: true,
      });

      expect(evidence.subagents.length).toBeGreaterThanOrEqual(requestedSubagents);
      expect(terminalSubagents.length).toBeGreaterThanOrEqual(requestedSubagents);
      expect(successfulSubagents.length).toBeGreaterThanOrEqual(requestedSubagents);
      expect(evidence.runtime.peakRunningSubagents).toBeGreaterThanOrEqual(expectedParallelConcurrency);
      expect(evidence.runtime.peakRunningSubagents).toBeLessThanOrEqual(4);
      if (requestedSubagents > expectedParallelConcurrency) {
        const replenishedWorkerSlot = evidence.runtime.samples.some((sample: any[]) => {
          const running = sample.filter((item: any) => item.status === "running").length;
          const done = sample.filter((item: any) => item.status === "done").length;
          return done > 0 && running === expectedParallelConcurrency;
        });
        expect(replenishedWorkerSlot).toBe(true);
      }
      expect(evidence.runtime.streamingTransitions.some((item: any) => item.value === true)).toBe(true);
      expect(evidence.runtime.streamingTransitions.at(-1)?.value).toBe(false);
      if (requireProcessText) expect(evidence.processText.length).toBeGreaterThan(0);
      if (requireCacheRead) expect(cacheReadInputTokens).toBeGreaterThan(0);
      expect(evidence.toolCalls.some((record: any) => record?.name === "task" && record?.status === "success")).toBe(true);
      const sentMessageCells = evidence.collaborationCells.filter((cell: any) => cell.action === "sent_message");
      expect(sentMessageCells).toHaveLength(1);
      expect(sentMessageCells[0].summary).toContain(`已发送消息 ${requestedSubagents} 个智能体`);
      expect(sentMessageCells[0].details).toHaveLength(requestedSubagents);
      expect(sentMessageCells[0].fitsViewport).toBe(true);
      for (let index = 1; index <= requestedSubagents; index += 1) {
        expect(sentMessageCells[0].details.join("\n")).toContain(`fact-${index}.txt`);
      }
      expect(evidence.errorToasts).toEqual([]);
      expect(childReplayEvidence.chatTurnCount).toBeGreaterThan(0);
      expect(childReplayEvidence.expandedProcessCount).toBeGreaterThan(0);
      expect(childReplayEvidence.textareaCount).toBe(0);
      expect(childReplayEvidence.detailsCount).toBe(0);
      expect(childReplayEvidence.fitsViewport).toBe(true);
      expect(childReplayEvidence.bodyFitsViewport).toBe(true);
      for (let index = 1; index <= requestedSubagents; index += 1) {
        expect(evidence.assistantText).toContain(`FACT_${index}=value-${index * 17}`);
      }
      expect(evidence.assistantText).toContain("REAL_PROVIDER_E2E_OK");
    } finally {
      await app.close().catch(() => undefined);
      await rm(testRoot, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
    }
  });

  test("real provider completes a multi-file coding fix and keeps the diff in the process trace", async () => {
    test.setTimeout(Number(process.env.MINICODE_REAL_E2E_CODING_TIMEOUT_MS ?? "600000"));
    const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-real-coding-e2e-"));
    const { userDataDir, workspace } = await prepareRealWorkspace(testRoot, "workspace");
    const sourcePath = path.join(workspace, "src", "calculator.mjs");
    const testPath = path.join(workspace, "test", "calculator.test.mjs");
    await mkdir(path.dirname(sourcePath), { recursive: true });
    await mkdir(path.dirname(testPath), { recursive: true });
    await writeFile(
      sourcePath,
      [
        "export function total(values) {",
        "  return values.reduce((sum, value) => sum - value, 0);",
        "}",
        "",
      ].join("\n"),
      "utf8",
    );
    await writeFile(
      testPath,
      [
        "import assert from 'node:assert/strict';",
        "import test from 'node:test';",
        "import { total } from '../src/calculator.mjs';",
        "",
        "test('totals all values', () => {",
        "  assert.equal(total([2, 3, 1]), 6);",
        "});",
        "",
        "// The coding task must replace this placeholder with a regression test.",
        "// TODO: cover an empty list.",
        "",
      ].join("\n"),
      "utf8",
    );
    const app = await launchRealProvider(userDataDir, await availablePort());

    try {
      const window = await app.firstWindow();
      await initializeRealSession(window, workspace);
      const composer = realComposer(window);
      await expect(composer).toBeVisible({ timeout: 30_000 });
      await composer.fill([
        "Solve this coding task in the current workspace.",
        "First inspect src/calculator.mjs and test/calculator.test.mjs and run the existing test to reproduce the failure.",
        "Fix the total() implementation so it sums values correctly.",
        "Replace the TODO placeholder in test/calculator.test.mjs with a real regression test for an empty list.",
        "You must modify both src/calculator.mjs and test/calculator.test.mjs, and no other files.",
        "Run exactly: node --test test/calculator.test.mjs",
        "Do not stop after explaining; make the edits and verify the test passes. Report the command and its result in the final answer.",
      ].join("\n"));
      await composer.press("Enter");
      await expect.poll(
        () => window.evaluate(() => Boolean((window as any).__zustandStore?.getState().isStreaming)),
        { timeout: 30_000 },
      ).toBe(true);
      const approvedRequests = await approveExpectedRealTaskRequests(window);
      await expect.poll(
        () => window.evaluate(() => Boolean((window as any).__zustandStore?.getState().isStreaming)),
        { timeout: 120_000 },
      ).toBe(false);
      // Terminal completion can move the turn from the live tail into history
      // on the next render. Sample only after that transition has settled so a
      // transient live-only diff card cannot satisfy this test.
      await window.waitForTimeout(1_500);

      const source = await readFile(sourcePath, "utf8");
      const testSource = await readFile(testPath, "utf8");
      const workArea = window.locator('[data-zone="work"]');
      if (await workArea.getAttribute("data-collapsed") === "true") {
        await workArea.getByRole("button", { name: "展开处理步骤" }).click();
      }
      await expect(workArea.locator(".diff-cell")).toHaveCount(1);
      const commandResult = await window.evaluate(() => {
        const state = (window as any).__zustandStore?.getState();
        const messages = Array.isArray(state?.messages) ? state.messages : [];
        const records = messages
          .flatMap((message: any) => Array.isArray(message.blocks) ? message.blocks : [])
          .filter((block: any) => block.type === "tool_call")
          .map((block: any) => block.record)
          .filter(Boolean);
        return {
          assistantText: messages
            .filter((message: any) => message.role === "assistant")
            .map((message: any) => String(message.content ?? ""))
            .join("\\n"),
          records,
          isStreaming: Boolean(state?.isStreaming),
          bodyText: document.body.innerText,
          zones: Array.from(document.querySelectorAll<HTMLElement>('.chat-turn'))
            .slice(-1)
            .flatMap((turn) => Array.from(turn.children)
              .map((child) => child.getAttribute("data-zone"))
              .filter(Boolean)),
          processCount: document.querySelectorAll('.agent-loop-process').length,
          answerCount: document.querySelectorAll('.chat-turn-answer-zone').length,
          outcomeCount: document.querySelectorAll('[data-zone="outcome"]').length,
          processDiffCount: document.querySelectorAll('[data-zone="work"] .diff-cell').length,
          processEditActivityCount: document.querySelectorAll('[data-zone="work"] [data-activity-kind="fileChange"]').length,
          readActivityTexts: Array.from(document.querySelectorAll<HTMLElement>('[data-zone="work"] [data-activity-kind="fileRead"]'))
            .map((element) => element.innerText),
          errorToasts: Array.from(document.querySelectorAll('.toast-card[data-type="error"]'))
            .map((element) => element.textContent?.trim() ?? "")
            .filter(Boolean),
        };
      });

      const commandRecords = commandResult.records.filter((record: any) => record?.name === "run_command");
      const successfulCommand = commandRecords.find((record: any) => record?.status === "success");
      const evidence = {
        capturedAt: new Date().toISOString(),
        baseHost: new URL(baseUrl).host,
        model,
        wireApi,
        approvedRequests,
        files: {
          sourcePath,
          testPath,
          source,
          testSource,
        },
        command: successfulCommand,
        assistantText: commandResult.assistantText,
        records: commandResult.records,
        isStreaming: commandResult.isStreaming,
        zones: commandResult.zones,
        processCount: commandResult.processCount,
        answerCount: commandResult.answerCount,
        outcomeCount: commandResult.outcomeCount,
        processDiffCount: commandResult.processDiffCount,
        processEditActivityCount: commandResult.processEditActivityCount,
        readActivityTexts: commandResult.readActivityTexts,
        errorToasts: commandResult.errorToasts,
      };
      expect(JSON.stringify(evidence)).not.toContain(apiKey);
      await mkdir(evidenceRoot, { recursive: true });
      await writeFile(path.join(evidenceRoot, "coding-task.json"), JSON.stringify(evidence, null, 2), "utf8");
      await window.getByLabel("Agent 回复").last().scrollIntoViewIfNeeded();
      await window.screenshot({ path: path.join(evidenceRoot, "coding-task.png"), fullPage: false });

      expect(source).toMatch(/reduce\(\(sum, value\) => sum \+ value/);
      expect(testSource).toMatch(/empty list|empty array|\[\], 0/i);
      expect(successfulCommand).toBeTruthy();
      expect(String(successfulCommand?.arguments?.command ?? successfulCommand?.args?.command ?? ""))
        .toContain("node --test test/calculator.test.mjs");
      expect(commandResult.isStreaming).toBe(false);
      expect(commandResult.zones).toEqual(["work", "reply"]);
      expect(commandResult.processCount).toBe(1);
      expect(commandResult.answerCount).toBe(1);
      expect(commandResult.outcomeCount).toBe(0);
      expect(commandResult.processDiffCount).toBe(1);
      expect(commandResult.processEditActivityCount).toBe(0);
      expect(commandResult.readActivityTexts.some((text: string) =>
        text.includes("src/calculator.mjs") && text.includes("test/calculator.test.mjs"),
      )).toBe(true);
      expect(commandResult.errorToasts).toEqual([]);
      expect(commandResult.assistantText).toMatch(/pass|修复|test/i);
    } finally {
      await app.close().catch(() => undefined);
      await rm(testRoot, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
    }
  });

  test("real provider permission request can be rejected without writing the workspace", async () => {
    test.setTimeout(Number(process.env.MINICODE_REAL_E2E_APPROVAL_TIMEOUT_MS ?? "300000"));
    const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-real-approval-e2e-"));
    const { userDataDir, workspace } = await prepareRealWorkspace(testRoot, "workspace");
    const markerPath = path.join(workspace, "approval-must-not-exist.txt");
    const app = await launchRealProvider(userDataDir, await availablePort());

    try {
      const window = await app.firstWindow();
      await initializeRealSession(window, workspace);
      const composer = realComposer(window);
      await expect(composer).toBeVisible({ timeout: 30_000 });
      await composer.fill([
        "Use the write_file tool exactly once.",
        `Create the file ${markerPath} with the text APPROVAL_DENIED_MARKER.`,
        "Do not use any other tool and do not answer until you have requested that tool.",
      ].join("\n"));
      await composer.press("Enter");

      // Filesystem mutations use the production Diff Review path rather than
      // the generic tool-approval card. Wait for the actual review action so
      // this test verifies the same boundary a desktop user sees.
      const rejectFileChange = window.getByRole("button", { name: "拒绝文件更改" });
      await expect(rejectFileChange).toBeVisible({ timeout: 120_000 });
      await rejectFileChange.click();
      await expect.poll(
        () => window.evaluate(() => (window as any).__zustandStore?.getState().pendingDiffReview ?? null),
        { timeout: 30_000 },
      ).toBeNull();
      await expect.poll(
        () => window.evaluate(() => Boolean((window as any).__zustandStore?.getState().isStreaming)),
        { timeout: 120_000 },
      ).toBe(false);

      expect(existsSync(markerPath)).toBe(false);
      const state = await window.evaluate(() => {
        const store = (window as any).__zustandStore?.getState();
        const writeFileCalls = (store?.messages ?? [])
          .flatMap((message: any) => Array.isArray(message.blocks) ? message.blocks : [])
          .filter((block: any) => block.type === "tool_call" && block.record?.name === "write_file")
          .map((block: any) => ({
            status: block.record.status,
            developerDetail: block.record.developerDetail,
            errorKind: block.record.errorKind,
            transition: block.record.transition,
          }));
        return {
          writeFileCalls,
          errorToasts: Array.from(document.querySelectorAll('.toast-card[data-type="error"]'))
            .map((element) => element.textContent?.trim() ?? "")
            .filter(Boolean),
        };
      });
      expect(state.errorToasts).toEqual([]);
      expect(state.writeFileCalls).toHaveLength(1);
      expect(state.writeFileCalls[0].status).not.toBe("success");
      expect(state.writeFileCalls[0].developerDetail).toMatch(/rejected|denied/i);
    } finally {
      await app.close().catch(() => undefined);
      await rm(testRoot, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
    }
  });

  test("real provider streaming can be interrupted from the desktop composer", async () => {
    test.setTimeout(Number(process.env.MINICODE_REAL_E2E_CANCEL_TIMEOUT_MS ?? "300000"));
    const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-real-cancel-e2e-"));
    const { userDataDir, workspace } = await prepareRealWorkspace(testRoot, "workspace");
    const app = await launchRealProvider(userDataDir, await availablePort());

    try {
      const window = await app.firstWindow();
      await initializeRealSession(window, workspace);
      const composer = realComposer(window);
      await expect(composer).toBeVisible({ timeout: 30_000 });
      await composer.fill([
        "Do not use tools.",
        "Begin streaming a very long technical essay (at least 8000 words) about desktop application reliability.",
        "Keep generating continuously until the user stops you; do not summarize or stop early.",
      ].join("\n"));
      await composer.press("Enter");
      await expect.poll(
        () => window.evaluate(() => Boolean((window as any).__zustandStore?.getState().isStreaming)),
        { timeout: 30_000 },
      ).toBe(true);
      // Wait until the real provider has produced assistant content. An
      // interrupt sent during the short pre-assistant race can legitimately
      // leave only the user message, which does not exercise partial-turn
      // persistence.
      await expect.poll(
        () => window.evaluate(() => {
          const messages = (window as any).__zustandStore?.getState().messages ?? [];
          return messages.some((message: any) => {
            if (message.role !== "assistant") return false;
            const projectedText = (Array.isArray(message.blocks) ? message.blocks : [])
              .filter((block: any) => block.type === "text")
              .map((block: any) => String(block.content ?? block.text ?? ""))
              .join("");
            return `${String(message.content ?? "")}${projectedText}`.length > 0;
          });
        }),
        { timeout: 60_000 },
      ).toBe(true);
      // A real provider can finish before the Stop click lands. Record whether
      // the turn was still live at click time: only then does the run actually
      // exercise interruption, and only then is a terminal "completed" wrong.
      const wasStreamingAtClick = await window.evaluate(
        () => Boolean((window as any).__zustandStore?.getState().isStreaming),
      );
      await window.getByRole("button", { name: "停止当前回复" }).last().click();
      await expect.poll(
        () => window.evaluate(() => Boolean((window as any).__zustandStore?.getState().isStreaming)),
        { timeout: 120_000 },
      ).toBe(false);

      const state = await window.evaluate(() => {
        const messages = (window as any).__zustandStore?.getState().messages ?? [];
        const assistant = [...messages].reverse().find((message: any) => message.role === "assistant");
        const projectedText = (Array.isArray(assistant?.blocks) ? assistant.blocks : [])
          .filter((block: any) => block.type === "text")
          .map((block: any) => String(block.content ?? block.text ?? ""))
          .join("");
        return {
          terminalStatus: assistant?.terminalStatus,
          contentLength: `${String(assistant?.content ?? "")}${projectedText}`.length,
          errorToasts: Array.from(document.querySelectorAll('.toast-card[data-type="error"]'))
            .map((element) => element.textContent?.trim() ?? "")
            .filter(Boolean),
        };
      });
      test.skip(
        !wasStreamingAtClick,
        "provider finished the turn before Stop could be clicked; interruption was not exercised",
      );
      expect(["interrupted", "partial"]).toContain(state.terminalStatus);
      expect(state.contentLength).toBeGreaterThan(0);
      expect(state.errorToasts).toEqual([]);

      await composer.fill("Reply with exactly CANCEL_RECOVERY_E2E_OK and no tool calls.");
      await composer.press("Enter");
      await expect.poll(
        () => window.evaluate(() => Boolean((window as any).__zustandStore?.getState().isStreaming)),
        { timeout: 30_000 },
      ).toBe(true);
      await expect.poll(
        () => window.evaluate(() => (window as any).__zustandStore?.getState().messages
          ?.some((message: any) => String(message.content ?? "").includes("CANCEL_RECOVERY_E2E_OK"))),
        { timeout: 120_000 },
      ).toBe(true);
      await expect.poll(
        () => window.evaluate(() => Boolean((window as any).__zustandStore?.getState().isStreaming)),
        { timeout: 120_000 },
      ).toBe(false);
      const recovery = await window.evaluate(() => {
        const messages = (window as any).__zustandStore?.getState().messages ?? [];
        const assistantMessages = messages.filter((message: any) => message.role === "assistant");
        return {
          latestTerminalStatus: assistantMessages.at(-1)?.terminalStatus,
          markerCount: assistantMessages.filter((message: any) =>
            String(message.content ?? "").includes("CANCEL_RECOVERY_E2E_OK")
          ).length,
          isStreaming: Boolean((window as any).__zustandStore?.getState().isStreaming),
        };
      });
      expect(recovery.latestTerminalStatus).toBe("completed");
      expect(recovery.markerCount).toBe(1);
      expect(recovery.isStreaming).toBe(false);
    } finally {
      await app.close().catch(() => undefined);
      await rm(testRoot, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
    }
  });

  test("real provider completed conversation survives a desktop restart", async () => {
    test.setTimeout(Number(process.env.MINICODE_REAL_E2E_RESTART_TIMEOUT_MS ?? "360000"));
    const testRoot = await mkdtemp(path.join(tmpdir(), "minicode-real-restart-e2e-"));
    const { userDataDir, workspace } = await prepareRealWorkspace(testRoot, "workspace");
    const app = await launchRealProvider(userDataDir, await availablePort());

    try {
      const window = await app.firstWindow();
      await initializeRealSession(window, workspace);
      const composer = realComposer(window);
      await expect(composer).toBeVisible({ timeout: 30_000 });
      await composer.fill("Reply with exactly RESTART_REAL_E2E_OK and no tool calls.");
      await composer.press("Enter");
      await expect.poll(
        () => window.evaluate(() => Boolean((window as any).__zustandStore?.getState().isStreaming)),
        { timeout: 30_000 },
      ).toBe(true);
      await expect.poll(
        () => window.evaluate(() => (window as any).__zustandStore?.getState().messages
          ?.some((message: any) => String(message.content ?? "").includes("RESTART_REAL_E2E_OK"))),
        { timeout: 120_000 },
      ).toBe(true);
      await expect.poll(
        () => window.evaluate(() => Boolean((window as any).__zustandStore?.getState().isStreaming)),
        { timeout: 120_000 },
      ).toBe(false);
      const conversationId = await window.evaluate(() => (window as any).__zustandStore?.getState().conversationId ?? "");
      await app.close();

      const restarted = await launchRealProvider(userDataDir, await availablePort());
      try {
        const restartedWindow = await restarted.firstWindow();
        await expect.poll(
          () => restartedWindow.evaluate(() => Boolean((window as any).__zustandStore?.getState().isConnected)),
          { timeout: 60_000 },
        ).toBe(true);
        await expect.poll(
          () => restartedWindow.evaluate(({ conversationId }) => {
            const state = (window as any).__zustandStore?.getState();
            return state?.conversationId === conversationId
              && state.messages?.some((message: any) => String(message.content ?? "").includes("RESTART_REAL_E2E_OK"));
          }, { conversationId }),
          { timeout: 120_000 },
        ).toBe(true);
        await expect(
          restartedWindow.getByText("RESTART_REAL_E2E_OK", { exact: true }).last(),
        ).toBeVisible({ timeout: 30_000 });
        const restored = await restartedWindow.evaluate(() => {
          const state = (window as any).__zustandStore?.getState();
          return {
            conversationId: state?.conversationId ?? "",
            messageCount: state?.messages?.length ?? 0,
            bodyText: document.body.innerText,
          };
        });
        expect(restored.conversationId).toBe(conversationId);
        expect(restored.messageCount).toBeGreaterThan(0);
        expect(restored.bodyText).toContain("RESTART_REAL_E2E_OK");
      } finally {
        await restarted.close().catch(() => undefined);
      }
    } finally {
      await app.close().catch(() => undefined);
      await rm(testRoot, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
    }
  });
});
