"""MiniCode's independently implemented Windows receiving helper.

Uses documented Win32 APIs, Pillow and psutil; contains no Codex runtime code.
Importing this module never enumerates, captures or operates a desktop.
"""
from __future__ import annotations

import base64
import ctypes as C
from ctypes import wintypes as W
import io
import json
import os
import sys


class MOUSEINPUT(C.Structure):
    _fields_ = [("dx", C.c_int32), ("dy", C.c_int32), ("mouseData", C.c_uint32),
        ("dwFlags", C.c_uint32), ("time", C.c_uint32), ("dwExtraInfo", C.c_size_t)]


class KEYBDINPUT(C.Structure):
    _fields_ = [("wVk", C.c_uint16), ("wScan", C.c_uint16), ("dwFlags", C.c_uint32),
        ("time", C.c_uint32), ("dwExtraInfo", C.c_size_t)]


class INPUTDATA(C.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(C.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", C.c_uint32), ("data", INPUTDATA)]


class BITMAPINFOHEADER(C.Structure):
    _fields_ = [("biSize", C.c_uint32), ("biWidth", C.c_int32), ("biHeight", C.c_int32),
        ("biPlanes", C.c_uint16), ("biBitCount", C.c_uint16), ("biCompression", C.c_uint32),
        ("biSizeImage", C.c_uint32), ("biXPelsPerMeter", C.c_int32),
        ("biYPelsPerMeter", C.c_int32), ("biClrUsed", C.c_uint32), ("biClrImportant", C.c_uint32)]


class WindowsDesktop:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("MiniCode native CUA receiving currently supports Windows only")
        self.user = C.WinDLL("user32", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.gdi = C.WinDLL("gdi32", use_last_error=True)
        self.callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        declarations = (
            (self.user, "SetProcessDpiAwarenessContext", [W.HANDLE], W.BOOL),
            (self.user, "EnumWindows", [self.callback, W.LPARAM], W.BOOL),
            (self.user, "IsWindowVisible", [W.HWND], W.BOOL),
            (self.user, "IsWindow", [W.HWND], W.BOOL),
            (self.user, "GetWindowTextLengthW", [W.HWND], C.c_int),
            (self.user, "GetWindowTextW", [W.HWND, W.LPWSTR, C.c_int], C.c_int),
            (self.user, "GetWindowThreadProcessId", [W.HWND, C.POINTER(W.DWORD)], W.DWORD),
            (self.user, "GetWindowRect", [W.HWND, C.POINTER(W.RECT)], W.BOOL),
            (self.user, "GetForegroundWindow", [], W.HWND),
            (self.user, "SetForegroundWindow", [W.HWND], W.BOOL),
            (self.user, "GetWindowDC", [W.HWND], W.HDC),
            (self.user, "ReleaseDC", [W.HWND, W.HDC], C.c_int),
            (self.user, "PrintWindow", [W.HWND, W.HDC, W.UINT], W.BOOL),
            (self.user, "GetSystemMetrics", [C.c_int], C.c_int),
            (self.user, "SendInput", [W.UINT, C.POINTER(INPUT), C.c_int], W.UINT),
            (self.user, "SetPropW", [W.HWND, W.LPCWSTR, W.HANDLE], W.BOOL),
            (self.user, "GetPropW", [W.HWND, W.LPCWSTR], W.HANDLE),
            (self.user, "RemovePropW", [W.HWND, W.LPCWSTR], W.HANDLE),
            (self.gdi, "CreateCompatibleDC", [W.HDC], W.HDC),
            (self.gdi, "CreateCompatibleBitmap", [W.HDC, C.c_int, C.c_int], W.HBITMAP),
            (self.gdi, "SelectObject", [W.HDC, W.HANDLE], W.HANDLE),
            (self.gdi, "DeleteObject", [W.HANDLE], W.BOOL),
            (self.gdi, "DeleteDC", [W.HDC], W.BOOL),
            (self.gdi, "GetDIBits", [W.HDC, W.HBITMAP, W.UINT, W.UINT, C.c_void_p,
                C.POINTER(BITMAPINFOHEADER), W.UINT], C.c_int),
            (self.kernel, "OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            (self.kernel, "CloseHandle", [W.HANDLE], W.BOOL),
            (self.kernel, "GetApplicationUserModelId", [W.HANDLE, C.POINTER(W.UINT), W.LPWSTR], W.LONG),
        )
        for library, name, args, result in declarations:
            function = getattr(library, name)
            function.argtypes, function.restype = args, result
        if not self.user.SetProcessDpiAwarenessContext(C.c_void_p(-4)):
            raise C.WinError(C.get_last_error())

    def windows(self):
        result = []
        @self.callback
        def collect(hwnd, _):
            if self.user.IsWindowVisible(hwnd):
                title = self.title(hwnd)
                if title:
                    pid = W.DWORD()
                    self.user.GetWindowThreadProcessId(hwnd, C.byref(pid))
                    result.append({"window_id": str(hwnd), "pid": pid.value, "title": title})
            return True
        if not self.user.EnumWindows(collect, 0):
            raise C.WinError(C.get_last_error())
        return result

    def title(self, hwnd):
        text = C.create_unicode_buffer(self.user.GetWindowTextLengthW(hwnd) + 1)
        self.user.GetWindowTextW(hwnd, text, len(text))
        return text.value

    def identity(self, hwnd):
        import psutil
        if not self.user.IsWindow(hwnd):
            raise RuntimeError("The selected window no longer exists")
        pid = W.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, C.byref(pid))
        process = psutil.Process(pid.value)
        executable, created = process.exe(), process.create_time()
        if process.name().casefold() in {"applicationframehost.exe", "wwahost.exe"}:
            raise RuntimeError("Hosted Windows app identity requires per-window AUMID resolution; this independent provider does not grant access through the shared host executable")
        handle = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not handle:
            raise C.WinError(C.get_last_error())
        try:
            size = W.UINT()
            status = self.kernel.GetApplicationUserModelId(handle, C.byref(size), None)
            if status == 15703:  # APPMODEL_ERROR_NO_APPLICATION: an unpackaged executable.
                aumid = None
            elif status == 122:  # ERROR_INSUFFICIENT_BUFFER: the API returns its required size.
                value = C.create_unicode_buffer(size.value)
                status = self.kernel.GetApplicationUserModelId(handle, C.byref(size), value)
                if status:
                    raise OSError(status, "GetApplicationUserModelId failed")
                aumid = value.value
            else:
                raise OSError(status, "GetApplicationUserModelId did not return an application identity")
        finally:
            self.kernel.CloseHandle(handle)
        return {"pid": pid.value, "created": created, "executable": executable,
            "aumid": aumid, "app_id": aumid or executable.casefold(), "name": process.name()}

    def bounds(self, hwnd):
        rect = W.RECT()
        if not self.user.GetWindowRect(hwnd, C.byref(rect)):
            raise C.WinError(C.get_last_error())
        return {"x": rect.left, "y": rect.top, "width": rect.right - rect.left, "height": rect.bottom - rect.top}

    def pin(self, hwnd, token):
        if not self.user.SetPropW(hwnd, "MiniCode.Cua." + token, 1):
            raise C.WinError(C.get_last_error())

    def check_pin(self, hwnd, token):
        if self.user.GetPropW(hwnd, "MiniCode.Cua." + token) != 1:
            raise RuntimeError("The original window target was destroyed or replaced")

    def unpin(self, hwnd, token):
        self.user.RemovePropW(hwnd, "MiniCode.Cua." + token)

    def capture(self, hwnd):
        from PIL import Image
        bounds = self.bounds(hwnd)
        width, height = bounds["width"], bounds["height"]
        if width <= 0 or height <= 0:
            raise RuntimeError("The selected window has no capturable area")
        source = self.user.GetWindowDC(hwnd)
        if not source:
            raise C.WinError(C.get_last_error())
        try:
            memory = self.gdi.CreateCompatibleDC(source)
            if not memory:
                raise C.WinError(C.get_last_error())
            try:
                bitmap = self.gdi.CreateCompatibleBitmap(source, width, height)
                if not bitmap:
                    raise C.WinError(C.get_last_error())
                try:
                    original = self.gdi.SelectObject(memory, bitmap)
                    try:
                        if not self.user.PrintWindow(hwnd, memory, 2):  # PW_RENDERFULLCONTENT; never capture the desktop as a substitute.
                            raise RuntimeError("Windows PrintWindow could not capture the selected window")
                    finally:
                        self.gdi.SelectObject(memory, original)
                    header = BITMAPINFOHEADER(C.sizeof(BITMAPINFOHEADER), width, -height, 1, 32)
                    pixels = C.create_string_buffer(width * height * 4)
                    if self.gdi.GetDIBits(memory, bitmap, 0, height, pixels, C.byref(header), 0) != height:
                        raise RuntimeError("Windows GetDIBits did not return the complete window image")
                    image = Image.frombytes("RGB", (width, height), pixels.raw, "raw", "BGRX")
                    encoded = io.BytesIO()
                    image.save(encoded, format="PNG")
                    return {"mimeType": "image/png", "data": base64.b64encode(encoded.getvalue()).decode(),
                        "width": width, "height": height}
                finally:
                    self.gdi.DeleteObject(bitmap)
            finally:
                self.gdi.DeleteDC(memory)
        finally:
            self.user.ReleaseDC(hwnd, source)

    def focus(self, hwnd):
        self.user.SetForegroundWindow(hwnd)
        if self.user.GetForegroundWindow() != hwnd:
            raise RuntimeError("Windows did not grant foreground input to the selected window")

    def send(self, hwnd, events, check_target):
        self.focus(hwnd)
        check_target()
        if self.user.GetForegroundWindow() != hwnd:
            raise RuntimeError("Foreground changed before the selected window's input could be sent")
        batch = (INPUT * len(events))(*events)
        sent = self.user.SendInput(len(batch), batch, C.sizeof(INPUT))
        if sent != len(batch):
            raise RuntimeError(f"Windows SendInput accepted {sent} of {len(batch)} target events")

    def move_event(self, hwnd, x, y):
        bounds = self.bounds(hwnd)
        if not (0 <= x < bounds["width"] and 0 <= y < bounds["height"]):
            raise ValueError("Input coordinates must remain inside the selected window image")
        left, top = self.user.GetSystemMetrics(76), self.user.GetSystemMetrics(77)
        width, height = self.user.GetSystemMetrics(78), self.user.GetSystemMetrics(79)
        return INPUT(0, INPUTDATA(mi=MOUSEINPUT(round((bounds["x"] + x - left) * 65535 / (width - 1)),
            round((bounds["y"] + y - top) * 65535 / (height - 1)), 0, 0xC001)))

    def input(self, hwnd, method, args, check_target):
        if method == "focus":
            self.focus(hwnd)
            check_target()
            return
        if method == "type_text":
            units = args["text"].encode("utf-16-le")
            events = [INPUT(1, INPUTDATA(ki=KEYBDINPUT(0, int.from_bytes(units[i:i + 2], "little"), flag)))
                for i in range(0, len(units), 2) for flag in (4, 6)]
        elif method == "keypress":
            mapping = {"CTRL": 0x11, "SHIFT": 0x10, "ALT": 0x12, "META": 0x5B,
                "ENTER": 0x0D, "TAB": 9, "ESC": 0x1B, "BACKSPACE": 8, "DELETE": 0x2E,
                "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28, "HOME": 0x24,
                "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22, "SPACE": 0x20}
            keys = [ord(key.upper()) if len(key) == 1 and key.isascii() and key.isalnum() else mapping[key.upper()]
                for key in args["keys"]]
            events = [INPUT(1, INPUTDATA(ki=KEYBDINPUT(key, 0, 0))) for key in keys]
            events += [INPUT(1, INPUTDATA(ki=KEYBDINPUT(key, 0, 2))) for key in reversed(keys)]
        else:
            events = [self.move_event(hwnd, args["x"], args["y"])]
            if method == "click":
                down, up = {"left": (2, 4), "right": (8, 16), "middle": (32, 64)}[args.get("button", "left")]
                events += [INPUT(0, INPUTDATA(mi=MOUSEINPUT(0, 0, 0, flag)))
                    for _ in range(args.get("click_count", 1)) for flag in (down, up)]
            elif method == "scroll":
                events.append(INPUT(0, INPUTDATA(mi=MOUSEINPUT(0, 0, args["delta"] & 0xFFFFFFFF, 0x800))))
            elif method != "move":
                raise ValueError("Unsupported native input operation: " + method)
        if events:
            self.send(hwnd, events, check_target)


class NativeCuaSession:
    def __init__(self, desktop, approval_store=None):
        self.desktop, self.targets, self.approval_store = desktop, {}, approval_store

    def dispatch(self, method, args, owner):
        key = (owner["session_id"], owner["turn_id"])
        if not all(key):
            raise ValueError("Native CUA requires its original thread and turn")
        if method == "list_windows":
            return self.desktop.windows()
        if method == "lookup_app_approval":
            return self.approval_store.get(args["app_id"]) if self.approval_store is not None else None
        if method == "select_window":
            hwnd = int(args["window_id"])
            identity = self.desktop.identity(hwnd)
            token = args["target"]
            self.desktop.pin(hwnd, token)
            self.targets[token] = {"hwnd": hwnd, "identity": identity, "owner": key, "approved": False, "approval_scope": ""}
            return {**identity, "target": token, "window_id": str(hwnd), "title": self.desktop.title(hwnd),
                "bounds": self.desktop.bounds(hwnd)}
        if method == "end_turn":
            targets = [token for token, value in self.targets.items() if value["owner"] == key]
            for token in targets:
                self.desktop.unpin(self.targets[token]["hwnd"], token)
                del self.targets[token]
            return bool(targets)
        target = self.targets[args["target"]]
        if target["owner"] != key:
            raise PermissionError("The window target belongs to another thread or turn")
        def check_target():
            self.desktop.check_pin(target["hwnd"], args["target"])
            if self.desktop.identity(target["hwnd"]) != target["identity"]:
                raise RuntimeError("The original window or process identity has changed")
            if target["approval_scope"] == "always" and self.approval_store.get(target["identity"]["app_id"]) is None:
                target["approved"] = False
                raise PermissionError("Permanent Computer Use app approval was revoked")
        check_target()
        if method == "grant_app_approval":
            if self.approval_store is None:
                raise RuntimeError("This receiving provider has no persistent app approval store")
            return self.approval_store.grant(target["identity"])
        if method == "authorize":
            scope = args.get("scope", "session")
            if scope == "always" and (self.approval_store is None or self.approval_store.get(target["identity"]["app_id"]) is None):
                raise PermissionError("The selected app has no persistent approval")
            target["approved"] = True
            target["approval_scope"] = scope
            return None
        if not target["approved"]:
            raise PermissionError("The selected app has not been approved")
        if method == "get_state":
            return {"title": self.desktop.title(target["hwnd"]), "bounds": self.desktop.bounds(target["hwnd"])}
        if method == "screenshot":
            image = self.desktop.capture(target["hwnd"])
            check_target()
            return image
        self.desktop.input(target["hwnd"], method, args, check_target)
        return None

    def close(self):
        for token, value in self.targets.items():
            self.desktop.unpin(value["hwnd"], token)
        self.targets.clear()


def main():
    # This bootstrap checks the receiving interpreter, never a controlling host's platform.
    from PIL import Image
    import psutil
    del Image, psutil
    desktop = WindowsDesktop()
    cleanups = json.loads(sys.argv[1])
    for pin in cleanups:
        desktop.unpin(int(pin["window_id"]), pin["target"])
    approval_store = None
    if sys.argv[2]:
        from backend.mcp.cua_approval_store import NativeAppApprovalStore
        from pathlib import Path
        approval_store = NativeAppApprovalStore(Path(sys.argv[2]))
    session = NativeCuaSession(desktop, approval_store)
    print(json.dumps({"ready": True, "implementation": "minicode-win32", "platform": sys.platform,
        "cleanedTargetCount": len(cleanups)}), flush=True)
    try:
        for line in sys.stdin:
            request = json.loads(line)
            try:
                result = session.dispatch(request["method"], request["args"], request["owner"])
                response = {"id": request["id"], "result": result}
            except Exception as error:
                response = {"id": request["id"], "error": f"{type(error).__name__}: {error}"}
            print(json.dumps(response), flush=True)
    finally:
        session.close()


if __name__ == "__main__":
    main()
