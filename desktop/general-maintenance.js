"use strict";

/** Compression runs once at startup; auto-archive keeps Source sQe/oQe idle cadence. */
function startGeneralMaintenance({ getPreferences, getIdleSeconds, runMaintenance, onError,
  now = Date.now, schedule = setInterval, cancel = clearInterval }) {
  if (getPreferences().localHistoryCompression) {
    void (async () => runMaintenance({ auto_archive_after_days: 0, compress_local_history: true }))().catch(onError);
  }
  let nextRunAt = 0;
  let running = false;
  let disposed = false;
  const check = async () => {
    const preferences = getPreferences();
    if (disposed || running || now() < nextRunAt || getIdleSeconds() < 900
      || preferences.autoArchiveAfterDays === 0) return;
    running = true;
    nextRunAt = now() + 900000;
    try {
      await runMaintenance({ auto_archive_after_days: preferences.autoArchiveAfterDays,
        compress_local_history: false });
    } catch (error) {
      onError(error);
    } finally {
      running = false;
    }
  };
  const timer = schedule(check, 60000);
  timer.unref?.();
  return { check, dispose() { disposed = true; cancel(timer); } };
}

module.exports = { startGeneralMaintenance };
