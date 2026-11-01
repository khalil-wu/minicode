"use strict";

const crypto = require("node:crypto");

// Media belongs to one renderer. The backend only receives this original
// claim, never authority to reassign another window's media or thread owner.
class RealtimeVoiceManager {
  constructor({ windowManager, getRemoteDesktop, platform = process.platform }) {
    this.windows = windowManager;
    this.getRemoteDesktop = getRemoteDesktop;
    this.platform = platform;
    this.active = null;
    this.transfer = null;
    this.readyWindows = new Set();
    this.readyWaiters = new Set();
    this.requests = new Map();
  }

  host(record) { return record.remoteHostId || "local"; }
  locator(claim) { return { threadId: claim.threadId, hostId: claim.hostId }; }

  assertClaim(record, claimId) {
    const claim = this.active;
    if (!claim || claim.record !== record || claim.claimId !== claimId || record.window.isDestroyed()) {
      throw new Error("The original desktop voice claim is no longer active.");
    }
    return claim;
  }

  claim(record, payload) {
    if (typeof payload.threadId !== "string" || !payload.threadId) throw new Error("Voice requires its original task.");
    if (this.active) throw new Error("Another desktop voice chat is still active.");
    const transfer = this.transfer;
    if (transfer && (payload.transferId !== transfer.id || transfer.targetRecord !== record
      || transfer.target.threadId !== payload.threadId || transfer.target.hostId !== this.host(record))) {
      throw new Error("A voice transfer owns the next desktop voice start.");
    }
    if (!transfer && payload.transferId) throw new Error("The voice transfer is no longer active.");
    const claim = { record, threadId: payload.threadId, hostId: this.host(record), claimId: crypto.randomUUID(),
      phase: "starting", previous: transfer ? transfer.previous : null };
    this.active = claim;
    return { claimId: claim.claimId, hostId: claim.hostId,
      captureScreenContext: this.platform === "win32", transferVoiceCall: true };
  }

  update(record, payload) {
    const claim = this.assertClaim(record, payload.claimId);
    if (!["active", "stopping"].includes(payload.phase)) throw new Error("Unsupported desktop voice phase.");
    if (claim.phase === "stopping" && payload.phase !== "stopping") throw new Error("The original voice chat is stopping.");
    claim.phase = payload.phase;
    if (claim.phase === "stopping") this.cancelCaptures(claim);
  }

  release(record, payload) {
    const claim = this.assertClaim(record, payload.claimId);
    this.active = null;
    this.cancelCaptures(claim);
  }

  cancelCaptures(claim, reason = "The original voice chat ended before screen context arrived.") {
    for (const request of this.requests.values()) {
      if (request.claim === claim && request.type === "capture") request.reject(new Error(reason));
    }
  }

  ready(record, { connected }) {
    if (connected) {
      this.readyWindows.add(record.windowInstanceId);
      for (const waiter of this.readyWaiters) if (waiter.record === record) waiter.resolve();
    } else {
      const wasReady = this.readyWindows.delete(record.windowInstanceId);
      if (wasReady && this.transfer && (this.transfer.original.record === record || this.transfer.targetRecord === record)) {
        this.transfer.controller.abort(new Error("The desktop voice session disconnected during transfer."));
      }
      for (const request of this.requests.values()) if (request.record === record) request.reject(new Error("The desktop voice session disconnected."));
    }
  }

