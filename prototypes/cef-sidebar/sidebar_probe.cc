// Isolated CI experiment. No MiniCode runtime, profile, or production IPC.
#include <windows.h>

#include <cmath>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <iostream>
#include <string>
#include <thread>
#include <utility>

#include "include/cef_app.h"
#include "include/cef_client.h"
#include "include/cef_command_line.h"
#include "include/cef_parser.h"
#include "include/cef_request_context.h"
#include "include/cef_sandbox_win.h"
#include "include/cef_task.h"
#include "include/views/cef_display.h"

namespace {

struct Config {
  HWND owner;
  std::filesystem::path profile;
  std::filesystem::path output;
  std::string phase;
  std::string url;
  std::string token;
  double scale;
};

void Emit(CefRefPtr<CefDictionaryValue> dict) {
  auto value = CefValue::Create();
  value->SetDictionary(dict);
  std::cout << "CEF_PROBE_EVENT "
            << CefWriteJSON(value, JSON_WRITER_DEFAULT).ToString() << std::endl;
}

std::string Handle(HWND hwnd) {
  return std::to_string(reinterpret_cast<uintptr_t>(hwnd));
}

// Chrome child window_info.bounds is passed into CefWindow's DIP InitParams.
// Read the actual owner client pixels, then use CEF's display conversion (which
// includes forced device scale), rather than assuming a 96-DPI owner rectangle.
CefRect SidebarBounds(HWND owner) {
  RECT client{};
  GetClientRect(owner, &client);
  POINT screen{};
  ClientToScreen(owner, &screen);
  auto display = CefDisplay::GetDisplayNearestPoint(CefPoint(screen.x, screen.y), true);
  CefPoint origin(client.right * 2 / 5, 0);
  CefPoint end(client.right, client.bottom);
  display->ConvertPointFromPixels(origin);
  display->ConvertPointFromPixels(end);
  auto facts = CefDictionaryValue::Create();
  facts->SetString("event", "coordinate-conversion");
  facts->SetDouble("cefDisplayScale", display->GetDeviceScaleFactor());
  facts->SetInt("ownerPixelWidth", client.right);
  facts->SetInt("ownerPixelHeight", client.bottom);
  facts->SetInt("paneDipX", origin.x);
  facts->SetInt("paneDipWidth", end.x - origin.x);
  facts->SetInt("paneDipHeight", end.y - origin.y);
  Emit(facts);
  return CefRect(origin.x, origin.y, end.x - origin.x, end.y - origin.y);
}

class Task final : public CefTask {
 public:
  explicit Task(std::function<void()> run) : run_(std::move(run)) {}
  void Execute() override { run_(); }

