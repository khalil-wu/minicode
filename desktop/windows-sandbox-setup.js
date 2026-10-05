"use strict";

const { execFile } = require("node:child_process");

let setupInFlight = false;

function powershellLiteral(value) {
  return `'${value.replace(/'/g, "''")}'`;
}

async function setupWindowsSandbox({ scriptPath, apiBaseUrl, runtimeToken, shellProcessId,
  powershellCommand, platform = process.platform, execFileProcess = execFile, fetchStatus = fetch }) {
  if (platform !== "win32") return { ok: false, error: "Windows 执行环境初始化仅适用于 Windows 桌面端。" };
  if (setupInFlight) return { ok: false, error: "Windows 执行环境正在初始化，请等待当前操作完成。" };
  setupInFlight = true;
  try {
    let status;
    try {
      const response = await fetchStatus(`${apiBaseUrl}/api/sandbox/status`, {
        headers: { "x-minicode-token": runtimeToken }, cache: "no-store",
      });
      if (!response.ok) return { ok: false, error: `无法读取 Windows 执行环境配置（HTTP ${response.status}），请检查后端连接。` };
      status = await response.json();
    } catch (error) { return { ok: false, error: `无法读取 Windows 执行环境配置：${error.message}` }; }
    if (!status.sandbox_executable) return { ok: false, error: "后端配置的 Windows 隔离组件尚未安装，无法开始初始化。" };
    const quotedPath = (value) => `"${value.replace(/(\\+)$/, "$1$1")}"`;
    const argumentsList = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", quotedPath(scriptPath),
      "-ShellProcessId", String(shellProcessId), "-SandboxHome", quotedPath(status.sandbox_home),
      "-RuntimePath", quotedPath(status.sandbox_executable)].map(powershellLiteral).join(", ");
    const command = `$ErrorActionPreference = 'Stop'
$arguments = @(${argumentsList})
try {
  $setup = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -Verb RunAs -WindowStyle Hidden -Wait -PassThru -ArgumentList $arguments
  exit $setup.ExitCode
} catch {
  $cause = $_.Exception.GetBaseException()
  if ($cause -is [System.ComponentModel.Win32Exception] -and $cause.NativeErrorCode -eq 1223) { exit 1223 }
  [Console]::Error.WriteLine($_.Exception.Message)
  exit 1
}`;
    return await new Promise((resolve) => {
      execFileProcess(powershellCommand, ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", Buffer.from(command, "utf16le").toString("base64")], { windowsHide: true }, (error, _stdout, stderr) => {
        if (!error) resolve({ ok: true });
        else if (error.code === 1223) resolve({ ok: false, cancelled: true });
        else resolve({ ok: false, error: stderr.trim() || (typeof error.code === "number"
          ? `Windows 执行环境初始化未完成（退出码 ${error.code}）。`
          : "Windows 执行环境初始化程序无法启动，请检查桌面运行组件。") });
      });
    });
  } finally { setupInFlight = false; }
}

module.exports = { setupWindowsSandbox };