  waitReady(record, signal) {
    signal.throwIfAborted();
    if (record.window.isDestroyed()) throw new Error("The destination voice window is closed.");
    if (this.readyWindows.has(record.windowInstanceId)) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const finish = (error) => {
        this.readyWaiters.delete(waiter);
        signal.removeEventListener("abort", aborted);
        if (error) reject(error); else resolve();
      };
      const aborted = () => finish(signal.reason);
      const waiter = { record, resolve: () => finish(), reject: finish };
      this.readyWaiters.add(waiter);
      signal.addEventListener("abort", aborted, { once: true });
    });
  }

  request(record, claim, type, payload, signal) {
    signal.throwIfAborted();
    if (record.window.isDestroyed()) throw new Error("The desktop voice window is closed.");
    return new Promise((resolve, reject) => {
      const requestId = crypto.randomUUID();
      const finish = (error, value) => {
        this.requests.delete(requestId);
        signal.removeEventListener("abort", aborted);
        if (error) {
          if (this.readyWindows.has(record.windowInstanceId) && !record.window.isDestroyed()) record.window.webContents.send("minicode:realtimeVoice:request", { type: "cancel", requestId });
          reject(error);
        } else resolve(value);
      };
      const aborted = () => finish(signal.reason);
      this.requests.set(requestId, { record, claim, type, callId: payload.callId,
        resolve: value => finish(null, value), reject: error => finish(error) });
      signal.addEventListener("abort", aborted, { once: true });
      try { record.window.webContents.send("minicode:realtimeVoice:request", { ...payload, type, requestId, claimId: claim.claimId }); }
      catch (error) { this.requests.delete(requestId); signal.removeEventListener("abort", aborted); reject(error); }
    });
  }

  reply(record, payload) {
    const request = this.requests.get(payload.requestId);
    if (!request) return false;
    if (request.record !== record || payload.claimId !== request.claim.claimId || payload.callId !== request.callId) {
      throw new Error("The voice reply does not belong to its original request.");
    }
    if (payload.error) request.reject(new Error(payload.error));
    else request.resolve(payload.value);
    return true;
  }

  async targetRecord(original, target) {
    if (target.hostId === original.hostId) return original.record;
    const existing = this.windows.getMainWindows().map(window => this.windows.getWindowRecord(window))
      .find(record => this.host(record) === target.hostId);
    if (existing) return existing;
    const options = target.hostId === "local" ? {} : { remoteHostId: target.hostId,
      runtime: (await this.getRemoteDesktop().connect(target.hostId)).runtime() };
    const window = await this.windows.createNewWindow(options);
    return this.windows.getWindowRecord(window);
  }

  async capture(claim, callId, signal) {
    if (this.platform !== "win32") throw new Error("Page and sidebar capture is unavailable on this device.");
    if (this.transfer) throw new Error("Screen context is unavailable during voice transfer.");
    const window = this.windows.getFocusedMainWindow();
    if (!window || !window.isFocused() || window.isDestroyed()) throw new Error("MiniCode must be foreground to read page and sidebar context.");
    const record = this.windows.getWindowRecord(window);
    const value = await this.request(record, claim, "capture", { callId }, signal);
    signal.throwIfAborted();
    if (this.active !== claim || claim.phase !== "active" || this.transfer || !window.isFocused() || window.isDestroyed()
      || this.windows.getFocusedMainWindow() !== window) throw new Error("The original voice or foreground page changed before screen context arrived.");
    return value;
  }

  async transferCall(claim, args, callId, signal) {
    if (this.transfer) throw new Error("A voice transfer is already in progress.");
    const returning = args.return === true;
    const allowed = returning ? ["return", "context"] : ["threadId", "hostId", "context"];
    if (Object.keys(args).some(key => !allowed.includes(key)) || (!returning && (typeof args.threadId !== "string" || !args.threadId))
      || (args.hostId !== undefined && (typeof args.hostId !== "string" || !args.hostId))
      || (args.context !== undefined && (typeof args.context !== "string" || args.context.length > 4000))) {
      throw new Error("Invalid voice transfer destination or context.");
    }
    const target = returning ? claim.previous : { threadId: args.threadId, hostId: args.hostId || claim.hostId };
    if (!target) throw new Error("There is no previous voice task to return to.");
    if (target.threadId === claim.threadId && target.hostId === claim.hostId) throw new Error("The voice chat already belongs to that task.");
    const transfer = { id: crypto.randomUUID(), original: claim, target, targetRecord: null, controller: new AbortController(),
      previous: returning ? null : claim.previous || this.locator(claim) };
    this.transfer = transfer;
    this.cancelCaptures(claim, "Voice transfer started before screen context arrived.");
    signal = AbortSignal.any([signal, transfer.controller.signal]);
    try {
      const record = await this.targetRecord(claim, target);
      transfer.targetRecord = record;
      await this.waitReady(record, signal);
      const prepared = await this.request(record, claim, "prepare", { callId, threadId: target.threadId }, signal);
      signal.throwIfAborted();
      if (args.context && prepared.capability && prepared.capability.version && prepared.capability.version !== "v3") {
        // Only realtime v3 accepts handoff context (initial_items). Refuse
        // before the original call is stopped so a rejected transfer cannot
        // end both conversations.
        throw new Error("The destination voice task uses realtime " + prepared.capability.version + " and cannot receive handoff context; transfer without context or use a realtime v3 destination.");
      }
      if (this.active !== claim || claim.phase !== "active") throw new Error("The original voice chat changed before transfer.");
      claim.phase = "stopping";
      const stopped = await this.request(claim.record, claim, "stop", { callId }, signal);
      if (!stopped.mediaReleased || !stopped.stopped || this.active === claim) throw new Error("The original voice chat has no media release and stop receipt.");
      signal.throwIfAborted();
      const started = await this.request(record, claim, "start", { callId, threadId: target.threadId,
        transferId: transfer.id, context: args.context, capability: prepared.capability }, signal);
      signal.throwIfAborted();
      if (!started.started || this.active?.record !== record || this.active.threadId !== target.threadId
        || this.active.hostId !== target.hostId || this.active.phase !== "active") throw new Error("The destination voice chat did not start.");
      this.windows.focusMainWindow(record.window);
      return { transferred: true, ...target };
    } finally {
      if (this.transfer === transfer) this.transfer = null;
    }
  }

  async executeControl(payload, record, signal) {
    const claim = this.assertClaim(record, payload.claim_id);
    if (claim.phase !== "active" || claim.threadId !== payload.conversation_id || claim.hostId !== payload.host_id
      || typeof payload.call_id !== "string" || !payload.call_id) throw new Error("Voice tools require the original active task, host and call request.");
    if (payload.tool === "capture_screen_context") return this.capture(claim, payload.call_id, signal);
    if (payload.tool === "transfer_voice_call") return this.transferCall(claim, payload.arguments, payload.call_id, signal);
    throw new Error("Unsupported desktop voice tool.");
  }

  releaseWindow(windowInstanceId) {
    this.readyWindows.delete(windowInstanceId);
    if (this.transfer && (this.transfer.original.record.windowInstanceId === windowInstanceId
      || this.transfer.targetRecord?.windowInstanceId === windowInstanceId)) {
      this.transfer.controller.abort(new Error("The voice window is no longer available during transfer."));
    }
    for (const waiter of this.readyWaiters) if (waiter.record.windowInstanceId === windowInstanceId) waiter.reject(new Error("The voice window is no longer available."));
    for (const request of this.requests.values()) {
      if (request.record.windowInstanceId === windowInstanceId || request.claim.record.windowInstanceId === windowInstanceId) request.reject(new Error("The original voice window is no longer available."));
    }
    if (this.active?.record.windowInstanceId === windowInstanceId) this.active = null;
  }
}

module.exports = { RealtimeVoiceManager };
