"use strict";

async function releaseEditorWindow({ apiBaseUrl, runtimeToken, windowOwnerId, windowInstanceId, desktopLifecycleToken }, request = fetch) {
  const response = await request(`${apiBaseUrl.replace(/\/+$/, "")}/api/workspace/language/lifecycle`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-minicode-token": runtimeToken,
      "x-minicode-desktop-lifecycle-token": desktopLifecycleToken },
    body: JSON.stringify({ action: "release_window", window_owner_id: windowOwnerId, window_instance_id: windowInstanceId }),
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error(`Editor window cleanup failed with HTTP ${response.status}.`);
}

async function closeWindowsAfterDraftCommit(windows) {
  await Promise.all(windows.map((window) => new Promise((resolve, reject) => {
    if (window.isDestroyed()) { resolve(); return; }
    const closed = () => resolve();
    window.once("closed", closed);
    try {
      // The renderer's existing beforeunload barrier commits composer and
      // editor drafts before it resumes this real native close.
      window.close();
    } catch (error) {
      window.removeListener("closed", closed);
      reject(error);
    }
  })));
}

module.exports = { releaseEditorWindow, closeWindowsAfterDraftCommit };