 private:
  std::function<void()> run_;
  IMPLEMENT_REFCOUNTING(Task);
};

class ProbeClient final : public CefClient,
                          public CefLifeSpanHandler,
                          public CefLoadHandler,
                          public CefDisplayHandler {
 public:
  explicit ProbeClient(Config config) : config_(std::move(config)) {}
  CefRefPtr<CefLifeSpanHandler> GetLifeSpanHandler() override { return this; }
  CefRefPtr<CefLoadHandler> GetLoadHandler() override { return this; }
  CefRefPtr<CefDisplayHandler> GetDisplayHandler() override { return this; }
  bool failed() const { return failed_; }

  void Check(const std::string& name, bool passed) {
    auto event = Event("check");
    event->SetString("name", name);
    event->SetBool("passed", passed);
    Emit(event);
    failed_ |= !passed;
  }

  void OnAfterCreated(CefRefPtr<CefBrowser> browser) override {
    browser_ = browser;
    Emit(Event("browser-created"));
    // ChildWindowDelegate finishes creating/showing the Chrome widget after
    // this callback. Position its HWND on the next UI task, after that work.
    CefRefPtr<ProbeClient> self = this;
    CefPostTask(TID_UI, new Task([self]() { self->PlaceWindow("created"); }));
  }

  void OnLoadEnd(CefRefPtr<CefBrowser> browser,
                 CefRefPtr<CefFrame> frame,
                 int http_status) override {
    if (!frame->IsMain()) return;
    Check("http-response", http_status == 200);
    Check("chrome-style", browser->GetHost()->GetRuntimeStyle() ==
                              CEF_RUNTIME_STYLE_CHROME);
    auto context = browser->GetHost()->GetRequestContext();
    auto profile = Event("profile");
    profile->SetString("cachePath", context->GetCachePath());
    Emit(profile);
    Check("persistent-context-path", !context->GetCachePath().empty());
    if (config_.phase == "write") {
      context->SetContentSetting("", "", CEF_CONTENT_SETTING_TYPE_NOTIFICATIONS,
                                 CEF_CONTENT_SETTING_VALUE_BLOCK);
      context->SetContentSetting(config_.url, config_.url,
                                 CEF_CONTENT_SETTING_TYPE_POPUPS,
                                 CEF_CONTENT_SETTING_VALUE_ALLOW);
    }
    Check("default-content-setting",
          context->GetContentSetting("", "", CEF_CONTENT_SETTING_TYPE_NOTIFICATIONS) ==
              CEF_CONTENT_SETTING_VALUE_BLOCK);
    Check("site-content-setting",
          context->GetContentSetting(config_.url, config_.url,
                                     CEF_CONTENT_SETTING_TYPE_POPUPS) ==
              CEF_CONTENT_SETTING_VALUE_ALLOW);
    browser->GetHost()->SetFocus(true);
    EvaluateLater("loaded");
  }

  void OnLoadError(CefRefPtr<CefBrowser> /*browser*/,
                   CefRefPtr<CefFrame> frame,
                   ErrorCode code,
                   const CefString& text,
                   const CefString& url) override {
    if (!frame->IsMain()) return;
    auto event = Event("load-error");
    event->SetInt("code", code);
    event->SetString("text", text);
    event->SetString("url", url);
    Emit(event);
    Check("main-frame-load", false);
  }

  void OnTitleChange(CefRefPtr<CefBrowser> /*browser*/,
                     const CefString& title) override {
    const std::string raw = title.ToString();
    const std::string prefix = "CEF_PROBE:";
    if (!raw.starts_with(prefix)) return;
    auto value = CefParseJSON(raw.substr(prefix.size()), JSON_PARSER_RFC);
    auto metrics = value->GetDictionary();
    const std::string stage = metrics->GetString("stage");
    metrics->SetString("event", "renderer-metrics");
    metrics->SetString("phase", config_.phase);
    Emit(metrics);
    Check(stage + ":local-storage", metrics->GetString("value").ToString() == config_.token);
    const double observed_scale = metrics->GetType("dpr") == VTYPE_INT
        ? metrics->GetInt("dpr") : metrics->GetDouble("dpr");
    Check(stage + ":device-scale",
          std::abs(observed_scale - config_.scale) < 0.01);
    Check(stage + ":document-focus", metrics->GetBool("focused"));
    InspectWindow(stage, metrics);
    Emit(Event(stage == "loaded" ? "ready-resize" : "ready-close"));
  }

  void Command(const std::string& command) {
    if (command == "resize") {
      PlaceWindow("resize");
      browser_->GetHost()->SetFocus(true);
      EvaluateLater("resized");
    } else if (command == "close") {
      Emit(Event("close-requested"));
      browser_->GetHost()->CloseBrowser(false);
    } else {
      Check("known-host-command", false);
    }
  }

  void OnBeforeClose(CefRefPtr<CefBrowser> /*browser*/) override {
    Check("before-close:owner-alive", IsWindow(config_.owner) != FALSE);
    browser_ = nullptr;
    Emit(Event("before-close"));
    CefQuitMessageLoop();
  }

 private:
  CefRefPtr<CefDictionaryValue> Event(const std::string& name) const {
    auto event = CefDictionaryValue::Create();
    event->SetString("event", name);
    event->SetString("phase", config_.phase);
    return event;
  }

  void EvaluateLater(const std::string& stage) {
    CefRefPtr<ProbeClient> self = this;
    CefPostDelayedTask(TID_UI, new Task([self, stage]() {
      self->browser_->GetMainFrame()->ExecuteJavaScript(
          "window.probeReport('" + stage + "')", self->config_.url, 0);
    }), 250);
  }

  void PlaceWindow(const std::string& stage) {
    RECT parent{};
    Check(stage + ":placement-owner-rect", GetClientRect(config_.owner, &parent) != FALSE);
    const int left = parent.right * 2 / 5;
    HWND child = browser_->GetHost()->GetWindowHandle();
    // SetWindowPos takes native pixels in the bootstrap's DPI-aware context.
    // CEF's Chrome widget initializes/clamps its own screen bounds; ownership
    // by an external HWND requires the host to position the actual child.
    const BOOL positioned = SetWindowPos(
        child, nullptr, left, 0, parent.right - left, parent.bottom,
        SWP_NOZORDER | SWP_NOACTIVATE);
    const DWORD error = positioned ? ERROR_SUCCESS : GetLastError();
    auto event = Event("native-placement");
    event->SetString("stage", stage);
    event->SetInt("x", left);
    event->SetInt("width", parent.right - left);
    event->SetInt("height", parent.bottom);
    event->SetInt("win32Error", static_cast<int>(error));
    Emit(event);
    Check(stage + ":set-window-pos", positioned != FALSE);
    browser_->GetHost()->NotifyMoveOrResizeStarted();
  }

  void InspectWindow(const std::string& stage,
                     CefRefPtr<CefDictionaryValue> metrics) {
    HWND child = browser_->GetHost()->GetWindowHandle();
    RECT parent{}, rect{};
    Check(stage + ":owner-rect", GetClientRect(config_.owner, &parent) != FALSE);
    Check(stage + ":child-rect", GetWindowRect(child, &rect) != FALSE);
    POINT origin{rect.left, rect.top};
    Check(stage + ":map-owner", ScreenToClient(config_.owner, &origin) != FALSE);
    const int width = rect.right - rect.left;
    const int height = rect.bottom - rect.top;
    DWORD owner_pid = 0;
    GetWindowThreadProcessId(config_.owner, &owner_pid);
    HWND focus = GetFocus();
    auto facts = Event("native-window");
    facts->SetString("stage", stage);
    facts->SetString("owner", Handle(config_.owner));
    facts->SetString("child", Handle(child));
    facts->SetString("parent", Handle(GetParent(child)));
    facts->SetString("root", Handle(GetAncestor(child, GA_ROOT)));
    facts->SetString("focus", Handle(focus));
    facts->SetString("foreground", Handle(GetForegroundWindow()));
    facts->SetInt("ownerPid", static_cast<int>(owner_pid));
    facts->SetInt("helperPid", static_cast<int>(GetCurrentProcessId()));
    facts->SetInt("x", origin.x);
    facts->SetInt("y", origin.y);
    facts->SetInt("width", width);
    facts->SetInt("height", height);
    facts->SetInt("ownerDpi", static_cast<int>(GetDpiForWindow(config_.owner)));
    facts->SetInt("childDpi", static_cast<int>(GetDpiForWindow(child)));
    Emit(facts);
    Check(stage + ":cross-process-owner", owner_pid != GetCurrentProcessId());
    Check(stage + ":original-hwnd-child", GetParent(child) == config_.owner &&
          GetAncestor(child, GA_ROOT) == config_.owner &&
          (GetWindowLongPtr(child, GWL_STYLE) & WS_CHILD) != 0);
    Check(stage + ":sidebar-bounds", origin.x == parent.right * 2 / 5 &&
          origin.y == 0 && width == parent.right - origin.x && height == parent.bottom);
    Check(stage + ":native-focus", focus == child || IsChild(child, focus));
    Check(stage + ":native-dpi", GetDpiForWindow(child) > 0 &&
          GetDpiForWindow(child) == GetDpiForWindow(config_.owner));
    Check(stage + ":viewport-size",
          std::abs(metrics->GetInt("width") * config_.scale - width) <= 2 &&
          std::abs(metrics->GetInt("height") * config_.scale - height) <= 2);
    int extra_windows = 0;
    EnumWindows([](HWND window, LPARAM data) -> BOOL {
      DWORD pid = 0;
      GetWindowThreadProcessId(window, &pid);
      if (pid == GetCurrentProcessId() && IsWindowVisible(window)) {
        ++*reinterpret_cast<int*>(data);
      }
      return TRUE;
    }, reinterpret_cast<LPARAM>(&extra_windows));
    Check(stage + ":no-extra-top-level", extra_windows == 0);
  }

  const Config config_;
  CefRefPtr<CefBrowser> browser_;
  bool failed_ = false;
  IMPLEMENT_REFCOUNTING(ProbeClient);
};

class ProbeApp final : public CefApp, public CefBrowserProcessHandler {
 public:
  ProbeApp(Config config, CefRefPtr<ProbeClient> client)
      : config_(std::move(config)), client_(client) {}
  CefRefPtr<CefBrowserProcessHandler> GetBrowserProcessHandler() override {
    return this;
  }
  void OnContextInitialized() override {
    const bool owner_valid = IsWindow(config_.owner) != FALSE;
    client_->Check("owner-valid-at-create", owner_valid);
    if (!owner_valid) {
      CefQuitMessageLoop();
      return;
    }
    CefRequestContextSettings context_settings;
    CefString(&context_settings.cache_path) = (config_.profile / "Sidebar").wstring();
    auto context = CefRequestContext::CreateContext(context_settings, nullptr);
    CefWindowInfo info;
    info.SetAsChild(config_.owner, SidebarBounds(config_.owner));
    info.runtime_style = CEF_RUNTIME_STYLE_CHROME;
    CefBrowserSettings settings;
    client_->Check("create-browser-accepted", CefBrowserHost::CreateBrowser(
        info, client_, config_.url, settings, nullptr, context));
  }

