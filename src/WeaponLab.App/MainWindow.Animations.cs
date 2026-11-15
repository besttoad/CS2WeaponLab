using System.Diagnostics;
using Avalonia.Controls;
using Avalonia.Interactivity;
using Avalonia.Platform.Storage;
using WeaponLab.Core.Blender;
using WeaponLab.Core.Clips;
using WeaponLab.Core.Delivery;
using WeaponLab.Core.Export;
using WeaponLab.Core.Sandbox;
using WeaponLab.Core.Weapons;

namespace WeaponLab.App;

/// <summary>The Animations tab: clips, events, tracks, exports, sandbox and Blender buttons.</summary>
public sealed partial class MainWindow
{
    private bool _buildingBlend;
    private string? _clipPath;
    private ClipView? _clipView;
    private IReadOnlyList<PropertyGroup> _properties = [];

    private async Task LoadClipAsync(WeaponEntry weapon, string path)
    {
        if (_connection is not { } connection)
        {
            return;
        }

        _clipPath = path;
        LoadViewportClip(path);
        try
        {
            // What the game plays: the addon's own version when it has one (user decision 2026-11-07), else CS2's.
            var target = Target();
            var addonClip = WeaponLab.Core.Addons.AddonInventory.ClipFile(target, _inventory, path);
            var view = await Task.Run(() => ClipPresenter.Load(connection, path, addonClip, target?.GamePath));
            if (_clipPath != path)
            {
                return; // The selection moved on while decoding.
            }

            _clipView = view;
            CenterTitle.Text = $"{weapon.Id} / {ExplorerNode.ClipName(path)}";
            VerifyClipButton.IsEnabled = true;
            var sandbox = CurrentSandbox();
            ShowProperties(sandbox is null
                ? view.Properties
                : [.. view.Properties, ClipPresenter.ExportProperties(ClipExporter.LastExport(sandbox, ExplorerNode.ClipName(path)))]);
            ApplyViewFilter();
        }
        catch (Exception error) when (error is InvalidDataException or IOException)
        {
            ClearClip();
            Log($"Could not read {ExplorerNode.ClipName(path)}: {error.Message}");
        }
    }

    private void ClearClip()
    {
        _clipPath = null;
        _clipView = null;
        VerifyClipButton.IsEnabled = false;
        EventList.ItemsSource = null;
        TrackList.ItemsSource = null;
    }

    private void ShowWeaponProperties(WeaponEntry weapon)
    {
        _showingBone = false;
        var sandbox = _weapon == weapon ? CurrentSandbox() : null;
        var blend = sandbox is not null && BlendBuilder.HasBlend(sandbox) ? BlendBuilder.BlendPath(sandbox) : null;
        var exported = sandbox is null ? 0 : weapon.ClipPaths.Count(path => ClipExporter.LastExport(sandbox, ExplorerNode.ClipName(path)) is not null);
        var blocks = BlocksFor(weapon);
        ShowProperties([WeaponAddonProperties(weapon), .. ClipPresenter.WeaponProperties(weapon, sandbox is not null, _sandboxes.FolderFor(weapon.Id), blend, exported),
            new PropertyGroup("VData", [new PropertyRow("Block", blocks.Count == 0 ? "none found" : string.Join(", ", blocks))])]);
    }

    /// <summary>After a sandbox or .blend changes, refresh the weapon's properties if they are showing.</summary>
    private void RefreshWeaponView(WeaponEntry weapon)
    {
        if (_weapon == weapon && _clipPath is null)
        {
            ShowWeaponProperties(weapon);
        }
    }

    private SandboxInfo? CurrentSandbox()
    {
        if (_weapon is null || !_sandboxes.Exists(_weapon.Id))
        {
            return null;
        }

        try
        {
            return _sandboxes.Open(_weapon.Id);
        }
        catch (Exception error) when (error is IOException or InvalidDataException or System.Text.Json.JsonException)
        {
            return null;
        }
    }

