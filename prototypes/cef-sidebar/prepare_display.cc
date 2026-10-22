// Disposable GitHub runner display setup. Never changes persisted display modes.
#include <windows.h>

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

namespace {

bool Snapshot(std::ostream& out, const char* stage) {
  MONITORINFO monitor{};
  monitor.cbSize = sizeof(monitor);
  const BOOL ok = GetMonitorInfoW(
      MonitorFromPoint(POINT{0, 0}, MONITOR_DEFAULTTOPRIMARY), &monitor);
  const int width = monitor.rcWork.right - monitor.rcWork.left;
  const int height = monitor.rcWork.bottom - monitor.rcWork.top;
  out << "{\"event\":\"display-snapshot\",\"stage\":\"" << stage
      << "\",\"monitorApiSucceeded\":" << (ok ? "true" : "false")
      << ",\"screenWidth\":" << GetSystemMetrics(SM_CXSCREEN)
      << ",\"screenHeight\":" << GetSystemMetrics(SM_CYSCREEN)
      << ",\"workWidth\":" << width << ",\"workHeight\":" << height
      << ",\"maxTrackWidth\":" << GetSystemMetrics(SM_CXMAXTRACK)
      << ",\"maxTrackHeight\":" << GetSystemMetrics(SM_CYMAXTRACK) << "}" << std::endl;
  // The largest unchanged owner is 1120x760 DIP at 1.5x = 1680x1140 px.
  return ok && width >= 1680 && height >= 1140;
}

}  // namespace

int wmain(int argc, wchar_t* argv[]) {
  wchar_t actions[6]{};
  GetEnvironmentVariableW(L"GITHUB_ACTIONS", actions, 6);
  if (argc != 2 || std::wstring(actions) != L"true") {
    std::cerr << "Display setup is restricted to isolated GitHub Actions" << std::endl;
    return 2;
  }
  std::ofstream evidence{std::filesystem::path(argv[1])};
  if (Snapshot(evidence, "before")) return 0;

  DEVMODEW current{};
  current.dmSize = sizeof(current);
  if (!EnumDisplaySettingsW(nullptr, ENUM_CURRENT_SETTINGS, &current)) {
    evidence << "{\"event\":\"current-display-mode-unavailable\",\"error\":"
             << GetLastError() << "}" << std::endl;
    return 1;
  }
  DEVMODEW chosen{};
  uint64_t chosen_area = UINT64_MAX;
  for (DWORD index = 0;; ++index) {
    DEVMODEW mode{};
    mode.dmSize = sizeof(mode);
    if (!EnumDisplaySettingsW(nullptr, index, &mode)) break;
    evidence << "{\"event\":\"supported-mode\",\"width\":" << mode.dmPelsWidth
             << ",\"height\":" << mode.dmPelsHeight << ",\"bpp\":" << mode.dmBitsPerPel
             << ",\"frequency\":" << mode.dmDisplayFrequency << "}" << std::endl;
    const uint64_t area = static_cast<uint64_t>(mode.dmPelsWidth) * mode.dmPelsHeight;
    if (mode.dmPelsWidth >= 1920 && mode.dmPelsHeight >= 1200 &&
        mode.dmBitsPerPel == current.dmBitsPerPel && area < chosen_area) {
      chosen = mode;
      chosen_area = area;
    }
  }
  if (!chosen.dmSize) {
    evidence << "{\"event\":\"no-supported-mode-for-original-owner\"}" << std::endl;
    return 1;
  }
  const LONG test = ChangeDisplaySettingsExW(nullptr, &chosen, nullptr, CDS_TEST, nullptr);
  evidence << "{\"event\":\"display-mode-test\",\"width\":" << chosen.dmPelsWidth
           << ",\"height\":" << chosen.dmPelsHeight << ",\"result\":" << test << "}" << std::endl;
  if (test != DISP_CHANGE_SUCCESSFUL) return 1;
  // Flags=0 applies the enumerated mode for this disposable session only.
  const LONG applied = ChangeDisplaySettingsExW(nullptr, &chosen, nullptr, 0, nullptr);
  evidence << "{\"event\":\"display-mode-apply\",\"result\":" << applied << "}" << std::endl;
  const bool fits = Snapshot(evidence, "after");
  return applied == DISP_CHANGE_SUCCESSFUL && fits ? 0 : 1;
}
