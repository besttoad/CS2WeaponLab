using System.Collections.ObjectModel;
using Avalonia.Controls;
using Avalonia.Interactivity;
using Avalonia.Platform.Storage;
using WeaponLab.Core.Blender;
using WeaponLab.Core.Delivery;
using WeaponLab.Core.Game;
using WeaponLab.Core.Profiles;
using WeaponLab.Core.Sandbox;
using WeaponLab.Core.VData;
using WeaponLab.Core.Weapons;

namespace WeaponLab.App;

/// <summary>
/// The main window. The outliner on the left selects a weapon; every tool
/// tab (Animations, VData, Sounds, Graph, Deliver) and the weapon panel on
/// the right then work on that weapon within the active profile. The code is
/// split into partial classes per area (MainWindow.*.cs).
/// </summary>
public sealed partial class MainWindow : Window
{
    private const int AnimationsTool = 0;
    // Tab order (user 2026-11-07): Animations | Anim Events | Graph | VData | Deliver.
    private const int SoundsTool = 1;
    private const int GraphTool = 2;
    private const int VDataTool = 3;
    private const int DeliverTool = 4;

    private SandboxStore _sandboxes;
    private readonly ProfileStore _profiles;
    private readonly ObservableCollection<HistoryEntry> _history = [];

    private Profile _profile;
    private VDataOverrides _vdataOverrides;
    private GameConnection? _connection;
    private BlenderInstall? _blender;
    private WeaponEntry? _weapon;

    // While true, control events caused by code (not the user) are ignored.
    private bool _updatingControls;

    public MainWindow()
    {
        InitializeComponent();
        // The build's version in the title bar, so a test build is easy to tell apart.
        if (typeof(MainWindow).Assembly.GetName().Version is { } version)
        {
            Title = $"CS2 Weapon Lab {version.ToString(3)}";
        }

        HistoryList.ItemsSource = _history;

        _profiles = new ProfileStore(SandboxStore.DefaultWorkspacePath());
        var migrated = _profiles.MigrateLegacy();
        _profile = _profiles.LoadActive();
        NoteProfileWritten();
        _sandboxes = _profiles.SandboxesFor(_profile.Name);
        KeepOldViewportDefaults();
        _vdataOverrides = VDataOverrides.For(_profile, SaveProfile);
        FillProfileCombo();
        if (migrated.Count > 0)
        {
            Log($"Profiles are local workspaces now: moved {string.Join(", ", migrated)}.");
        }

        OnDeliverViewChanged(null, null); // The tree is the default view: fill its legend.
        OutlinerDescription.Text = OutlinerDescriptions[AnimationsTool]; // The first tab is open before any tab change.
        WireViewport();
        WireAnimEvents();

        Activated += async (_, _) => await ReloadProfileIfChangedAsync(); // Blender's Change gun model edits the profile (0.24).
        Closed += (_, _) =>
        {
            _exportWatcher?.Dispose();
            _connection?.Dispose();
        };
        WatchExports();

        Log($"Workspace: {_sandboxes.WorkspacePath}. Profile: {_profile.Name}.");
        UpdateSuggestion();
        _ = LocateBlenderAsync();
    }

    // ---- Connection ----

    private async void OnConnectClicked(object? sender, RoutedEventArgs e)
    {
        await ConnectAsync(installPath: null);
    }

    private async void OnBrowseClicked(object? sender, RoutedEventArgs e)
    {
        var folders = await StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions
        {
            Title = $"Choose the '{GameInstall.FolderName}' folder",
            AllowMultiple = false,
        });