    /// <summary>Enables the sandbox and Blender buttons for the selected weapon's state.</summary>
    private void RefreshWeaponActions()
    {
        var sandbox = CurrentSandbox();
        var hasBlend = sandbox is not null && BlendBuilder.HasBlend(sandbox);
        var needsUpdate = sandbox is not null && BlendBuilder.NeedsUpdate(sandbox);
        var buildable = sandbox is not null && sandbox.Manifest.Clips.Any(clip => clip.Editable);
        OpenSandboxButton.IsEnabled = sandbox is not null;
        BuildBlendButton.IsEnabled = _weapon is not null && (sandbox is null || buildable) && _blender is not null && _connection is not null && !_buildingBlend;
        ToolTip.SetTip(BuildBlendButton,
            sandbox is not null && !buildable ? "This weapon has no clips on the viewmodel hand rig, so there is nothing to build."
            : needsUpdate ? "This .blend was built by an older Weapon Lab and cannot export yet. Updating adds what export needs; your .blend and its edits are not touched."
            : hasBlend ? "Rebuild the .blend from the game clips. The current .blend is backed up to blender/backups first."
            : "Build a Blender file with the arms and weapon rigs, one Action per clip and preview meshes (events stay in Anim Events). Creates the weapon's sandbox first if needed, and asks where to save the .blend.");
        BuildBlendButton.Content = needsUpdate ? "Update for export" : hasBlend ? "Rebuild .blend" : "Build .blend";
        OpenBlendButton.IsEnabled = hasBlend && _blender is not null && !_buildingBlend;
        DeleteBlendButton.IsEnabled = hasBlend && !_buildingBlend;
        MoveBlendButton.IsEnabled = hasBlend && !_buildingBlend;
        ShowBlendButton.IsEnabled = hasBlend;
        UpdateSuggestion();
        RefreshChecksButton();
    }

    /// <summary>Deletes the weapon's .blend (and Blender's .blend1 copy) after asking; exports and rebuild backups stay.</summary>
    private async void OnDeleteBlendClicked(object? sender, RoutedEventArgs e)
    {
        if (_weapon is not { } weapon || CurrentSandbox() is not { } sandbox || !BlendBuilder.HasBlend(sandbox))
        {
            return;
        }

        var blend = BlendBuilder.BlendPath(sandbox);
        if (!await Dialogs.ConfirmAsync(this, "Delete Blender file",
                $"Are you sure you want to delete {weapon.Id}'s Blender file?\n\n{blend}\n\nThis cannot be undone, and any unexported work in it is lost. " +
                "Exported clips, rebuild backups (blender\\backups) and the rest of the sandbox stay. Close it in Blender first.", "Delete", destructive: true))
        {
            return;
        }

        try
        {
            File.Delete(blend);
            if (File.Exists(blend + "1"))
            {
                File.Delete(blend + "1"); // Blender's own previous-save copy.
            }

            Log($"{weapon.Id}: deleted its Blender file {blend}.");
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            await Dialogs.ShowAsync(this, "Delete Blender file", $"The file could not be deleted (is it open in Blender?): {error.Message}");
        }

        RefreshWeaponActions();
        RefreshWeaponView(weapon);
        ApplyStatusToTree();
    }

    // ---- Property Browser ----

    private void ShowProperties(IReadOnlyList<PropertyGroup> groups)
    {
        _properties = groups;
        ApplyPropertyFilter();
    }

    private void OnPropertyFilterChanged(object? sender, TextChangedEventArgs e)
    {
        ApplyPropertyFilter();
    }

    private void ApplyPropertyFilter()
    {
        PropertyList.ItemsSource = ClipPresenter.Filter(_properties, PropertyFilterBox.Text?.Trim() ?? "");
    }

    // ---- Events / Tracks / Exports ----

    private void OnViewChanged(object? sender, SelectionChangedEventArgs e)
    {
        if (EventsView is null || TracksView is null || ExportsView is null)
        {
            return; // Fired while the window is still loading.
        }

        EventsView.IsVisible = ViewTabs.SelectedIndex == 0;
        TracksView.IsVisible = ViewTabs.SelectedIndex == 1;
        ExportsView.IsVisible = ViewTabs.SelectedIndex == 2;
    }

