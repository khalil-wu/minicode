"use strict";

const { randomUUID } = require("node:crypto");

function createPetActionBridge(getWindowByInstanceId) {
  const pending = new Map();
  const requestOwner = (ownerId, payload) => {
    const window = getWindowByInstanceId(ownerId);
    if (!window || window.isDestroyed()) throw new Error("The task window is no longer open.");
    const id = randomUUID();
    return new Promise((resolve, reject) => {
      pending.set(id, { ownerId, resolve, reject });
      try { window.webContents.send("minicode:pets:actionRequested", { id, ...payload }); }
      catch (error) { pending.delete(id); reject(error); }
    });
  };
  return {
    request: (ownerId, taskId, actionId) => requestOwner(ownerId, { kind: "action", taskId, actionId }),
    requestFollowup: (ownerId, taskId, prompt) => requestOwner(ownerId, { kind: "followup", taskId, prompt }),
    complete(ownerId, result) {
      const receipt = pending.get(result.id);
      if (!receipt || receipt.ownerId !== ownerId) throw new Error("This pet action does not belong to the responding window.");
      pending.delete(result.id);
      if (result.error) receipt.reject(new Error(result.error));
      else receipt.resolve();
    },
    release(ownerId) {
      for (const [id, receipt] of pending) if (receipt.ownerId === ownerId) {
        pending.delete(id);
        receipt.reject(new Error("The task window closed before the response was accepted."));
      }
    },
  };
}

module.exports = { createPetActionBridge };
