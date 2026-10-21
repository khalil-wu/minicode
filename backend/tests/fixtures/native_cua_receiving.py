"""Controlled native boundary: no desktop, capture or input API is imported."""
import json
import os
import sys
import time
from pathlib import Path

from backend.mcp.native_cua_windows import NativeCuaSession


class FixtureDesktop:
    def __init__(self):
        self.current_identity = {"pid": 91, "created": 123.5, "executable": "C:\\fixture\\editor.exe",
            "aumid": None, "app_id": "fixture.editor", "name": "Fixture Editor"}
        self.pins = set()
        self.operations = []

    def windows(self):
        return [{"window_id": "11", "pid": 91, "title": "Controlled receiving window"}]

    def identity(self, hwnd):
        assert hwnd == 11
        return dict(self.current_identity)

    def pin(self, hwnd, token):
        self.pins.add((hwnd, token))

    def check_pin(self, hwnd, token):
        if (hwnd, token) not in self.pins:
            raise RuntimeError("The original window target was destroyed or replaced")

    def unpin(self, hwnd, token):
        self.pins.discard((hwnd, token))

    def title(self, hwnd):
        return "Controlled receiving window"

    def bounds(self, hwnd):
        return {"x": 30, "y": 40, "width": 100, "height": 80}

    def capture(self, hwnd):
        self.operations.append(("fixture_capture", hwnd))
        return {"mimeType": "image/png", "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=", "width": 1, "height": 1}

    def input(self, hwnd, method, args, check_target):
        check_target()
        self.operations.append((method, hwnd, args))


def main():
    session = NativeCuaSession(FixtureDesktop())
    log = Path(os.environ["MINICODE_CUA_FIXTURE_LOG"])
    cleanups = json.loads(sys.argv[1])
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"bootstrap_cleanup": cleanups, "pid": os.getpid()}) + "\n")
    print(json.dumps({"ready": True, "implementation": "minicode-win32", "platform": "win32",
        "cleanedTargetCount": len(cleanups)}), flush=True)
    try:
        for line in sys.stdin:
            request = json.loads(line)
            if request["method"] == "screenshot" and os.environ.get("MINICODE_CUA_FIXTURE_BLOCK") == "1":
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"blocked": True, "pid": os.getpid(), "owner": request["owner"]}) + "\n")
                time.sleep(600)  # Real blocking stand-in; original owner must terminate this process.
            try:
                result = session.dispatch(request["method"], request["args"], request["owner"])
                response = {"id": request["id"], "result": result}
            except Exception as error:
                response = {"id": request["id"], "error": f"{type(error).__name__}: {error}"}
            with log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({**request, "response": response}) + "\n")
            print(json.dumps(response), flush=True)
    finally:
        session.close()
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"closed": True, "remaining_pins": len(session.desktop.pins)}) + "\n")


if __name__ == "__main__":
    main()
