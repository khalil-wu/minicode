(() => {
  "use strict";
  for (const name of ["console", "Atomics", "SharedArrayBuffer", "WebAssembly"]) delete globalThis[name];
  const stringify = JSON.stringify, parse = JSON.parse;
  const then = Function.call.bind(Promise.prototype.then);
  const pending = new Map(), timers = new Map();
  const resultSources = new WeakMap();
  let requests = [], output = [], updates = [], storage = Object.create(null);
  let sequence = 0, done = false, error = "", yielded = false, outputSize = 0;
  const EXIT = {};
  function emit(entry) {
    if (done) return;
    if (entry.kind === "text") {
      outputSize += entry.text.length;
      if (outputSize > 1024 * 1024) throw new RangeError("Script text output exceeded one million characters; filter results or save an artifact.");
    }
    output.push(entry);
  }
  function request(name, args, freeform) {
    if (done) throw new Error("The script has completed");
    const id = String(++sequence);
    const detached = parse(stringify(typeof args === "string" && freeform ? { [freeform.input_field]: args } : args ?? {}));
    if (detached === null || typeof detached !== "object" || Array.isArray(detached)) throw new TypeError(`tools.${name} expects a JSON argument object`);
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      requests.push({ kind: "tool", id, name, args: detached });
    });
  }
  const imageExpects = "image expects a non-empty image URL string, an object with image_url and optional detail, or a raw MCP image block";
  const audioExpects = "audio expects a non-empty audio URL string, an object with audio_url, or a raw MCP audio block";
  function detailValue(value) {
    if (value === undefined || value === null) return undefined;
    if (typeof value !== "string") throw new TypeError("image detail must be a string when provided");
    return value;
  }
  function mediaUrl(value, kind, expects) {
    if (typeof value === "string") return value;
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError(expects);
    const raw = value[`${kind}_url`];
    if (raw !== undefined) {
      if (typeof raw !== "string") throw new TypeError(expects);
      return raw;
    }
    if (typeof value.type !== "string") throw new TypeError(expects);
    if (value.type !== kind) throw new TypeError(`${kind} only accepts MCP ${kind} blocks, got "${value.type}"`);
    if (typeof value.data !== "string" || !value.data) throw new TypeError(`${kind} expected MCP ${kind} data`);
    if (value.data.toLowerCase().startsWith("data:")) return value.data;
    const mime = value.mimeType ?? value.mime_type;
    return `data:${typeof mime === "string" && mime ? mime : "application/octet-stream"};base64,${value.data}`;
  }
  function image(value, detailOverride) {
    const artifact = typeof value?.artifact_id === "string" ? { artifact_id: value.artifact_id } : {};
    const override = detailValue(detailOverride);
    const imageUrl = mediaUrl(value, "image", imageExpects);
    if (!imageUrl) throw new TypeError(imageExpects);
    const scheme = imageUrl.split(":", 1)[0].toLowerCase();
    if (scheme === "http" || scheme === "https") throw new TypeError("Tool call failed: remote image URLs are not supported in tool outputs. Pass a base64 data URI instead");
    if (scheme !== "data" || !imageUrl.includes(":")) throw new TypeError("Tool call failed: invalid image output. Pass a base64 data URI instead");
    const validDetails = ["auto", "low", "high", "original"];
    const embedded = typeof value === "object" && value !== null && value.image_url !== undefined
      ? detailValue(value.detail)
      : validDetails.includes(value?._meta?.["codex/imageDetail"]) ? value._meta["codex/imageDetail"] : undefined;
    const detail = (override ?? embedded ?? "high").toLowerCase();
    if (!validDetails.includes(detail)) throw new TypeError("image detail must be one of: auto, low, high, original");
    emit({ kind: "image", image_url: imageUrl, detail, ...artifact });
  }
  function audio(value) {
    const audioUrl = mediaUrl(value, "audio", audioExpects);
    if (!audioUrl) throw new TypeError(audioExpects);
    if (!/^data:/i.test(audioUrl)) throw new TypeError("Tool call failed: invalid audio output. Pass a base64 data URI instead");
    emit({ kind: "audio", audio_url: audioUrl });
  }
  const globals = {
    text(value) { emit({ kind: "text", text: value === null || ["undefined", "string", "boolean", "number", "bigint"].includes(typeof value) ? String(value) : stringify(value) ?? String(value),
      ...(resultSources.has(value) ? { tool_call_id: resultSources.get(value) }
        : Array.isArray(value) && value.length && value.every(item => resultSources.has(item))
          ? { tool_call_ids: value.map(item => resultSources.get(item)) } : {}) }); },
    image,
    audio,
    generatedImage(value) {
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError("generatedImage expects an image generation result object");
      if (value.output_hint !== undefined && typeof value.output_hint !== "string") throw new TypeError("generatedImage output_hint must be a string when provided");
      image(value); output[output.length - 1].source = "image_generation";
      if (value.output_hint !== undefined) globals.text(value.output_hint);
    },
    notify(value) { globals.text(value); yielded = true; },
    yield_control() { yielded = true; return Promise.resolve(); },
    exit() { done = true; throw EXIT; },
    store(key, value) {
      if (typeof key !== "string") throw new TypeError("store key must be a string");
      const detached = parse(stringify(value));
      storage[key] = detached;
      updates.push({ key, value: detached });
    },
    load(key) { return storage[key] === undefined ? undefined : parse(stringify(storage[key])); },
    setTimeout(callback, delay = 0, ...args) {
      if (typeof callback !== "function") throw new TypeError("setTimeout requires a function");
      const id = String(++sequence);
      timers.set(id, () => callback(...args));
      requests.push({ kind: "timer", id, delay: Math.max(0, Number(delay) || 0) });
      return id;
    },
    clearTimeout(id) { timers.delete(String(id)); requests.push({ kind: "cancel_timer", id: String(id) }); },
  };
  for (const [key, value] of Object.entries(globals)) Object.defineProperty(globalThis, key, { value, writable: false, configurable: false });
  return (operation, encoded) => {
    const payload = encoded ? parse(encoded) : null;
    if (operation === "start") {
      storage = Object.assign(Object.create(null), payload.storage);
      const tools = Object.create(null);
      for (const entry of payload.tools) tools[entry.public_name ?? entry.name] = args => request(entry.name, args, entry.freeform);
      Object.defineProperty(globalThis, "tools", { value: Object.freeze(tools) });
      Object.defineProperty(globalThis, "ALL_TOOLS", { value: payload.tools.map(({public_name, ...entry}) =>
        ({...entry, name: public_name ?? entry.name})) });
      try {
        const promise = (0, eval)(`(async () => { "use strict";\n${payload.code}\n})()`);
        then(promise, () => { done = true; }, value => {
          if (value !== EXIT) error = String(value?.stack || value);
          done = true;
        });
      } catch (value) {
        if (value !== EXIT) error = String(value?.stack || value);
        done = true;
      }
    } else if (operation === "deliver") {
      for (const reply of payload) {
        if (reply.timer) {
          const callback = timers.get(reply.id); timers.delete(reply.id);
          if (callback && !done) { try { callback(); } catch (value) { error = String(value?.stack || value); done = true; } }
        } else {
          const continuation = pending.get(reply.id); pending.delete(reply.id);
          if (continuation && !done) {
            if (reply.value !== null && (typeof reply.value === "object" || typeof reply.value === "function")) resultSources.set(reply.value, reply.tool_call_id);
            continuation.resolve(reply.value);
          }
        }
      }
    } else if (operation === "abort") {
      error = payload; done = true;
    }
    if (operation === "drain") {
      const packet = stringify({ requests, output, updates, done, error, yielded, pending_tools: pending.size });
      requests = []; output = []; updates = []; yielded = false;
      return packet;
    }
    return "";
  };
})()
