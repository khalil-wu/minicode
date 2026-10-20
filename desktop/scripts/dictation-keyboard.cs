using System;
using System.Collections.Generic;
using System.Collections.Concurrent;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;

namespace MiniCode {
  public static class DictationKeyboard {
    const uint WM_COMMAND = 0x8001;
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    static readonly object OutputLock = new object();
    static readonly ConcurrentQueue<Dictionary<string, object>> Commands = new ConcurrentQueue<Dictionary<string, object>>();
    static readonly HookProc HookDelegate = OnKeyboard;
    static readonly Dictionary<string, Shortcut> Shortcuts = new Dictionary<string, Shortcut>();
    static readonly HashSet<string> Down = new HashSet<string>();
    static readonly HashSet<uint> IgnoreUntilUp = new HashSet<uint>();
    static IntPtr Hook;
    static uint ThreadId;
    static bool Active;
    static bool Suspended;

    class Shortcut { public uint Vk; public uint Modifiers; public int Id; public bool Bare; }
    delegate IntPtr HookProc(int code, IntPtr message, IntPtr data);
    [StructLayout(LayoutKind.Sequential)] struct KeyboardData { public uint vk, scan, flags, time; public UIntPtr extra; }
    [StructLayout(LayoutKind.Sequential)] struct Message { public IntPtr hwnd; public uint message; public UIntPtr wParam; public IntPtr lParam; public uint time; public int x, y; }
    [StructLayout(LayoutKind.Sequential)] struct Rect { public int left, top, right, bottom; }
    [StructLayout(LayoutKind.Sequential)] struct GuiInfo { public uint size, flags; public IntPtr active, focus, capture, menu, move, caret; public Rect caretRect; }
    [StructLayout(LayoutKind.Sequential)] struct KeyboardInput { public ushort vk, scan; public uint flags, time; public UIntPtr extra; }
    [StructLayout(LayoutKind.Sequential)] struct MouseInput { public int dx, dy; public uint data, flags, time; public UIntPtr extra; }
    [StructLayout(LayoutKind.Explicit)] struct InputUnion { [FieldOffset(0)] public KeyboardInput keyboard; [FieldOffset(0)] public MouseInput mouse; }
    [StructLayout(LayoutKind.Sequential)] struct Input { public uint type; public InputUnion data; }
    [DllImport("user32.dll", SetLastError=true)] static extern IntPtr SetWindowsHookEx(int kind, HookProc callback, IntPtr module, uint thread);
    [DllImport("user32.dll")] static extern bool UnhookWindowsHookEx(IntPtr hook);
    [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr hook, int code, IntPtr message, IntPtr data);
    [DllImport("user32.dll")] static extern int GetMessage(out Message message, IntPtr window, uint min, uint max);
    [DllImport("user32.dll")] static extern bool PeekMessage(out Message message, IntPtr window, uint min, uint max, uint remove);
    [DllImport("user32.dll")] static extern bool PostThreadMessage(uint thread, uint message, UIntPtr wParam, IntPtr lParam);
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
    [DllImport("kernel32.dll", CharSet=CharSet.Auto)] static extern IntPtr GetModuleHandle(string module);
    [DllImport("user32.dll")] static extern short GetAsyncKeyState(int key);
    [DllImport("user32.dll")] static extern uint GetDoubleClickTime();
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("user32.dll")] static extern bool GetGUIThreadInfo(uint thread, ref GuiInfo info);
    [DllImport("user32.dll", SetLastError=true)] static extern bool RegisterHotKey(IntPtr window, int id, uint modifiers, uint vk);
    [DllImport("user32.dll")] static extern bool UnregisterHotKey(IntPtr window, int id);
    [DllImport("user32.dll", SetLastError=true)] static extern uint SendInput(uint count, Input[] inputs, int size);

