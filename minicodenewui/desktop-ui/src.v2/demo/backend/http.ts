// HTTP side of the demo backend: answers the renderer's REST calls from
// in-memory fixtures. Unknown endpoints get an empty JSON object and are
// listed on `window.__demoUnhandled` so missing routes are easy to spot.

import { DEMO_MODELS, DEMO_WORKSPACE, DEMO_WORKSPACE_NAME, state, type Json } from "./state";

type Route = (url: URL, init: RequestInit | undefined) => unknown | Promise<unknown>;

const unhandled = new Set<string>();
(window as unknown as { __demoUnhandled: Set<string> }).__demoUnhandled = unhandled;

const draftSelection = (): Json => ({
  provider: "anthropic",
  provider_id: "anthropic",
  current_model: state.model,
  model: state.model,
  available_models: DEMO_MODELS.map((entry) => entry.model),
  available_model_options: DEMO_MODELS.map((entry) => ({
    model: entry.model, display_name: entry.label,
    reasoning_effort_levels: [...entry.efforts], default_reasoning_effort: entry.effort,
  })),
  models_source: "configured",
  effective_reasoning_effort: state.effort,
  model_selection_mode: "default",
  default_model_selection: { provider: "anthropic", model: state.model, reasoning_effort: state.effort, conversation_id: "", working_directory: DEMO_WORKSPACE },
  available_permission_modes: ["confirm", "auto", "bypass"],
  feature_flags: {},
  composer_commands: [],
  config_version: "demo-config",
  config_cwd: DEMO_WORKSPACE,
});

const routes: Array<[RegExp, Route]> = [
  [/^\/health$/, () => ({ status: "ok", version: "demo" })],
  [/^\/api\/health$/, () => ({ status: "ok", version: "demo" })],
  [/^\/api\/llm\/draft-selection$/, () => draftSelection()],
  [/^\/api\/thread-metadata\/sections$/, () => ({ sections: [] })],
  [/^\/api\/workspace\/local-environment$/, () => ({ actions: [] })],
  [/^\/api\/projects$/, () => ({
    projects: [{ id: "proj_aurora", name: DEMO_WORKSPACE_NAME, path: DEMO_WORKSPACE, root_path: DEMO_WORKSPACE }],
  })],
];

const respond = (body: unknown, status = 200): Response =>
  new Response(body === undefined ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

export function installDemoFetch(): void {
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const href = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(href, location.href);
    const isBackend = url.origin === location.origin
      ? url.pathname.startsWith("/api/") || url.pathname === "/health"
      : /^(127\.0\.0\.1|localhost)$/.test(url.hostname) && url.port !== location.port;
    if (!isBackend) return nativeFetch(input, init);
    await new Promise((resolve) => setTimeout(resolve, 30 + Math.random() * 60));
    for (const [pattern, route] of routes) {
      if (pattern.test(url.pathname)) return respond(await route(url, init));
    }
    const key = `${(init?.method ?? "GET").toUpperCase()} ${url.pathname}`;
    if (!unhandled.has(key)) {
      unhandled.add(key);
      console.info("[demo] unhandled", key);
    }
    return respond({});
  };
}
