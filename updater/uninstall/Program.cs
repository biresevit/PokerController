using System.Diagnostics;
using System.Text.Json;

namespace PokerSuite.Uninstall;

/// <summary>
/// Removes PokerSuite from a PC: the updater's sign-in task, the running PokerSuite processes (only those started
/// from the install folder, never a developer's own copy) and the C:\PokerSuite and %LOCALAPPDATA%\PokerSuite
/// folders. Microsoft prerequisites (.NET, LocalDB, ODBC, Visual C++) stay: other programs may use them.
///
/// Options (for scripting and tests): --console (no window), --yes (no questions), --dry-run (only report),
/// --root, --local and --task-prefix override the install folder, Setup's folder and the task name.
/// </summary>
static class Program
{
    [STAThread]
    static int Main(string[] args)
    {
        var options = Options.Parse(args);
        if (options.Console)
        {
            int seats = Uninstaller.ActiveSeats(options.Root);
            if (seats > 0 && !options.Yes)
            {
                Console.WriteLine(Uninstaller.SeatWarning(seats) + " Run again with --yes to uninstall anyway.");
                return 2;
            }
            return new Uninstaller(options, Console.WriteLine).Run() ? 0 : 1;
        }
        ApplicationConfiguration.Initialize();
        Application.Run(new UninstallForm(options));
        return 0;
    }
}

