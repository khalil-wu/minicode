"use strict";

const fs = require("node:fs");
const crypto = require("node:crypto");
const { atomicWriteTextSync } = require("./utils");
let historyFile = "";
let visits = [];

function init(file) {
  historyFile = file;
  visits = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : [];
}
function commit(next) {
  atomicWriteTextSync(historyFile, `${JSON.stringify(next, null, 2)}\n`);
  visits = next;
}
function recordVisit(url, title, source) {
  if (url === "about:blank") return null;
  const visit = { id: crypto.randomUUID(), url, title, visitTime: Date.now(), source };
  commit([visit, ...visits]);
  return visit.id;
}
function updateTitle(id, title) {
  if (!id) return;
  commit(visits.map((visit) => visit.id === id ? { ...visit, title } : visit));
}
function search({ text = "", source = "all" } = {}) {
  const query = text.trim().toLocaleLowerCase();
  return visits.filter((visit) => (source === "all" || visit.source === source)
    && `${visit.title} ${visit.url}`.toLocaleLowerCase().includes(query));
}
function remove(ids) {
  const selectedDays = new Set(visits.filter((visit) => ids.includes(visit.id))
    .map((visit) => `${visit.url}\0${new Date(visit.visitTime).toDateString()}`));
  commit(visits.filter((visit) => !selectedDays.has(`${visit.url}\0${new Date(visit.visitTime).toDateString()}`)));
  return true;
}
function clear() { commit([]); return true; }

module.exports = { init, recordVisit, updateTitle, search, remove, clear };
