using System.Diagnostics;
using System.Text.RegularExpressions;
using WeaponLab.Core.Game;

namespace WeaponLab.Core.Blender;

/// <param name="Version">Parsed from <c>blender --version</c>.</param>
public sealed record BlenderInstall(string ExecutablePath, Version Version, string VersionText)
{
    public override string ToString() => $"Blender {VersionText}";
}

/// <summary>
/// Finds Blender: the <c>WEAPONLAB_BLENDER</c> environment variable, Steam
/// libraries (<c>steamapps/common/Blender</c>) and Program Files installs.
/// Picks the newest version that supports slotted Actions.
/// </summary>
public static partial class BlenderLocator
{
    public static readonly Version Minimum = new(4, 4);

    [GeneratedRegex(@"Blender\s+(?<version>\d+\.\d+(\.\d+)?)(?<suffix>[^\r\n]*)")]
    private static partial Regex VersionLine();

    public static BlenderInstall? Locate(string? preferredExecutable = null)
    {
        // A path chosen in Settings is deliberate: use it even when a newer Blender is installed.
        if (!string.IsNullOrWhiteSpace(preferredExecutable)
            && Probe(preferredExecutable.Trim('"')) is { } preferred
            && preferred.Version >= Minimum)
        {
            return preferred;
        }

        // If the saved executable is missing or unsupported, fall back to automatic discovery.
        return Candidates()
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .Where(File.Exists)
            .Select(Probe)
            .OfType<BlenderInstall>()
            .Where(install => install.Version >= Minimum)
            .OrderByDescending(install => install.Version)
            .FirstOrDefault();
    }

    public static IEnumerable<string> Candidates(string? preferredExecutable = null)
    {
        if (!string.IsNullOrWhiteSpace(preferredExecutable))
        {
            yield return preferredExecutable.Trim('"');
        }

        var configured = Environment.GetEnvironmentVariable("WEAPONLAB_BLENDER");
        if (!string.IsNullOrWhiteSpace(configured))
        {
            yield return configured.Trim('"');
        }

        foreach (var library in GameLocator.SteamLibraries())
        {
            yield return Path.Combine(library, "steamapps", "common", "Blender", "blender.exe");
        }

        foreach (var variable in new[] { "ProgramFiles", "ProgramFiles(x86)" })
        {
            var root = Environment.GetEnvironmentVariable(variable);
            var foundation = root is null ? null : Path.Combine(root, "Blender Foundation");
            if (foundation is null || !Directory.Exists(foundation))
            {
                continue;
            }

            foreach (var folder in Directory.EnumerateDirectories(foundation))
            {
                yield return Path.Combine(folder, "blender.exe");
            }
        }
    }

    /// <summary>Runs <c>blender --version</c>; returns null if it does not answer like Blender.</summary>
    public static BlenderInstall? Probe(string executable)
    {
        try
        {
            using var process = Process.Start(new ProcessStartInfo(executable, "--version")
            {
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            });
            if (process is null)
            {
                return null;
            }

            var output = process.StandardOutput.ReadToEnd();
            if (!process.WaitForExit(30_000))
            {
                process.Kill();
                return null;
            }

            return ParseVersion(executable, output);
        }
        catch (Exception error) when (error is System.ComponentModel.Win32Exception or IOException or InvalidOperationException)
        {
            return null;
        }
    }

    internal static BlenderInstall? ParseVersion(string executable, string versionOutput)
    {
        var match = VersionLine().Match(versionOutput);
        if (!match.Success || !Version.TryParse(match.Groups["version"].Value, out var version))
        {
            return null;
        }

        return new BlenderInstall(executable, version, (match.Groups["version"].Value + match.Groups["suffix"].Value).Trim());
    }
}