sealed record Options(string Root, string Local, string TaskPrefix, bool DryRun, bool Yes, bool Console)
{
    public static Options Parse(string[] args)
    {
        string Value(string name, string fallback)
        {
            int i = Array.IndexOf(args, name);
            return i >= 0 && i + 1 < args.Length ? args[i + 1] : fallback;
        }
        return new Options(
            Path.GetFullPath(Value("--root", @"C:\PokerSuite")),
            Path.GetFullPath(Value("--local", Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PokerSuite"))),
            Value("--task-prefix", "PokerSuite Updater"),
            args.Contains("--dry-run"), args.Contains("--yes"), args.Contains("--console"));
    }
}

sealed class Uninstaller(Options options, Action<string> log)
{
    // The updater first: it restarts the applications it supervises.
    static readonly string[] ProcessNames = ["PokerSuite.Updater", "PokerController", "PDecision"];
    static readonly string[] Playing = ["seated", "sitting_out", "joining", "draining", "reserved"];

    /// <summary>Bots the worker reported at tables within the last ten minutes (status.json of every site).</summary>
    public static int ActiveSeats(string root)
    {
        var runs = Path.Combine(root, "Managed", "runs");
        if (!Directory.Exists(runs)) return 0;
        int total = 0;
        foreach (var file in Directory.EnumerateFiles(runs, "status.json", SearchOption.AllDirectories))
        {
            try
            {
                using var json = JsonDocument.Parse(File.ReadAllText(file));
                var root_ = json.RootElement;
                if (!root_.TryGetProperty("at", out var at) || !DateTimeOffset.TryParse(at.GetString(), out var stamp) ||
                    stamp < DateTimeOffset.UtcNow.AddMinutes(-10)) continue;
                if (!root_.TryGetProperty("entries", out var entries) || entries.ValueKind != JsonValueKind.Object) continue;
                foreach (var name in Playing)
                    if (entries.TryGetProperty(name, out var n) && n.TryGetInt32(out int count)) total += count;
            }
            catch (Exception) { }
        }
        return total;
    }

    public static string SeatWarning(int seats) =>
        $"{seats} bot entr{(seats == 1 ? "y is" : "ies are")} still at poker tables on this PC. Disable this PC on the " +
        "backoffice Machines page and wait until it shows 0 entries, otherwise the bots are left at their tables " +
        "until the poker site removes them.";

    public bool Run()
    {
        var self = Environment.ProcessPath ?? "";
        if (Under(self, options.Root))
        {
            log($"Run the uninstaller from outside {options.Root} (for example from Downloads).");
            return false;
        }
        log(options.DryRun ? "Dry run: nothing is changed." : "Removing PokerSuite.");
        bool ok = RemoveTasks();
        ok &= StopProcesses();
        ok &= Delete(options.Root);
        ok &= Delete(options.Local);
        log(!ok ? "Finished with problems; see the lines above." : options.DryRun ? "Dry run finished." : "PokerSuite was removed.");
        return ok;
    }

    bool RemoveTasks()
    {
        var prefix = options.TaskPrefix.Replace("'", "''");
        var list = Pwsh($"Get-ScheduledTask -TaskName '{prefix}*' -ErrorAction SilentlyContinue | ForEach-Object {{ $_.TaskName + '|' + (($_.Actions | ForEach-Object {{ $_.Execute }}) -join ';') }}");
        // Only a task that starts a program inside the folder being removed: never another install's task.
        var names = list.Output.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Select(line => line.Split('|', 2))
            .Where(parts => parts.Length == 2 && parts[1].Split(';').Any(exe => exe.Length > 0 && Under(exe.Trim('"'), options.Root)))
            .Select(parts => parts[0]).ToArray();
        if (names.Length == 0) { log("No PokerSuite sign-in task found."); return true; }
        bool ok = true;
        foreach (var name in names)
        {
            if (options.DryRun) { log($"Would remove the sign-in task '{name}'."); continue; }
            var result = Pwsh($"Unregister-ScheduledTask -TaskName '{name.Replace("'", "''")}' -Confirm:$false");
            log(result.Code == 0 ? $"Removed the sign-in task '{name}'." : $"Could not remove the sign-in task '{name}': {result.Error.Trim()}");
            ok &= result.Code == 0;
        }
        return ok;
    }

    bool StopProcesses()
    {
        bool ok = true;
        foreach (var name in ProcessNames)
        {
            foreach (var process in Process.GetProcessesByName(name))
            {
                string? path = null;
                try { path = process.MainModule?.FileName; } catch (Exception) { }
                if (path == null || !Under(path, options.Root)) continue;  // never a copy outside the install folder
                if (options.DryRun) { log($"Would stop {name} ({process.Id})."); continue; }
                try
                {
                    process.Kill(entireProcessTree: true);
                    process.WaitForExit(15000);
                    log($"Stopped {name} ({process.Id}).");
                }
                catch (Exception error)
                {
                    log($"Could not stop {name} ({process.Id}): {error.Message}");
                    ok = false;
                }
            }
        }
        return ok;
    }

    bool Delete(string folder)
    {
        if (!Directory.Exists(folder)) { log($"{folder} is not there."); return true; }
        if (options.DryRun) { log($"Would delete {folder}."); return true; }
        for (int attempt = 1; ; attempt++)
        {
            try
            {
                foreach (var file in Directory.EnumerateFiles(folder, "*", SearchOption.AllDirectories))
                    File.SetAttributes(file, FileAttributes.Normal);
                Directory.Delete(folder, recursive: true);
                log($"Deleted {folder}.");
                return true;
            }
            catch (Exception error) when (attempt < 5)
            {
                Thread.Sleep(2000);  // a stopped process may still hold a file for a moment
                _ = error;
            }
            catch (Exception error)
            {
                log($"Could not delete everything in {folder}: {error.Message} Close programs using it and run the uninstaller again.");
                return false;
            }
        }
    }

    static bool Under(string path, string folder)
    {
        string full;
        try { full = Path.GetFullPath(Environment.ExpandEnvironmentVariables(path)); } catch (Exception) { return false; }
        var parent = Path.GetFullPath(folder).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        return full.StartsWith(parent, StringComparison.OrdinalIgnoreCase);
    }

    static (int Code, string Output, string Error) Pwsh(string command)
    {
        var info = new ProcessStartInfo("powershell.exe") { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
        foreach (var arg in new[] { "-NoProfile", "-NonInteractive", "-Command", command }) info.ArgumentList.Add(arg);
        using var process = Process.Start(info)!;
        var output = process.StandardOutput.ReadToEndAsync();
        var error = process.StandardError.ReadToEndAsync();
        process.WaitForExit();
        return (process.ExitCode, output.Result, error.Result);
    }
}

sealed class UninstallForm : Form
{
    readonly Options options;
    readonly TextBox output = new() { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill };
    readonly Button uninstall = new() { Text = "Uninstall", AutoSize = true };
    readonly Button close = new() { Text = "Close", AutoSize = true };

    public UninstallForm(Options options)
    {
        this.options = options;
        Text = "Uninstall PokerSuite";
        Width = 720; Height = 460;
        var intro = new Label
        {
            AutoSize = true, MaximumSize = new Size(680, 0), Padding = new Padding(8),
            Text = $"Removes the PokerSuite bots from this PC: the updater's sign-in task, the running PokerSuite " +
                   $"programs, {options.Root} and {options.Local}. Microsoft components installed by Setup (.NET, " +
                   "LocalDB, ODBC, Visual C++) stay; remove them in Settings > Apps if nothing else needs them.\r\n\r\n" +
                   "First disable this PC on the backoffice Machines page and wait until it shows 0 entries.",
        };
        var buttons = new FlowLayoutPanel { Dock = DockStyle.Bottom, AutoSize = true, FlowDirection = FlowDirection.RightToLeft, Padding = new Padding(8) };
        buttons.Controls.Add(close);
        buttons.Controls.Add(uninstall);
        intro.Dock = DockStyle.Top;
        Controls.Add(output);
        Controls.Add(buttons);
        Controls.Add(intro);
        close.Click += (_, _) => Close();
        uninstall.Click += async (_, _) => await Start();
    }

    async Task Start()
    {
        int seats = Uninstaller.ActiveSeats(options.Root);
        if (seats > 0 && MessageBox.Show(this, Uninstaller.SeatWarning(seats) + "\r\n\r\nUninstall anyway?", Text,
                MessageBoxButtons.YesNo, MessageBoxIcon.Warning, MessageBoxDefaultButton.Button2) != DialogResult.Yes)
            return;
        if (seats == 0 && MessageBox.Show(this, "Remove PokerSuite from this PC?", Text, MessageBoxButtons.YesNo,
                MessageBoxIcon.Question) != DialogResult.Yes)
            return;
        uninstall.Enabled = close.Enabled = false;
        void Log(string line) => BeginInvoke(() => output.AppendText(line + Environment.NewLine));
        bool ok = await Task.Run(() => new Uninstaller(options, Log).Run());
        close.Enabled = true;
        uninstall.Enabled = !ok;
    }
}
