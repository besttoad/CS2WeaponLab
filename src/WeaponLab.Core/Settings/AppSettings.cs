using System.Text.Json;
using System.Text.Json.Serialization;
using WeaponLab.Core.IO;

namespace WeaponLab.Core.Settings;

/// <summary>
/// Weapon Lab's own settings (0.24 Settings window), shared by every version and by the app and the CLI:
/// <c>%APPDATA%\CS2WeaponLab\settings.json</c>. Kept outside the workspace, since one of them says where the
/// workspace is. Plain JSON, written atomically.
/// </summary>
public sealed class AppSettings
{
    public const string FileName = "settings.json";

    private static readonly JsonSerializerOptions Json = new()
    {
        WriteIndented = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    /// <summary>Where profiles, sandboxes, backups and deliveries live; null = the default (beside the app, see <see cref="Sandbox.SandboxStore.DefaultWorkspacePath"/>).</summary>
    public string? WorkspaceFolder { get; set; }

    /// <summary>Where new .blend files are suggested: <c>&lt;folder&gt;\&lt;profile&gt;\&lt;weapon&gt;.blend</c>; null = in the weapon's sandbox (blender\).</summary>
    public string? BlendFolder { get; set; }

    /// <summary>The Blender executable selected in Settings; null = detect the newest installed Blender 4.4 or newer.</summary>
    public string? BlenderExecutablePath { get; set; }

    /// <summary>The settings file; <c>WEAPONLAB_SETTINGS</c> points elsewhere (tests).</summary>
    public static string FilePath =>
        Environment.GetEnvironmentVariable("WEAPONLAB_SETTINGS") is { Length: > 0 } custom
            ? custom
            : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "CS2WeaponLab", FileName);

    public static bool Exists => File.Exists(FilePath);

    /// <summary>The saved settings, or defaults when there is no file or it cannot be read.</summary>
    public static AppSettings Load()
    {
        try
        {
            return File.Exists(FilePath) ? JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(FilePath), Json) ?? new AppSettings() : new AppSettings();
        }
        catch (Exception error) when (error is IOException or JsonException or UnauthorizedAccessException)
        {
            return new AppSettings();
        }
    }

    public void Save()
    {
        Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
        AtomicFile.WriteAllText(FilePath, JsonSerializer.Serialize(this, Json) + "\n");
    }
}