 private:
  const Config config_;
  CefRefPtr<ProbeClient> client_;
  IMPLEMENT_REFCOUNTING(ProbeApp);
};

}  // namespace

CEF_BOOTSTRAP_EXPORT int RunConsoleMain(int /*argc*/, char* /*argv*/[],
                                        void* sandbox_info,
                                        cef_version_info_t* /*version_info*/) {
  CefMainArgs args(GetModuleHandle(nullptr));
  const int subprocess = CefExecuteProcess(args, nullptr, sandbox_info);
  if (subprocess >= 0) return subprocess;
  auto command = CefCommandLine::CreateCommandLine();
  command->InitFromString(GetCommandLineW());
  Config config{
      reinterpret_cast<HWND>(std::stoull(command->GetSwitchValue("owner-hwnd").ToString())),
      command->GetSwitchValue("profile-root").ToWString(),
      command->GetSwitchValue("output").ToWString(),
      command->GetSwitchValue("phase"), command->GetSwitchValue("url"),
      command->GetSwitchValue("token"),
      std::stod(command->GetSwitchValue("force-device-scale-factor").ToString())};
  if (!sandbox_info) {
    std::cerr << "CEF sandbox bootstrap did not provide sandbox_info" << std::endl;
    return 2;
  }
  CefSettings settings;
  CefString(&settings.root_cache_path) = config.profile.wstring();
  CefString(&settings.cache_path) = (config.profile / "Default").wstring();
  CefString(&settings.log_file) = (config.output / ("cef-" + config.phase + ".log")).wstring();
  CefRefPtr<ProbeClient> client = new ProbeClient(config);
  CefRefPtr<ProbeApp> app = new ProbeApp(config, client);
  client->Check("sandbox-bootstrap", sandbox_info != nullptr);
  if (!CefInitialize(args, settings, app, sandbox_info)) {
    std::cerr << "CefInitialize exit code: " << CefGetExitCode() << std::endl;
    return 2;
  }
  std::thread input([client]() {
    std::string line;
    while (std::getline(std::cin, line)) {
      CefPostTask(TID_UI, new Task([client, line]() { client->Command(line); }));
      if (line == "close") break;
    }
  });
  CefRunMessageLoop();
  auto exited = CefDictionaryValue::Create();
  exited->SetString("event", "message-loop-exited");
  Emit(exited);
  input.join();
  const int result = client->failed() ? 1 : 0;
  app = nullptr;
  client = nullptr;
  CefShutdown();
  return result;
}