    static void Emit(object value) { lock(OutputLock) { Console.WriteLine(Json.Serialize(value)); Console.Out.Flush(); } }
    static uint LogicalKey(uint vk) { return vk == 162 || vk == 163 ? 17u : vk == 160 || vk == 161 ? 16u : vk == 164 || vk == 165 ? 18u : vk == 92 ? 91u : vk; }
    static uint Modifiers() {
      return ((GetAsyncKeyState(18) & 0x8000) != 0 ? 1u : 0u) | ((GetAsyncKeyState(17) & 0x8000) != 0 ? 2u : 0u)
        | ((GetAsyncKeyState(16) & 0x8000) != 0 ? 4u : 0u) | (((GetAsyncKeyState(91) | GetAsyncKeyState(92)) & 0x8000) != 0 ? 8u : 0u);
    }
    static uint ModifierFor(uint vk) { return vk == 18 ? 1u : vk == 17 ? 2u : vk == 16 ? 4u : vk == 91 ? 8u : 0u; }
    static Dictionary<string, object> Target() {
      IntPtr foreground = GetForegroundWindow(); uint pid;
      uint thread = GetWindowThreadProcessId(foreground, out pid);
      GuiInfo gui = new GuiInfo(); gui.size = (uint)Marshal.SizeOf(typeof(GuiInfo));
      if (!GetGUIThreadInfo(thread, ref gui)) throw new Win32Exception(Marshal.GetLastWin32Error());
      return new Dictionary<string, object> { {"foreground", foreground.ToInt64().ToString()}, {"pid", pid}, {"focus", gui.focus.ToInt64().ToString()} };
    }
    static IntPtr OnKeyboard(int code, IntPtr message, IntPtr pointer) {
      if (code < 0) return CallNextHookEx(Hook, code, message, pointer);
      KeyboardData data = (KeyboardData)Marshal.PtrToStructure(pointer, typeof(KeyboardData));
      if ((data.flags & 0x10) != 0) return CallNextHookEx(Hook, code, message, pointer);
      bool down = message.ToInt32() == 0x100 || message.ToInt32() == 0x104;
      bool up = message.ToInt32() == 0x101 || message.ToInt32() == 0x105;
      uint vk = LogicalKey(data.vk);
      if (IgnoreUntilUp.Contains(vk)) { if (up) IgnoreUntilUp.Remove(vk); return CallNextHookEx(Hook, code, message, pointer); }
      if (Active && !Suspended && vk == 27 && down) { Emit(new { kind="escape" }); return new IntPtr(1); }
      foreach (var entry in Shortcuts) {
        Shortcut shortcut = entry.Value;
        if (vk != shortcut.Vk) continue;
        if (up && Down.Remove(entry.Key)) { Emit(new { kind="key", hotkey=entry.Key, phase="up", timestamp=data.time }); return new IntPtr(1); }
        if (Suspended || !down || (Modifiers() & ~ModifierFor(vk)) != shortcut.Modifiers) continue;
        if (Down.Add(entry.Key)) Emit(new { kind="key", hotkey=entry.Key, phase="down", timestamp=data.time, target=Target() });
        return new IntPtr(1);
      }
      return CallNextHookEx(Hook, code, message, pointer);
    }
    static Dictionary<string, Shortcut> ParseShortcuts(Dictionary<string, object> command) {
      var result = new Dictionary<string, Shortcut>(); int id = 1;
      foreach (string name in new [] {"hold", "toggle"}) {
        object raw = command[name]; if (raw == null) continue;
        var value = (Dictionary<string, object>)raw;
        result[name] = new Shortcut { Vk=Convert.ToUInt32(value["vk"]), Modifiers=Convert.ToUInt32(value["modifiers"]), Bare=(bool)value["bare"], Id=id++ };
      }
      return result;
    }
    static void Unregister(Dictionary<string, Shortcut> values) { foreach (var entry in values) if (!entry.Value.Bare) UnregisterHotKey(IntPtr.Zero, entry.Value.Id); }
    static void Register(Dictionary<string, Shortcut> values) {
      foreach (var entry in values) if (!entry.Value.Bare && !RegisterHotKey(IntPtr.Zero, entry.Value.Id, entry.Value.Modifiers | 0x4000, entry.Value.Vk)) throw new Win32Exception(Marshal.GetLastWin32Error(), "The dictation shortcut is already registered by another application.");
    }
    static void Configure(Dictionary<string, object> command) {
      var next = ParseShortcuts(command); var previous = new Dictionary<string, Shortcut>(Shortcuts);
      Unregister(previous);
      try { Register(next); if (Suspended) Unregister(next); }
      catch { Unregister(next); if (!Suspended) Register(previous); throw; }
      Shortcuts.Clear(); foreach (var entry in next) Shortcuts.Add(entry.Key, entry.Value); Down.Clear();
      IgnorePressedKeys();
    }
    static void IgnorePressedKeys() {
      foreach (var entry in Shortcuts) if ((GetAsyncKeyState((int)entry.Value.Vk) & 0x8000) != 0) IgnoreUntilUp.Add(entry.Value.Vk);
    }
    static void SetSuspended(bool value) {
      if (Suspended == value) return;
      if (value) { Unregister(Shortcuts); Suspended=true; Down.Clear(); }
      else {
        try { Register(Shortcuts); } catch { Unregister(Shortcuts); throw; }
        IgnorePressedKeys(); Suspended=false;
      }
    }
    static bool Insert(Dictionary<string, object> command) {
      var expected = (Dictionary<string, object>)command["target"]; var current = Target();
      if (Convert.ToString(expected["foreground"]) != Convert.ToString(current["foreground"]) || Convert.ToString(expected["pid"]) != Convert.ToString(current["pid"])
        || Convert.ToString(expected["focus"]) != Convert.ToString(current["focus"]) || Convert.ToString(current["focus"]) == "0" || Modifiers() != 0) return false;
      string text = (string)command["text"]; var input = new Input[text.Length * 2];
      for (int i=0; i<text.Length; i++) {
        input[2*i] = new Input { type=1, data=new InputUnion { keyboard=new KeyboardInput { scan=text[i], flags=4 } } };
        input[2*i+1] = new Input { type=1, data=new InputUnion { keyboard=new KeyboardInput { scan=text[i], flags=6 } } };
      }
      uint sent = SendInput((uint)input.Length, input, Marshal.SizeOf(typeof(Input)));
      if (sent != input.Length) throw new Win32Exception(Marshal.GetLastWin32Error(), "Windows did not accept the dictation text.");
      return true;
    }
    static bool Handle(Dictionary<string, object> command) {
      object id=command["id"];
      try {
        string op=(string)command["op"]; object value=true;
        if (op == "configure") Configure(command);
        else if (op == "active") Active=(bool)command["value"];
        else if (op == "suspend") SetSuspended((bool)command["value"]);
        else if (op == "insert") value=Insert(command);
        else if (op == "shutdown") { Emit(new { id=id, ok=true, value=true }); return false; }
        else throw new ArgumentException("Unknown dictation command");
        Emit(new { id=id, ok=true, value=value });
      } catch (Exception error) { Emit(new { id=id, ok=false, error=error.Message }); }
      return true;
    }
    public static void Run() {
      Console.InputEncoding = System.Text.Encoding.UTF8; Console.OutputEncoding = new System.Text.UTF8Encoding(false);
      ThreadId=GetCurrentThreadId(); Message message; PeekMessage(out message, IntPtr.Zero, 0, 0, 0);
      Hook=SetWindowsHookEx(13, HookDelegate, GetModuleHandle(null), 0);
      if (Hook == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
      var reader=new Thread(() => {
        var commandJson = new JavaScriptSerializer();
        string line;
        while ((line=Console.ReadLine()) != null) { Commands.Enqueue(commandJson.Deserialize<Dictionary<string, object>>(line)); PostThreadMessage(ThreadId, WM_COMMAND, UIntPtr.Zero, IntPtr.Zero); }
        Commands.Enqueue(new Dictionary<string, object> { {"id", "eof"}, {"op", "shutdown"} }); PostThreadMessage(ThreadId, WM_COMMAND, UIntPtr.Zero, IntPtr.Zero);
      }); reader.IsBackground=true; reader.Start();
      Emit(new { kind="ready", doubleClickTime=GetDoubleClickTime() });
      try {
        while (GetMessage(out message, IntPtr.Zero, 0, 0) > 0) {
          if (message.message != WM_COMMAND) continue;
          Dictionary<string, object> command;
          while (Commands.TryDequeue(out command)) if (!Handle(command)) return;
        }
      } finally { Unregister(Shortcuts); UnhookWindowsHookEx(Hook); }
    }
  }
}
