using System.Diagnostics;
using System.Drawing;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Windows.Forms;

namespace AnimaPromptStudioLauncher;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        var port = ParsePort(args);
        var openBrowser = !args.Any(a => a.Equals("--no-browser", StringComparison.OrdinalIgnoreCase));
        Application.Run(new TrayContext(port, openBrowser));
    }

    private static int ParsePort(string[] args)
    {
        for (var i = 0; i + 1 < args.Length; i++)
            if (args[i].Equals("--port", StringComparison.OrdinalIgnoreCase) && int.TryParse(args[i + 1], out var port) && port is >= 1 and <= 65535)
                return port;
        return 8191;
    }
}

internal sealed class TrayContext : ApplicationContext
{
    private readonly int _port;
    private readonly string _root;
    private readonly string _logPath;
    private readonly string _pidPath;
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(1) };
    private readonly NotifyIcon _tray;
    private readonly ToolStripMenuItem _status;
    private readonly ToolStripMenuItem _start;
    private readonly ToolStripMenuItem _stop;
    private readonly System.Windows.Forms.Timer _timer;
    private Process? _server;
    private Process? _worker;
    private bool _busy;

    public TrayContext(int port, bool openBrowserOnStart)
    {
        _port = port;
        _root = FindProjectRoot();
        var data = Path.Combine(_root, "data");
        Directory.CreateDirectory(data);
        _logPath = Path.Combine(data, "launcher.log");
        _pidPath = Path.Combine(data, "launcher-exe.pid");

        _status = new ToolStripMenuItem("状态：检查中") { Enabled = false };
        var open = new ToolStripMenuItem("打开工作台", null, (_, _) => OpenBrowser());
        _start = new ToolStripMenuItem("启动服务", null, async (_, _) => await StartServiceAsync(true));
        _stop = new ToolStripMenuItem("停止服务", null, async (_, _) => await StopServiceAsync());
        var exit = new ToolStripMenuItem("退出启动器", null, (_, _) => ExitThread());
        var menu = new ContextMenuStrip();
        menu.Items.AddRange([_status, new ToolStripSeparator(), open, _start, _stop, new ToolStripSeparator(), exit]);

        _tray = new NotifyIcon { Icon = LoadIcon(), Text = "Prompt Workbench v7", Visible = true, ContextMenuStrip = menu };
        _tray.DoubleClick += (_, _) => OpenBrowser();
        _timer = new System.Windows.Forms.Timer { Interval = 3000 };
        _timer.Tick += async (_, _) => await RefreshStateAsync(false);
        _timer.Start();
        _ = InitializeAsync(openBrowserOnStart);
    }

    private async Task InitializeAsync(bool openBrowser)
    {
        await RefreshStateAsync(false);
        if (await IsHealthyAsync()) StartWorker();
        else await StartServiceAsync(openBrowser);
    }

    private async Task<bool> IsHealthyAsync()
    {
        try { using var response = await _http.GetAsync($"http://127.0.0.1:{_port}/api/status"); return response.StatusCode == HttpStatusCode.OK; }
        catch (HttpRequestException) { return false; }
        catch (TaskCanceledException) { return false; }
    }

    private async Task RefreshStateAsync(bool notify)
    {
        var running = await IsHealthyAsync();
        SetState(running ? "状态：运行中" : "状态：未运行", true, running);
        if (running) StartWorker();
        if (notify) _tray.ShowBalloonTip(1800, "Anima Prompt Studio", running ? "工作台正在运行" : "工作台已停止", ToolTipIcon.Info);
    }

    private async Task StartServiceAsync(bool openBrowser)
    {
        if (_busy) return;
        if (await IsHealthyAsync()) { StartWorker(); if (openBrowser) OpenBrowser(); return; }
        _busy = true;
        SetState("状态：启动中", false, false);
        try
        {
            var (python, prefix) = FindPython();
            _server = StartPython(python, $"{prefix}-m uvicorn backend.app:app --host 127.0.0.1 --port {_port}");
            File.WriteAllText(_pidPath, _server.Id.ToString());
            AppendLog($"started uvicorn pid={_server.Id} port={_port}");
            StartWorker(python, prefix);
            for (var i = 0; i < 60; i++)
            {
                await Task.Delay(500);
                if (await IsHealthyAsync())
                {
                    SetState("状态：运行中", true, true);
                    if (openBrowser) OpenBrowser();
                    _tray.ShowBalloonTip(2200, "Anima Prompt Studio", "工作台已启动", ToolTipIcon.Info);
                    return;
                }
                if (_server.HasExited) break;
            }
            throw new InvalidOperationException("工作台未能在 30 秒内就绪");
        }
        catch (Exception ex)
        {
            AppendLog("start failed: " + ex.Message);
            KillTrackedProcesses();
            SetState("状态：启动失败", true, false);
            _tray.ShowBalloonTip(3500, "Anima Prompt Studio", ex.Message, ToolTipIcon.Error);
        }
        finally { _busy = false; }
    }

    private (string Path, string Prefix) FindPython()
    {
        var py = FindOnPath("py.exe");
        if (py is not null) return (py, "-3.11 ");
        var venv = System.IO.Path.Combine(_root, ".venv", "Scripts", "python.exe");
        if (File.Exists(venv)) return (venv, "");
        var python = FindOnPath("python.exe");
        if (python is not null) return (python, "");
        throw new InvalidOperationException("找不到 py.exe、python.exe 或项目 .venv");
    }

    private void StartWorker(string? pythonOverride = null, string? prefixOverride = null)
    {
        if (_worker is { HasExited: false }) return;
        try
        {
            var (python, prefix) = pythonOverride is null ? FindPython() : (pythonOverride, prefixOverride ?? "");
            _worker = StartPython(python, $"{prefix}-m backend.worker");
            AppendLog($"started worker pid={_worker.Id}");
        }
        catch (Exception ex) { AppendLog("worker start failed: " + ex.Message); }
    }

    private Process StartPython(string python, string arguments)
    {
        var info = new ProcessStartInfo(python, arguments) { WorkingDirectory = _root, UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true, StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8 };
        var process = Process.Start(info) ?? throw new InvalidOperationException("无法启动 Python 服务");
        process.OutputDataReceived += (_, e) => AppendLog(e.Data);
        process.ErrorDataReceived += (_, e) => AppendLog(e.Data);
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        return process;
    }

    private async Task StopServiceAsync()
    {
        if (_busy) return;
        _busy = true;
        SetState("状态：停止中", false, false);
        try
        {
            KillTrackedProcesses();
            StopExternalProjectProcesses();
            for (var i = 0; i < 20 && await IsHealthyAsync(); i++) await Task.Delay(250);
            if (File.Exists(_pidPath)) File.Delete(_pidPath);
            AppendLog($"stopped port={_port}");
            SetState("状态：已停止", true, false);
            _tray.ShowBalloonTip(1800, "Anima Prompt Studio", "工作台已停止", ToolTipIcon.Info);
        }
        catch (Exception ex) { AppendLog("stop failed: " + ex.Message); SetState("状态：停止失败", true, true); }
        finally { _busy = false; }
    }

    private void StopExternalProjectProcesses()
    {
        using var ps = new Process { StartInfo = new ProcessStartInfo("powershell.exe", $"-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command \"$c=Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort {_port} -State Listen -ErrorAction SilentlyContinue; foreach($x in $c) {{ $w=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $x.OwningProcess); if($w.CommandLine -match 'backend\\.app:app|uvicorn') {{ Stop-Process -Id $x.OwningProcess -Force -ErrorAction SilentlyContinue }} }}\"") { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true } };
        try { ps.Start(); ps.WaitForExit(5000); } catch (Exception ex) { AppendLog("external stop failed: " + ex.Message); }
    }

    private void KillTrackedProcesses()
    {
        foreach (var process in new[] { _server, _worker })
        {
            try { if (process is { HasExited: false }) process.Kill(entireProcessTree: true); } catch { }
            process?.Dispose();
        }
        _server = null; _worker = null;
    }

    private void SetState(string text, bool enabled, bool running) { _status.Text = text; _start.Enabled = enabled && !running; _stop.Enabled = enabled && running; }
    private void OpenBrowser() => Process.Start(new ProcessStartInfo($"http://127.0.0.1:{_port}") { UseShellExecute = true });
    private void AppendLog(string? line) { if (!string.IsNullOrWhiteSpace(line)) try { File.AppendAllText(_logPath, $"{DateTime.Now:s} {line}{Environment.NewLine}"); } catch { } }
    private static Icon LoadIcon() { try { return Icon.ExtractAssociatedIcon(Application.ExecutablePath) ?? SystemIcons.Application; } catch { return SystemIcons.Application; } }

    private string FindProjectRoot()
    {
        var current = new DirectoryInfo(AppContext.BaseDirectory);
        for (var i = 0; i < 8 && current is not null; i++, current = current.Parent)
            if (File.Exists(Path.Combine(current.FullName, "backend", "app.py"))) return current.FullName;
        current = new DirectoryInfo(Environment.CurrentDirectory);
        for (var i = 0; i < 8 && current is not null; i++, current = current.Parent)
            if (File.Exists(Path.Combine(current.FullName, "backend", "app.py"))) return current.FullName;
        return AppContext.BaseDirectory;
    }

    private static string? FindOnPath(string name)
    {
        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            var candidate = Path.Combine(dir.Trim(), name);
            if (File.Exists(candidate)) return candidate;
        }
        return null;
    }

    protected override void ExitThreadCore() { _timer.Stop(); _tray.Visible = false; _tray.Dispose(); _http.Dispose(); base.ExitThreadCore(); }
}
