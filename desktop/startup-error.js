"use strict";

const state = {
  busy: false,
};

const titleEl = document.getElementById("title");
const messageEl = document.getElementById("message");
const detailEl = document.getElementById("detail");
const logPathEl = document.getElementById("logPath");
const retryButton = document.getElementById("retry");
const logsButton = document.getElementById("logs");
const quitButton = document.getElementById("quit");

function setBusy(nextBusy) {
  state.busy = nextBusy;
  retryButton.disabled = nextBusy;
  retryButton.textContent = nextBusy ? "Retrying..." : "Retry startup";
}

function applyStartupState(payload) {
  if (!payload) return;
  titleEl.textContent = payload.title || "MiniCode Desktop couldn't finish startup";
  messageEl.textContent = payload.message || "MiniCode Desktop hit a startup issue.";
  detailEl.textContent = payload.detail || "No additional details were captured.";
  logPathEl.textContent = payload.logsPath ? `Log: ${payload.logsPath}` : "";
}

async function bootstrap() {
  if (!window.minicodeStartup) {
    detailEl.textContent = "Startup bridge was not available in this recovery surface.";
    retryButton.disabled = true;
    logsButton.disabled = true;
    return;
  }

  applyStartupState(await window.minicodeStartup.getState());
  window.minicodeStartup.onState((payload) => {
    applyStartupState(payload);
    setBusy(false);
  });
}

retryButton.addEventListener("click", async () => {
  if (!window.minicodeStartup || state.busy) return;
  setBusy(true);
  const ok = await window.minicodeStartup.retry();
  if (!ok) {
    setBusy(false);
  }
});

logsButton.addEventListener("click", async () => {
  if (!window.minicodeStartup) return;
  await window.minicodeStartup.openLogs();
});

quitButton.addEventListener("click", async () => {
  if (!window.minicodeStartup) return;
  await window.minicodeStartup.quit();
});

void bootstrap();