    private void OnViewFilterChanged(object? sender, TextChangedEventArgs e)
    {
        ApplyViewFilter();
    }

    private void ApplyViewFilter()
    {
        if (_clipView is null)
        {
            return;
        }

        var filter = ViewFilterBox.Text?.Trim() ?? "";
        bool Matches(params string[] values) => filter.Length == 0 || values.Any(value => value.Contains(filter, StringComparison.OrdinalIgnoreCase));

        EventList.ItemsSource = _clipView.Events.Where(row => Matches(row.Name, row.Kind, row.Frame)).ToList();
        TrackList.ItemsSource = _clipView.Tracks.Where(row => Matches(row.Bone, row.Parent, row.Stream, row.Channels)).ToList();
    }

    private void RefreshExports()
    {
        var sandbox = CurrentSandbox();
        var rows = sandbox is null || _weapon is null
            ? []
            : _weapon.ClipPaths
                .Select(path => ClipExporter.LastExport(sandbox, ExplorerNode.ClipName(path)))
                .OfType<ClipExportReport>()
                .OrderByDescending(report => report.ExportedUtc)
                .Select(ExportRow.From)
                .ToList();
        ExportList.ItemsSource = rows;
        ExportsTab.Content = rows.Count == 0 ? "Exports" : $"Exports ({rows.Count})";
    }

