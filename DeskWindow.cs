using System;
using System.Drawing;
using System.IO;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

[assembly: System.Runtime.Versioning.TargetFramework(".NETFramework,Version=v4.8", FrameworkDisplayName = ".NET Framework 4.8")]

internal sealed class DeskWindow : Form
{
    [DllImport("user32.dll")]
    private static extern uint GetDpiForSystem();

    private static readonly HttpClient Client = new HttpClient(new HttpClientHandler {
        UseProxy = false, AllowAutoRedirect = false
    }) { Timeout = TimeSpan.FromSeconds(2) };

    private readonly Uri dashboard;
    private readonly string profile;
    private readonly WebView2 view = new WebView2 { Dock = DockStyle.Fill, Visible = false };
    private readonly Label status = new Label {
        Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter, Text = "正在打开本机看板…"
    };
    private readonly Timer healthTimer = new Timer { Interval = 2000 };
    private bool checking;
    private int failures;

    [STAThread]
    private static int Main(string[] args)
    {
        Uri address;
        if (args.Length != 2 ||
            !Uri.TryCreate(args[0], UriKind.Absolute, out address) ||
            address.Scheme != "http" || address.Host != "127.0.0.1" || !String.IsNullOrEmpty(address.UserInfo) ||
            !Regex.IsMatch(address.AbsolutePath, "^/[A-Za-z0-9_-]{32}/$") ||
            !String.IsNullOrEmpty(address.Query) || !String.IsNullOrEmpty(address.Fragment))
            return 2;

        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new DeskWindow(address, args[1]));
        return 0;
    }

    private DeskWindow(Uri address, string profilePath)
    {
        dashboard = address;
        profile = profilePath;
        Text = "Agent Usage Desk";
        Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        StartPosition = FormStartPosition.CenterScreen;
        AutoScaleDimensions = new SizeF(96F, 96F);
        AutoScaleMode = AutoScaleMode.Dpi;
        var bounds = Screen.PrimaryScreen.WorkingArea;
        var scale = Math.Max(1.0, GetDpiForSystem() / 96.0);
        int width = (int)(bounds.Width / scale);
        int height = (int)(bounds.Height / scale);
        MinimumSize = new Size(Math.Min(600, width - 24), Math.Min(450, height - 24));
        ClientSize = new Size(Math.Min(1360, width - 48), Math.Min(890, height - 72));
        BackColor = Color.FromArgb(245, 246, 241);
        status.Font = new Font("Microsoft YaHei UI", 12);
        Controls.Add(view);
        Controls.Add(status);
        Shown += OpenDashboard;
        healthTimer.Tick += CheckBackend;
    }

    private async void OpenDashboard(object sender, EventArgs args)
    {
        try
        {
            Directory.CreateDirectory(profile);
            var options = new CoreWebView2EnvironmentOptions {
                AdditionalBrowserArguments = "--disable-background-networking --disable-component-update --no-first-run"
            };
            var environment = await CoreWebView2Environment.CreateAsync(null, profile, options);
            if (IsDisposed) return;
            await view.EnsureCoreWebView2Async(environment);
            if (IsDisposed) return;
            var core = view.CoreWebView2;
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.AreBrowserAcceleratorKeysEnabled = false;
            core.Settings.AreHostObjectsAllowed = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.IsZoomControlEnabled = false;
            core.Settings.IsPasswordAutosaveEnabled = false;
            core.Settings.IsGeneralAutofillEnabled = false;
            core.NavigationStarting += (source, e) => { if (!InsideDashboard(e.Uri)) e.Cancel = true; };
            core.NewWindowRequested += (source, e) => { e.Handled = true; };
            core.PermissionRequested += (source, e) => { e.State = CoreWebView2PermissionState.Deny; };
            core.WebMessageReceived += (source, e) => {
                if (InsideDashboard(e.Source) && e.WebMessageAsJson == "\"quit\"")
                    BeginInvoke((Action)Close);
            };
            core.NavigationCompleted += (source, e) => {
                if (e.IsSuccess) {
                    status.Visible = false;
                    view.Visible = true;
                    view.BringToFront();
                    healthTimer.Start();
                } else {
                    status.Text = "暂时无法打开本机看板。请关闭此窗口后重新启动。";
                }
            };
            core.Navigate(dashboard.AbsoluteUri);
        }
        catch (WebView2RuntimeNotFoundException)
        {
            if (!IsDisposed) status.Text = "缺少 Microsoft WebView2 Runtime。安装后可重新打开应用。";
        }
        catch (Exception)
        {
            if (!IsDisposed) status.Text = "Windows 窗口初始化失败。请关闭此窗口后重新启动。";
        }
    }

    private bool InsideDashboard(string target)
    {
        Uri parsed;
        return Uri.TryCreate(target, UriKind.Absolute, out parsed)
            && parsed.Scheme == dashboard.Scheme && parsed.Host == dashboard.Host
            && parsed.Port == dashboard.Port && String.IsNullOrEmpty(parsed.UserInfo)
            && parsed.AbsolutePath.StartsWith(dashboard.AbsolutePath, StringComparison.Ordinal);
    }

    private async void CheckBackend(object sender, EventArgs args)
    {
        if (checking) return;
        checking = true;
        try
        {
            using (var reply = await Client.GetAsync(new Uri(dashboard, "api/status")))
                failures = reply.IsSuccessStatusCode ? 0 : failures + 1;
        }
        catch (HttpRequestException) { failures++; }
        catch (TaskCanceledException) { failures++; }
        finally { checking = false; }
        if (failures >= 2 && !IsDisposed) Close();
    }

    protected override void OnFormClosed(FormClosedEventArgs e)
    {
        healthTimer.Dispose();
        base.OnFormClosed(e);
    }
}
