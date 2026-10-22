using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

class QuantLabLauncher {
    // Windows CommandLineToArgvW-compatible quoting, including trailing backslashes.
    static string Quote(string value) {
        var b = new System.Text.StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value) {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') { b.Append('\\', slashes * 2 + 1); b.Append(c); }
            else { b.Append('\\', slashes); b.Append(c); }
            slashes = 0;
        }
        b.Append('\\', slashes * 2); b.Append('"');
        return b.ToString();
    }
    [STAThread] static int Main(string[] args) {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        bool diagnose = Array.IndexOf(args, "--diagnose") >= 0;
        string python = Path.Combine(root, "runtime", diagnose ? "python.exe" : "pythonw.exe");
        string script = Path.Combine(root, "app", "start_desktop.py");
        try {
            var info = new ProcessStartInfo(python, Quote(script));
            foreach (string arg in args) info.Arguments += " " + Quote(arg);
            info.UseShellExecute = false;
            info.WorkingDirectory = root;
            info.CreateNoWindow = !diagnose;
            info.EnvironmentVariables.Remove("PYTHONHOME");
            info.EnvironmentVariables.Remove("PYTHONPATH");
            var child = Process.Start(info);
            if (diagnose) { child.WaitForExit(); return child.ExitCode; }
            return 0;
        } catch (Exception ex) {
            MessageBox.Show("QuantLab 无法启动：\n" + ex.Message, "QuantLab");
            return 1;
        }
    }
}
