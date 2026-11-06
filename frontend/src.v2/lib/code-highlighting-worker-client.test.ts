import { afterEach, expect, it, vi } from "vitest";

vi.unmock("./code-highlighting-worker-client");
afterEach(() => { vi.unstubAllGlobals(); vi.resetModules(); });

it("routes concurrent languages and theme generations to their own callers", async () => {
  let created: any;
  vi.stubGlobal("Worker", class {
    requests: any[] = [];
    onmessage!: (event: any) => void;
    onerror!: (event: any) => void;
    constructor() { created = this; }
    postMessage(data: any) { this.requests.push(data); }
  });
  const { highlightCodeInWorker } = await import("./code-highlighting-worker-client");
  const old = highlightCodeInWorker("const old = 1", "typescript", "codex", "light");
  const current = highlightCodeInWorker("print('current')", "python", "github", "dark");
  const [a, b] = created.requests;
  expect(a.theme).toBe("codex"); expect(b.variant).toBe("dark");
  created.onmessage({ data: { requestId: b.requestId, lines: [`<span>${b.code}</span>`] } });
  expect(await current).toEqual(["<span>print('current')</span>"]);
  created.onmessage({ data: { requestId: a.requestId, error: "grammar failed" } });
  await expect(old).rejects.toThrow("grammar failed");
  const interrupted = highlightCodeInWorker("next", "text", "codex", "light");
  created.onerror({ message: "worker stopped" });
  await expect(interrupted).rejects.toThrow("worker stopped");
  await expect(highlightCodeInWorker("later", "text", "codex", "light")).rejects.toThrow("worker stopped");
});
