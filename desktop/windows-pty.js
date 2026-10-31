"use strict";

const { spawn } = require("node:child_process");
const { EventEmitter } = require("node:events");
const readline = require("node:readline");
const { StringDecoder } = require("node:string_decoder");

const SOURCE_VERSION = "0.158.0-alpha.2.1";

function createWindowsPty(executable) {
  return {
    spawn(program, args, options) {
      const child = spawn(executable, ["--desktop-pty"], {
        cwd: options.cwd, env: options.env, windowsHide: true, stdio: ["pipe", "pipe", "pipe"],
      });
      const events = new EventEmitter();
      const decoder = new StringDecoder("utf8");
      let closed = false;
      let terminating = false;
      let result;
      let transportError;
      const send = message => child.stdin.write(JSON.stringify(message) + "\n");
      const listen = (event, listener) => {
        events.on(event, listener);
        return { dispose: () => events.removeListener(event, listener) };
      };
      child.stderr.on("data", bytes => events.emit("data", bytes.toString("utf8")));
      child.on("error", error => { transportError = error; events.emit("data", `${error.message}\r\n`); });
      child.stdin.on("error", error => { transportError = error; events.emit("data", `${error.message}\r\n`); });
      const output = (async () => {
        for await (const line of readline.createInterface({ input: child.stdout })) {
          const message = JSON.parse(line);
          switch (message.event) {
            case "ready":
              if (message.identity.identity !== "minicode-desktop-pty" || message.identity.protocol !== 1
                || message.identity.sourceVersion !== SOURCE_VERSION) throw new Error("The native terminal owner has a different Source contract.");
              events.emit("ready");
              break;
            case "data": events.emit("data", decoder.write(Buffer.from(message.data, "base64"))); break;
            case "resized": events.emit("resized", { cols: message.cols, rows: message.rows }); break;
            case "inputClosed": events.emit("data", `${message.error}\r\n`); break;
            case "exit": result = { exitCode: message.exitCode }; break;
            default: throw new Error(`Unknown native terminal event: ${message.event}`);
          }
        }
      })().catch(error => {
        transportError = error;
        events.emit("data", `${error.message}\r\n`);
        child.stdin.end();
      });
      child.once("close", async (code, signal) => {
        closed = true;
        await output;
        const tail = decoder.end();
        if (tail) events.emit("data", tail);
        events.emit("exit", !transportError && code === 0 && result
          ? result : { exitCode: null, signal: signal || `owner-exit-${code}` });
      });
      send({ method: "start", params: { program, args, cwd: options.cwd, cols: options.cols, rows: options.rows } });
      return {
        pid: child.pid, process: program,
        onData: listener => listen("data", listener),
        onExit: listener => listen("exit", listener),
        onReady: listener => listen("ready", listener),
        onResize: listener => listen("resized", listener),
        write(data) {
          if (closed || terminating || result) return false;
          send({ method: "write", params: { data } });
          return true;
        },
        resize(cols, rows) {
          if (closed || terminating || result) return false;
          send({ method: "resize", params: { cols, rows } });
          return true;
        },
        kill() {
          if (closed || terminating || result) return;
          terminating = true;
          send({ method: "terminate" });
          child.stdin.end();
        },
        forceKill() { if (!closed) child.kill(); },
      };
    },
  };
}

module.exports = { createWindowsPty };