        var path = folders.Count > 0 ? folders[0].TryGetLocalPath() : null;
        if (path is not null)
        {
            await ConnectAsync(path);
        }
    }

    private async Task ConnectAsync(string? installPath)
    {
        SetConnectEnabled(false);
        SetStatus("Connecting to CS2 and scanning the game VPK…");
        try
        {
            var connection = await Task.Run(() =>
            {
                var opened = GameConnection.Open(installPath);
                opened.ReadVData();
                return opened;
            });
            _connection?.Dispose();
            _connection = connection;
            _weaponBlocks.Clear();
            ShowConnection(connection);
            Log($"Connected to {connection.Install.RootPath}: {connection.Weapons.Count} weapons, {connection.Addons.Count} addons.");
            await RefreshStatusAsync();

        }
        catch (Exception error) when (error is GameNotFoundException or IOException or InvalidDataException or UnauthorizedAccessException)
        {
            Log(error.Message);
        }
        finally
        {
            SetConnectEnabled(true);
        }
    }

    private void ShowConnection(GameConnection connection)
    {
        var tools = connection.Install.HasWorkshopTools ? "Workshop Tools" : "no Workshop Tools";
        GameText.Text = $"CS2 {connection.Version?.PatchVersion ?? "unknown build"} · {tools}";
        ToolTip.SetTip(GameText, connection.Install.RootPath);

        _updatingControls = true;
        AddonCombo.ItemsSource = connection.Addons;
        AddonCombo.SelectedItem = connection.Addons.FirstOrDefault(addon => addon.Name == _profile.TargetAddon)
            ?? connection.Addons.FirstOrDefault(addon => addon.Name == "weapon_template");
        AddonCombo.PlaceholderText = "Choose an addon";
        AddonCombo.IsEnabled = true;
        _updatingControls = false;
        FilterBox.IsEnabled = true;
        RebuildTree();
        RefreshWeaponActions();
        RefreshCurrentTool();
    }

    private AddonTarget? Target()
    {
        return _connection is not null && AddonCombo.SelectedItem is AddonInfo addon ? AddonTarget.For(_connection.Install, addon) : null;
    }

    // ---- Tools ----

    private void OnToolChanged(object? sender, SelectionChangedEventArgs e)
    {
        if (AnimationView is null)
        {
            return; // Fired while the window is still loading.
        }

        var tool = ToolTabs.SelectedIndex;
        AnimationView.IsVisible = tool == AnimationsTool;
        VDataPanel.IsVisible = tool == VDataTool;
        SoundsView.IsVisible = tool == SoundsTool;
        GraphView.IsVisible = tool == GraphTool;
        DeliverView.IsVisible = tool == DeliverTool;
        SkeletonPanel.IsVisible = tool == AnimationsTool;
        ArrangeForVData(tool == VDataTool);
        PlaceViewport(tool);
        OutlinerDescription.Text = OutlinerDescriptions.GetValueOrDefault(tool, "");
        ApplyStatusToTree(); // Each tab colours the outliner by its own part of the profile.
        RefreshCurrentTool();
    }

    /// <summary>What the outliner's rows are on each tab (user feedback 2026-11-07: the tree is the ANIMATION SLOT).</summary>
    private static readonly Dictionary<int, string> OutlinerDescriptions = new()
    {
        [AnimationsTool] = "Animation slots. CS2 picks a weapon's first-person animations by its anim class, so every weapon with that class plays these clips. Edit them in Blender; push them in Deliver.",
        [SoundsTool] = "Animation slots and their clips. Pick a clip to edit its sounds and gameplay events; yellow clips have event changes not pushed yet.",
        [GraphTool] = "Animation slots and the graphs their clips play through. CS2's root viewmodel graph picks a slot's graph by anim class; the addon's _custom copy of a graph starts from CS2's and carries your edits.",
        [DeliverTool] = "Animation slots with work for the addon. Yellow: in the profile, not in the addon yet; green: the addon has it.",
    };

    private GridLength _outlinerWidth = new(260);

    /// <summary>The VData tab has no outliner (it scrolls through every block); its info panel sits above a small History.</summary>
    private void ArrangeForVData(bool vdata)
    {
        var outliner = MainColumns.ColumnDefinitions[0];
        if (vdata && outliner.Width.Value > 0)
        {
            _outlinerWidth = outliner.Width;
        }

        outliner.Width = vdata ? new GridLength(0) : _outlinerWidth;
        MainColumns.ColumnDefinitions[1].Width = new GridLength(vdata ? 0 : 4);
        OutlinerPanel.IsVisible = !vdata;
        OutlinerSplitter.IsVisible = !vdata;
        VDataInfoPanel.IsVisible = vdata;
        RightColumn.RowDefinitions[0].Height = vdata ? new GridLength(1, GridUnitType.Star) : GridLength.Auto;
        RightColumn.RowDefinitions[1].Height = vdata ? new GridLength(150) : new GridLength(1, GridUnitType.Star);
    }

    /// <summary>The tabs that show the viewport (Anim Events borrows the Animations tab's).</summary>
    private bool ViewportShown => ToolTabs.SelectedIndex is AnimationsTool or SoundsTool;

    /// <summary>Shows the selected weapon in whichever tool tab is open.</summary>
    private void RefreshCurrentTool()
    {
        switch (ToolTabs.SelectedIndex)
        {
            case VDataTool:
                RefreshVData();
                break;
            case SoundsTool:
                RefreshViewport();
                _ = RefreshAnimEventsAsync();
                break;
            case GraphTool:
                _ = RefreshGraphAsync();
                break;
            case AnimationsTool:
                RefreshSkeletonPanel();
                RefreshViewport();
                break;
            case DeliverTool:
                RefreshDeliverView();
                break;
        }
    }

    // ---- Blender ----

    private async Task LocateBlenderAsync()
    {
        _blender = await Task.Run(() => BlenderLocator.Locate(WeaponLab.Core.Settings.AppSettings.Load().BlenderExecutablePath));
        if (_blender is null)
        {
            BlenderText.Text = "Blender not found";
            Log($"No Blender {BlenderLocator.Minimum} or newer found. Install it, or set WEAPONLAB_BLENDER to blender.exe.");
        }
        else
        {
            BlenderText.Text = _blender.ToString();
            ToolTip.SetTip(BlenderText, _blender.ExecutablePath);
            Log($"Using {_blender} at {_blender.ExecutablePath}");
        }

        RefreshWeaponActions();
    }

    // ---- Suggested next step ----

    /// <summary>
    /// Makes the one button that is the practical next step glow: Connect,
    /// then for the selected weapon Create sandbox → Build .blend → Open in Blender.
    /// </summary>
    private void UpdateSuggestion()
    {
        Button? suggested = null;
        if (_connection is null)
        {
            suggested = ConnectButton;
        }
        else if (_weapon is not null && !_buildingBlend)
        {
            var sandbox = CurrentSandbox();
            suggested = sandbox is null || !BlendBuilder.HasBlend(sandbox) || BlendBuilder.NeedsUpdate(sandbox) ? BuildBlendButton
                : OpenBlendButton;
        }

        foreach (var button in new[] { ConnectButton, BuildBlendButton, OpenBlendButton })
        {
            button.Classes.Set("suggested", button == suggested && button.IsEnabled);
        }
    }

    // ---- Status and history ----

    private void SetConnectEnabled(bool enabled)
    {
        ConnectButton.IsEnabled = enabled;
        BrowseButton.IsEnabled = enabled;
    }

    /// <summary>Safe from any thread: progress reports from background work land on the UI thread.</summary>
    private void SetStatus(string message)
    {
        if (!Avalonia.Threading.Dispatcher.UIThread.CheckAccess())
        {
            Avalonia.Threading.Dispatcher.UIThread.Post(() => SetStatus(message));
            return;
        }

        StatusText.Text = message;
    }

    private void Log(string message)
    {
        if (!Avalonia.Threading.Dispatcher.UIThread.CheckAccess())
        {
            Avalonia.Threading.Dispatcher.UIThread.Post(() => Log(message));
            return;
        }

        _history.Insert(0, HistoryEntry.Now(message));
        SetStatus(message);
    }
}
