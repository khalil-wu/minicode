"""Owned portable exec-server protocol peer for transport tests, not production."""
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n", write_through=True)
lock = threading.Lock()
processes = {}
handles = {}
workers = []


def send(message):
    with lock:
        print(json.dumps(message, ensure_ascii=False), flush=True)


def path(value):
    return Path(url2pathname(unquote(urlsplit(value).path)))


def info():
    return {"shell": {"name": "test", "path": sys.executable}, "executorVersion": "0.0.0",
            "providerId": "owned-stdio-test-peer", "cwd": Path.cwd().as_uri(),
            "capabilities": {"fileWriteStreaming": True, "sandboxedFileStreaming": True}}


def event(state, method, **params):
    with state["condition"]:
        seq = state["nextSeq"]
        state["nextSeq"] += 1
        payload = {"processId": state["id"], "seq": seq, **params}
        if method == "process/output":
            state["chunks"].append({"seq": seq, **params})
        if method == "process/exited":
            state.update(exited=True, exitCode=params["exitCode"])
        if method == "process/closed":
            state["closed"] = True
        send({"method": method, "params": payload})
        state["condition"].notify_all()


def launch(params):
    child = subprocess.Popen(params["argv"], cwd=path(params["cwd"]), env={**os.environ, **params["env"]},
                             stdin=subprocess.PIPE if params["pipeStdin"] else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
    state = {"id": params["processId"], "child": child, "chunks": [], "nextSeq": 1,
             "exited": False, "exitCode": None, "closed": False, "failure": None, "condition": threading.Condition()}
    processes[state["id"]] = state

    def pump(stream, name):
        while chunk := stream.read1(65536):
            event(state, "process/output", stream=name, chunk=base64.b64encode(chunk).decode())

    def supervise():
        readers = [threading.Thread(target=pump, args=(child.stdout, "stdout")),
                   threading.Thread(target=pump, args=(child.stderr, "stderr"))]
        for reader in readers:
            reader.start()
        code = child.wait()
        for reader in readers:
            reader.join()
        event(state, "process/exited", exitCode=code)
        event(state, "process/closed")

    worker = threading.Thread(target=supervise)
    workers.append(worker)
    worker.start()
    return {"processId": state["id"], "sandboxType": "none"}


def call(method, params):
    if method == "initialize":
        return {"sessionId": "owned-stdio-session", "environmentInfo": info()}
    if method == "initialized":
        return None
    if method == "environment/info":
        return info()
    if method == "environment/status":
        return {"status": "ready"}
    if method.startswith("fs/") and method not in {"fs/readBlock", "fs/writeBlock", "fs/close"}:
        assert "sandbox" in params
        if params["sandbox"] == {"deny": True}:
            raise PermissionError("fixture sandbox denied")
    if method == "fs/readFile":
        return {"dataBase64": base64.b64encode(path(params["path"]).read_bytes()).decode()}
    if method == "fs/writeFile":
        path(params["path"]).write_bytes(base64.b64decode(params["dataBase64"], validate=True))
        return {}
    if method == "fs/canonicalize":
        return {"path": path(params["path"]).resolve().as_uri()}
    if method == "fs/open":
        handles[params["handleId"]] = path(params["path"]).open("w+b" if params["mode"] == "replace" else "rb")
        return {"handleId": params["handleId"]}
    if method == "fs/readBlock":
        handle = handles[params["handleId"]]
        handle.seek(params["offset"])
        data = handle.read(params["len"])
        return {"chunk": base64.b64encode(data).decode(), "eof": len(data) < params["len"]}
    if method == "fs/writeBlock":
        handle = handles[params["handleId"]]
        handle.seek(params["offset"])
        handle.write(base64.b64decode(params["chunk"], validate=True))
        handle.flush()
        return {}
    if method == "fs/close":
        handles.pop(params["handleId"]).close()
        return {}
    if method == "process/start":
        assert "sandbox" in params
        return launch(params)
    state = processes[params["processId"]]
    if method == "process/read":
        with state["condition"]:
            if state["nextSeq"] - 1 <= (params["afterSeq"] or 0) and not state["closed"]:
                state["condition"].wait((params["waitMs"] or 0) / 1000)
            return {key: state[key] for key in ("nextSeq", "exited", "exitCode", "closed", "failure")} | {
                "chunks": [item for item in state["chunks"] if item["seq"] > (params["afterSeq"] or 0)]}
    if method == "process/write":
        state["child"].stdin.write(base64.b64decode(params["chunk"], validate=True))
        state["child"].stdin.flush()
        return {"status": "accepted"}
    if method == "process/terminate":
        running = state["child"].poll() is None
        if running:
            state["child"].terminate()
        return {"running": running}
    raise ValueError(method)


def handle(message):
    try:
        result = call(message["method"], message.get("params"))
        if "id" in message:
            send({"id": message["id"], "result": result})
    except Exception as error:
        send({"id": message.get("id"), "error": {"code": -32000, "message": str(error)}})


try:
    for line in sys.stdin:
        worker = threading.Thread(target=handle, args=(json.loads(line),))
        workers.append(worker)
        worker.start()
finally:
    for state in processes.values():
        if state["child"].poll() is None:
            state["child"].terminate()
    for worker in workers:
        worker.join()
    for handle in handles.values():
        handle.close()
