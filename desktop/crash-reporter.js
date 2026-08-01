"use strict";

function validatedCrashSubmitUrl(rawUrl) {
  const value = String(rawUrl || "").trim();
  if (!value) return "";
  const parsed = new URL(value);
  const isLoopback = ["127.0.0.1", "localhost", "::1"].includes(parsed.hostname.toLowerCase());
  const allowLoopbackHttp = process.env.MINICODE_ALLOW_INSECURE_CRASH_ENDPOINT === "1" && isLoopback;
  if (parsed.protocol !== "https:" && !allowLoopbackHttp) {
    throw new Error("Crash report endpoint must use HTTPS; HTTP is limited to explicit loopback testing.");
  }
  if (parsed.username || parsed.password) {
    throw new Error("Crash report endpoint cannot contain credentials.");
  }
  return parsed.toString();
}

function init({ crashReporter, app, logger = () => {} } = {}) {
  if (!crashReporter?.start || process.env.MINICODE_DISABLE_CRASH_REPORTER === "1") {
    return { enabled: false, uploading: false };
  }

  let submitURL = "";
  try {
    submitURL = validatedCrashSubmitUrl(process.env.MINICODE_CRASH_REPORT_URL);
  } catch (error) {
    logger(`[crash-reporter] ${error.message}`);
  }

  const uploading = Boolean(submitURL);
  const options = {
    productName: "MiniCode",
    companyName: "MiniCode Team",
    uploadToServer: uploading,
    compress: true,
    ignoreSystemCrashHandler: false,
    extra: {
      version: typeof app?.getVersion === "function" ? app.getVersion() : "unknown",
      platform: process.platform,
      channel: String(process.env.MINICODE_RELEASE_CHANNEL || "stable"),
    },
  };
  if (uploading) options.submitURL = submitURL;

  try {
    crashReporter.start(options);
    logger(`[crash-reporter] started (${uploading ? "upload enabled" : "local dumps only"})`);
    return { enabled: true, uploading, submitURL };
  } catch (error) {
    logger(`[crash-reporter] failed to start: ${error.message}`);
    return { enabled: false, uploading: false, error: error.message };
  }
}

module.exports = { init, validatedCrashSubmitUrl };
