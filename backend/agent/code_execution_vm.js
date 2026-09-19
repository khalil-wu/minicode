(() => {
  "use strict";
  for (const name of ["console", "Atomics", "SharedArrayBuffer", "WebAssembly"]) delete globalThis[name];
  const stringify = JSON.stringify, parse = JSON.parse;
  const then = Function.call.bind(Promise.prototype.then);
  const pending = new Map(), timers = new Map();
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
  function image(value) {
    const raw = typeof value === "string" ? value : value?.image_url;
    if (raw !== undefined) {
      const match = /^data:(image\/[^;,]+);base64,([\s\S]+)$/.exec(raw);
      if (!match) throw new TypeError("image() requires an image data URL");
      emit({ kind: "image", media_type: match[1], data: match[2] });
    } else {
      const mediaType = value?.media_type ?? value?.mimeType;
      if (typeof value?.data !== "string" || !mediaType?.startsWith("image/")) throw new TypeError("image() requires {data, media_type} or an MCP image block");
      emit({ kind: "image", media_type: mediaType, data: value.data });
    }
  }
  function audio(value) {
    const raw = typeof value === "string" ? value : value?.audio_url;
    if (raw !== undefined) {
      const match = /^data:(audio\/[^;,]+);base64,([\s\S]+)$/.exec(raw);
      if (!match) throw new TypeError("audio() requires an audio data URL");
      emit({ kind: "audio", media_type: match[1], data: match[2] });
    } else {
      const mediaType = value?.media_type ?? value?.mimeType;
      if (typeof value?.data !== "string" || !mediaType?.startsWith("audio/")) throw new TypeError("audio() requires {data, media_type} or an MCP audio block");
      emit({ kind: "audio", media_type: mediaType, data: value.data });
    }
  }
  const globals = {
    text(value) { emit({ kind: "text", text: typeof value === "string" ? value : stringify(value) ?? String(value) }); },
    image,
    audio,
    generatedImage(value) { image(value.image_url); if (value.output_hint) globals.text(value.output_hint); },
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
      for (const entry of payload.tools) tools[entry.name] = args => request(entry.name, args, entry.freeform);
      Object.defineProperty(globalThis, "tools", { value: Object.freeze(tools) });
      Object.defineProperty(globalThis, "ALL_TOOLS", { value: payload.tools });
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
          if (continuation && !done) continuation.resolve(reply.value);
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