    private async void OnDeleteExportClicked(object? sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: string clipId } || _weapon is not { } weapon || CurrentSandbox() is not { } sandbox)
        {
            return;
        }

        var delivered = Target() is { } target && AddonManifest.Read(target)?.Clips.GetValueOrDefault(weapon.Id)?.Contains(clipId) == true;
        if (!await Dialogs.ConfirmAsync(this, "Delete export",
                $"Delete the export of {clipId}? It counts as unedited again and will not be delivered." +
                (delivered ? $" It is in {Target()!.Name} now; applying the profile removes it there (or puts the addon's own version back)." : "") +
                "\n\nThe .blend keeps its keys, so you can export it again from Blender.", "Delete", destructive: true))
        {
            return;
        }

        try
        {
            ClipExporter.DeleteExport(sandbox, clipId);
            Log($"Deleted the export of {weapon.Id} / {clipId}.{(delivered ? " Apply the profile to update the addon." : "")}");
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            Log($"Could not delete the export of {clipId}: {error.Message}");
        }

        RefreshExports();
        RebuildTree();
        await RefreshStatusAsync();
        if (_clipPath is { } path && ExplorerNode.ClipName(path) == clipId)
        {
            await LoadClipAsync(weapon, path);
        }
    }

    // ---- Encoder verification ----

    private async void OnVerifyClipClicked(object? sender, RoutedEventArgs e)
    {
        if (_connection is not { } connection || _clipPath is not { } path)
        {
            return;
        }

        VerifyClipButton.IsEnabled = false;
        try
        {
            var result = await Task.Run(() => EncoderSelfTest.Run(connection.Archive.ReadBytes(path), path));
            Log(Describe(result));
        }
        finally
        {
            VerifyClipButton.IsEnabled = _clipPath is not null;
        }
    }

    private async void OnVerifyWeaponClicked(object? sender, RoutedEventArgs e)
    {
        if (_connection is not { } connection || _weapon is not { } weapon)
        {
            return;
        }

        VerifyWeaponButton.IsEnabled = false;
        SetStatus($"Verifying the encoder on {weapon.ClipPaths.Count} {weapon.Id} clips…");
        try
        {
            var results = await Task.Run(() => weapon.ClipPaths
                .Select(path => EncoderSelfTest.Run(connection.Archive.ReadBytes(path), path))
                .ToList());

            foreach (var failure in results.Where(result => !result.Passed))
            {
                Log(Describe(failure));
            }

            var passed = results.Count(result => result.Passed);
            var worst = results.Where(result => result.Passed).Select(result => result.MaxRotationErrorDegrees).DefaultIfEmpty(0).Max();
            Log($"Encoder check on {weapon.Id}: {passed} of {results.Count} clips passed (worst rotation error {worst:0.####}°).");
        }
        finally
        {
            VerifyWeaponButton.IsEnabled = true;
        }
    }

    private static string Describe(EncoderCheckResult result)
    {
        var name = ExplorerNode.ClipName(result.ResourcePath);
        return result.Passed
            ? $"Encoder check passed on {name}: byte-identical no-op, re-encode error {result.MaxTranslationError:0.######} units / {result.MaxRotationErrorDegrees:0.####}°, edit isolated."
            : $"Encoder check FAILED on {name} [{result.Check}]: {result.Message}";
    }

    // ---- Sandbox ----

    /// <summary>The weapon's sandbox, created from the game's clips when it does not exist yet.</summary>
    private async Task<SandboxInfo?> EnsureSandboxAsync(WeaponLab.Core.Game.GameConnection connection, WeaponEntry weapon)
    {
        if (_sandboxes.Exists(weapon.Id))
        {
            return _sandboxes.Open(weapon.Id);
        }

        try
        {
            var addon = _profile.TargetAddon;
            var progress = new Progress<string>(SetStatus);
            // Start from what the game plays: the addon's own clips when it has some for this weapon (user decision
            // 2026-11-07), unless Build .blend was told to use the game's. Before 0.22 this always took CS2's, so a
            // first edit could replace an addon's custom animation with CS2's.
            var target = Target();
            var fromAddon = target is not null && _profile.Weapon(weapon.Id).BuildFromAddon != false
                && weapon.ClipPaths.Any(path => WeaponLab.Core.Addons.AddonInventory.ClipFile(target, _inventory, path) is not null);
            var sandbox = await Task.Run(() => _sandboxes.Create(connection, weapon, addon, progress, fromAddon ? target : null));
            Log($"Created the sandbox for {weapon.Id} with {sandbox.Manifest.Clips.Count} clips"
                + (sandbox.Manifest.SourceAddon is { } source ? $", the addon's own where {source} has them." : " from the game."));
            return sandbox;
        }
        catch (Exception error) when (error is IOException or InvalidDataException or ArgumentException or UnauthorizedAccessException)
        {
            Log($"Could not create the sandbox for {weapon.Id}: {error.Message}");
            return null;
        }
    }

    private async void OnMoveBlendClicked(object? sender, RoutedEventArgs e)
    {
        if (_weapon is { } weapon)
        {
            await MoveBlendAsync(weapon);
        }
    }

    private void OnShowBlendClicked(object? sender, RoutedEventArgs e)
    {
        if (CurrentSandbox() is { } sandbox && BlendBuilder.HasBlend(sandbox))
        {
            Process.Start(new ProcessStartInfo("explorer.exe", $"/select,\"{BlendBuilder.BlendPath(sandbox)}\"") { UseShellExecute = false });
        }
    }

    private async void OnMoveBlendMenuClicked(object? sender, RoutedEventArgs e)
    {
        if (MenuNode(sender) is { Weapon: { } weapon })
        {
            await MoveBlendAsync(weapon);
        }
    }

    /// <summary>
    /// Move .blend… (0.24): moves the weapon's .blend (and Blender's .blend1) to a folder the user picks and records
    /// it, so Build, Open and the add-on follow (the add-on finds the sandbox from the file's own meta since 0.21).
    /// </summary>
    private async Task MoveBlendAsync(WeaponEntry weapon)
    {
        if (!_sandboxes.Exists(weapon.Id) || _sandboxes.Open(weapon.Id) is not { } sandbox || !BlendBuilder.HasBlend(sandbox))
        {
            Log($"{weapon.Id} has no .blend yet. Build one in the Animations tab.");
            return;
        }

        var from = BlendBuilder.BlendPath(sandbox);
        if (!await Dialogs.ConfirmAsync(this, "Move .blend", $"Move {Path.GetFileName(from)} from\n{Path.GetDirectoryName(from)}\nto a folder you pick next?\n\n"
                + "Close it in Blender first: Blender would keep saving to the old place.", "Pick the new place…"))
        {
            return;
        }

        var start = WeaponLab.Core.Settings.AppSettings.Load().BlendFolder is { Length: > 0 } folder ? Path.Combine(folder, _profile.Name) : Path.GetDirectoryName(from)!;
        Directory.CreateDirectory(start);
        var file = await StorageProvider.SaveFilePickerAsync(new FilePickerSaveOptions
        {
            Title = $"Move {weapon.Id}.blend to",
            SuggestedFileName = Path.GetFileName(from),
            SuggestedStartLocation = await StorageProvider.TryGetFolderFromPathAsync(new Uri(start)),
            DefaultExtension = ".blend",
            ShowOverwritePrompt = false,
            FileTypeChoices = [new FilePickerFileType("Blender file") { Patterns = ["*.blend"] }],
        });
        if (file?.TryGetLocalPath() is not { } to || string.Equals(Path.GetFullPath(to), Path.GetFullPath(from), StringComparison.OrdinalIgnoreCase))
        {
            return;
        }

        try
        {
            if (File.Exists(to))
            {
                throw new IOException($"{to} already exists; pick another name or folder.");
            }

            Directory.CreateDirectory(Path.GetDirectoryName(to)!);
            File.Move(from, to);
            if (File.Exists(from + "1"))
            {
                File.Move(from + "1", to + "1", overwrite: true);
            }

            BlendBuilder.SetBlendPath(sandbox, to);
            Log($"Moved {weapon.Id}.blend to {to}.");
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            await Dialogs.ShowAsync(this, "Move .blend", $"Could not move {Path.GetFileName(from)}: {error.Message}");
        }

        RefreshWeaponPanel();
        RefreshWeaponActions();
    }

    private async void OnSettingsClicked(object? sender, RoutedEventArgs e)
    {
        var workspaceChanged = await SettingsDialog.ShowAsync(this, _sandboxes.WorkspacePath);
        await LocateBlenderAsync(); // A newly selected Blender takes effect immediately for Build, Open and add-on actions.
        if (workspaceChanged)
        {
            await Dialogs.ShowAsync(this, "Settings", "The new workspace folder is used when Weapon Lab starts again. Nothing was moved: copy your old workspace there first if you want your profiles along.");
        }
    }

    /// <summary>Asks where to save the .blend, suggesting where it is now; false when cancelled.</summary>
    private async Task<bool> ChooseBlendLocationAsync(SandboxInfo sandbox)
    {
        var current = BlendBuilder.BlendPath(sandbox);
        if (!BlendBuilder.HasBlend(sandbox) && WeaponLab.Core.Settings.AppSettings.Load().BlendFolder is { Length: > 0 } blendFolder)
        {
            current = Path.Combine(blendFolder, _profile.Name, sandbox.Manifest.WeaponId + ".blend"); // Settings: new .blend files go here (0.24).
        }

        Directory.CreateDirectory(Path.GetDirectoryName(current)!);
        var folder = await StorageProvider.TryGetFolderFromPathAsync(new Uri(Path.GetDirectoryName(current)!));
        var file = await StorageProvider.SaveFilePickerAsync(new Avalonia.Platform.Storage.FilePickerSaveOptions
        {
            Title = $"Save {sandbox.Manifest.WeaponId}.blend",
            SuggestedFileName = Path.GetFileName(current),
            SuggestedStartLocation = folder,
            DefaultExtension = ".blend",
            ShowOverwritePrompt = false,
            FileTypeChoices = [new Avalonia.Platform.Storage.FilePickerFileType("Blender file") { Patterns = ["*.blend"] }],
        });
        if (file?.TryGetLocalPath() is not { } chosen)
        {
            return false;
        }

        BlendBuilder.SetBlendPath(sandbox, chosen);
        return true;
    }

    private void OnOpenSandboxClicked(object? sender, RoutedEventArgs e)
    {
        if (_weapon is { } weapon && _sandboxes.Exists(weapon.Id))
        {
            Process.Start(new ProcessStartInfo(_sandboxes.FolderFor(weapon.Id)) { UseShellExecute = true });
        }
    }

    // ---- Blender ----

    private async void OnBuildBlendClicked(object? sender, RoutedEventArgs e)
    {
        if (_connection is not { } connection || _blender is not { } blender || _weapon is not { } selected)
        {
            return;
        }

        // An older .blend that only lacks export references is updated in place; anything else opens the Build window.
        WeaponLab.Core.Sandbox.SandboxInfo sandbox;
        if (_sandboxes.Exists(selected.Id) && BlendBuilder.NeedsUpdate(_sandboxes.Open(selected.Id)))
        {
            sandbox = _sandboxes.Open(selected.Id);
        }
        else
        {
            if (await ShowBuildBlendDialogAsync(connection, selected) is not { } choices
                || await ApplyBuildChoicesAsync(connection, selected, choices) is not { } prepared)
            {
                return;
            }

            sandbox = prepared;
            InvalidateViewport(); // The skeleton or the model may have changed.
            RefreshSkeletonPanel();
        }

        var weaponId = sandbox.Manifest.WeaponId;
        var updateOnly = BlendBuilder.NeedsUpdate(sandbox);
        var carryKeys = false;
        if (!updateOnly && BlendBuilder.HasBlend(sandbox))
        {
            // 0.24: a rebuild can keep the animation in the current .blend (keys of bones that still exist with the same rest).
            var answer = await Dialogs.ChooseAsync(this, "Rebuild Blender file",
                $"Rebuilding replaces {weaponId}.blend with a new file generated from the chosen clips and skeleton.\n\n"
                + "Keep my animation: your keys in the current .blend are carried into the new one for every bone that still exists with the same parent and rest; "
                + "new bones start with the clip's motion. From the clips only: the new file starts from the clips (your exports stay, but the .blend no longer shows them).\n\n"
                + "Either way a copy of the current file is saved in the sandbox's blender\\backups folder first. Save it in Blender before rebuilding.",
                "Keep my animation", "From the clips only");
            if (answer < 0)
            {
                return;
            }

            carryKeys = answer == 0;
        }

        // A full build asks where the file goes; the default is the sandbox's blender folder.
        if (!updateOnly && !await ChooseBlendLocationAsync(sandbox))
        {
            return;
        }

        // The weapon's viewport model; none picked is CS2's own (0.21).
        var previewModel = _profile.Weapon(weaponId).PreviewModel;
        _buildingBlend = true;
        RefreshWeaponActions();
        Log(updateOnly
            ? $"Updating {weaponId} for export (your .blend is not touched)…"
            : $"Building {weaponId}.blend with {blender}{(previewModel is null ? "" : " and preview model " + previewModel)}…");
        try
        {
            var progress = new Progress<string>(SetStatus);
            var settings = _profile.Weapon(weaponId);
            var arms = new ArmsPreview(settings.ArmsModel ?? ArmsPreview.Default.Sleeves, settings.GlovesModel ?? ArmsPreview.Default.Gloves);
            var result = await BlendBuilder.BuildAsync(sandbox, connection, blender, progress, referencesOnly: updateOnly, previewModel: previewModel, arms: arms, propBones: settings.PropBones, carryKeys: carryKeys);
            WriteGameTiming(sandbox); // The build renews the input folder the timing file lives in.
            var previews = result.Previews.Count == 0 ? "no preview meshes" : "previews: " + string.Join(", ", result.Previews);
            Log(updateOnly
                ? $"{weaponId} is ready to export: reference samples added for {result.Actions} clips. Your .blend and its edits were kept."
                : $"Built {weaponId}.blend: {result.Actions} actions{(result.ThirdPersonClips > 0 ? $" ({result.ThirdPersonClips} third person, on the Body rig)" : "")}, {previews}.");
            if (result.Carried is { } carried)
            {
                Log($"{weaponId}: your keys were carried into the new .blend for {carried.Bones} bone(s) in {carried.Clips} clip(s)."
                    + (carried.NotCarried.Count == 0 ? "" : $" Kept the clip's motion (the bone is gone, or its rest or parent changed): {string.Join(", ", carried.NotCarried)}."));
            }

            foreach (var warning in result.Warnings)
            {
                Log($"{weaponId}: {warning}");
            }
        }
        catch (BlendBuildException error)
        {
            var logPath = Path.Combine(sandbox.FolderPath, "blender", "build.log");
            Directory.CreateDirectory(Path.GetDirectoryName(logPath)!);
            await File.WriteAllTextAsync(logPath, error.Log);
            Log($"Building {weaponId}.blend failed: {error.Message} (Blender output saved to {logPath})");
        }
        catch (Exception error) when (error is IOException or InvalidDataException or UnauthorizedAccessException)
        {
            Log($"Building {weaponId}.blend failed: {error.Message}");
        }
        finally
        {
            _buildingBlend = false;
            RefreshWeaponActions();
            if (_weapon is { } weapon)
            {
                RefreshWeaponView(weapon);
            }
        }
    }

    /// <summary>Optional: installs or removes the Weapon Lab panel in the user's Blender (see <see cref="BlenderAddon"/>).</summary>
    private async void OnBlenderAddonClicked(object? sender, RoutedEventArgs e)
    {
        if (_blender is not { } blender)
        {
            await Dialogs.ShowAsync(this, "Blender add-on", "Blender was not found. Install Blender 4.4 or newer, or set WEAPONLAB_BLENDER to its blender.exe.");
            return;
        }

        try
        {
            SetStatus($"Asking {blender} about the add-on…");
            var state = await BlenderAddon.StatusAsync(blender);
            var choice = await Dialogs.ChooseAsync(this, "Blender add-on",
                (state.Installed
                    ? $"The Weapon Lab panel is installed in {blender}, so it is there when you open a .blend directly.\n\n"
                    : $"The Weapon Lab panel is not installed in {blender}. Open in Blender loads it for that session; installing it also makes it appear when you open a .blend directly.\n\n") +
                "What gets installed is a small loader that uses this Weapon Lab's own panel and export tool, so it stays up to date. " +
                "Your other Blender settings are kept. If you move Weapon Lab, install it again.\n\n" + state.Path,
                state.Installed ? ["Remove from Blender", "Install again"] : ["Install into Blender"]);
            if (choice < 0)
            {
                SetStatus("Ready");
                return;
            }

            var remove = state.Installed && choice == 0;
            SetStatus(remove ? "Removing the add-on from Blender…" : "Installing the add-on into Blender…");
            state = remove ? await BlenderAddon.RemoveAsync(blender) : await BlenderAddon.InstallAsync(blender);
            Log(state.Installed
                ? $"The Weapon Lab panel is installed in {blender}; .blend files opened directly now have it ({state.Path})."
                : $"The Weapon Lab panel was removed from {blender}. Open in Blender still loads it for that session.");
        }
        catch (Exception error) when (error is IOException or InvalidOperationException or System.ComponentModel.Win32Exception)
        {
            Log($"Blender add-on: {error.Message}");
            await Dialogs.ShowAsync(this, "Blender add-on", error.Message);
        }
    }

    private void OnOpenBlendClicked(object? sender, RoutedEventArgs e)
    {
        if (_blender is { } blender && CurrentSandbox() is { } sandbox && BlendBuilder.HasBlend(sandbox))
        {
            WriteGameTiming(sandbox);
            BlendBuilder.Open(sandbox, blender);
            Log($"Opened {Path.GetFileName(BlendBuilder.BlendPath(sandbox))} in {blender}. In Blender, press N and pick the Weapon Lab tab in the sidebar.");
        }
    }
}
