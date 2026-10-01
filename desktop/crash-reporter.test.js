"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const crashReporting = require("./crash-reporter");

test("crash endpoint requires HTTPS and rejects credentials", () => {
  assert.equal(
    crashReporting.validatedCrashSubmitUrl("https://crashes.example.com/intake"),
    "https://crashes.example.com/intake",
  );
  assert.throws(() => crashReporting.validatedCrashSubmitUrl("http://crashes.example.com"), /must use HTTPS/);
  assert.throws(() => crashReporting.validatedCrashSubmitUrl("https://user:pass@crashes.example.com"), /cannot contain credentials/);
});

test("crash reporter starts local collection without an upload endpoint", () => {
  const previous = process.env.MINICODE_CRASH_REPORT_URL;
  delete process.env.MINICODE_CRASH_REPORT_URL;
  let options;
  try {
    const result = crashReporting.init({
      crashReporter: { start: (value) => { options = value; } },
      app: { getVersion: () => "1.2.3" },
    });
    assert.equal(result.enabled, true);
    assert.equal(result.uploading, false);
    assert.equal(options.uploadToServer, false);
    assert.equal(Object.hasOwn(options, "submitURL"), false);
    assert.equal(options.extra.version, "1.2.3");
  } finally {
    if (previous === undefined) delete process.env.MINICODE_CRASH_REPORT_URL;
    else process.env.MINICODE_CRASH_REPORT_URL = previous;
  }
});

test("explicit loopback testing allows only HTTP, including IPv6", () => {
  const previous = process.env.MINICODE_ALLOW_INSECURE_CRASH_ENDPOINT;
  process.env.MINICODE_ALLOW_INSECURE_CRASH_ENDPOINT = "1";
  try {
    assert.equal(crashReporting.validatedCrashSubmitUrl("http://[::1]/intake"), "http://[::1]/intake");
    assert.equal(crashReporting.validatedCrashSubmitUrl("http://localhost/intake"), "http://localhost/intake");
    assert.throws(() => crashReporting.validatedCrashSubmitUrl("ftp://localhost/intake"), /must use HTTPS/);
    assert.throws(() => crashReporting.validatedCrashSubmitUrl("http://remote.example/intake"), /must use HTTPS/);
    assert.throws(() => crashReporting.validatedCrashSubmitUrl("http://user:pass@[::1]/intake"), /cannot contain credentials/);
  } finally {
    if (previous === undefined) delete process.env.MINICODE_ALLOW_INSECURE_CRASH_ENDPOINT;
    else process.env.MINICODE_ALLOW_INSECURE_CRASH_ENDPOINT = previous;
  }
});
