"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { PassThrough } = require("node:stream");

const backendSidecar = require("./backend-sidecar");

function createChild(pid) {
  const child = new EventEmitter();
  child.pid = pid;
  child.exitCode = null;
  child.signalCode = null;
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.killCalls = [];
  child.kill = (signal) => {
    child.killCalls.push(signal);
    queueMicrotask(() => exitChild(child, null, signal));
    return true;
  };
  return child;
}

function exitChild(child, code = 0, signal = null) {
  child.exitCode = code;
  child.signalCode = signal;
  child.emit("exit", code, signal);
}

function initialize({ spawnProcess, sleep } = {}) {
  backendSidecar.resetStopRequested();
  backendSidecar.clearBackendRestartTimer();
  backendSidecar.init({
    writeStdout: () => {},
    writeStderr: () => {},
    getAppRoot: () => process.cwd(),
    sleep: sleep || (async () => {}),
    spawnProcess,
    config: {
      manageBackend: true,
      pythonCommand: "python",
      backendHost: "127.0.0.1",
      resolvedBackendPort: 8000,
      resolvedApiBaseUrl: "http://127.0.0.1:8000",
      restartInitialDelayMs: 60_000,
      restartMaxDelayMs: 60_000,
      restartJitterRatio: 0,
    },
  });
}

function releaseChild(child) {
  if (backendSidecar.getBackendProcess() === child) {
    exitChild(child);
  }
  backendSidecar.clearBackendRestartTimer();
}

test("backend readiness retries non-2xx responses and accepts the next 2xx", async () => {
  initialize();
  const originalFetch = global.fetch;
  let calls = 0;
  global.fetch = async () => {
    calls += 1;
    return { ok: calls >= 2 };
  };
  try {
    await backendSidecar.waitForBackendReady("http://127.0.0.1:1", 1200);
    assert.equal(calls, 2);
  } finally {
    global.fetch = originalFetch;
  }
});

test("stale child exit and error events do not clear the current owner", () => {
  const first = createChild(101);
  const second = createChild(102);
  const children = [first, second];
  initialize({ spawnProcess: () => children.shift() });

  assert.equal(backendSidecar.startBackendSidecar(), first);
  first.emit("error", new Error("first spawn failed"));
  backendSidecar.clearBackendRestartTimer();
  assert.equal(backendSidecar.getBackendProcess(), null);

  assert.equal(backendSidecar.startBackendSidecar(), second);
  first.emit("exit", 1, null);
  first.emit("error", new Error("late stale error"));

  assert.equal(backendSidecar.getBackendProcess(), second);
  assert.equal(backendSidecar.isBackendManagedByApp(), true);
  releaseChild(second);
});

test("concurrent managed launches resolve runtime and spawn only once", async () => {
  const child = createChild(201);
  let spawnCalls = 0;
  initialize({
    spawnProcess: () => {
      spawnCalls += 1;
      return child;
    },
  });

  const originalFetch = global.fetch;
  let releaseRuntime;
  let releaseFetch;
  let runtimeCalls = 0;
  global.fetch = () => new Promise((resolve) => {
    releaseFetch = resolve;
  });
  const runtimePromise = new Promise((resolve) => {
    releaseRuntime = resolve;
  });
  const resolveRuntime = () => {
    runtimeCalls += 1;
    return runtimePromise;
  };
  const runtime = {
    revision: 1,
    apiBaseUrl: "http://127.0.0.1:8000",
    wsBaseUrl: "ws://127.0.0.1:8000",
  };

  try {
    const firstLaunch = backendSidecar.launchBackendSidecar({
      resolveRuntime,
      getRuntime: () => runtime,
      timeoutMs: 1200,
    });
    const secondLaunch = backendSidecar.launchBackendSidecar({
      resolveRuntime,
      getRuntime: () => runtime,
      timeoutMs: 1200,
    });

    assert.equal(runtimeCalls, 1);
    releaseRuntime(runtime);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(spawnCalls, 1);
    releaseFetch({ ok: true });

    const [firstResult, secondResult] = await Promise.all([firstLaunch, secondLaunch]);
    assert.equal(firstResult, runtime);
    assert.equal(secondResult, runtime);
  } finally {
    global.fetch = originalFetch;
    releaseChild(child);
  }
});

test("readiness failure retires the child that failed startup", async () => {
  const child = createChild(301);
  let backendSpawnCalls = 0;
  let taskkillCalls = 0;
  initialize({
    spawnProcess: (command) => {
      if (command === "taskkill") {
        taskkillCalls += 1;
        const taskkill = new EventEmitter();
        queueMicrotask(() => {
          exitChild(child, 1, null);
          taskkill.emit("exit", 0);
        });
        return taskkill;
      }
      backendSpawnCalls += 1;
      return child;
    },
  });

  const originalFetch = global.fetch;
  global.fetch = async () => ({ ok: false });
  const runtime = {
    revision: 1,
    apiBaseUrl: "http://127.0.0.1:8000",
    wsBaseUrl: "ws://127.0.0.1:8000",
  };

  try {
    await assert.rejects(
      backendSidecar.launchBackendSidecar({
        resolveRuntime: async () => runtime,
        getRuntime: () => runtime,
        timeoutMs: 5,
      }),
      /readiness check timed out/,
    );
    assert.equal(backendSpawnCalls, 1);
    assert.equal(backendSidecar.getBackendProcess(), null);
    assert.equal(taskkillCalls + child.killCalls.length, 1);
  } finally {
    global.fetch = originalFetch;
    backendSidecar.clearBackendRestartTimer();
  }
});
