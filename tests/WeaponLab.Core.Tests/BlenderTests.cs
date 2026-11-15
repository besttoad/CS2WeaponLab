using System.IO.Compression;
using System.Text.Json;
using WeaponLab.Core.Blender;
using WeaponLab.Core.Game;
using WeaponLab.Core.Sandbox;

namespace WeaponLab.Core.Tests;

public sealed class BlenderTests : IDisposable
{
    private readonly string _root = Path.Combine(Path.GetTempPath(), "weaponlab-tests-" + Guid.NewGuid().ToString("N"));

    public void Dispose()
    {
        if (Directory.Exists(_root))
        {
            Directory.Delete(_root, recursive: true);
        }
    }

    [Theory]
    [InlineData("Blender 5.2.3 LTS\r\n\tbuild date: 2026-10-22\r\n", "5.2.3", "5.2.3 LTS")]
    [InlineData("Blender 4.4.0\n", "4.4.0", "4.4.0")]
    [InlineData("Blender 4.5.0\n", "4.5.0", "4.5.0")]
    [InlineData("Blender 3.6.5\n", "3.6.5", "3.6.5")]
    public void VersionIsParsedFromBlenderOutput(string output, string version, string text)
    {
        var install = BlenderLocator.ParseVersion("blender.exe", output);

        Assert.NotNull(install);
        Assert.Equal(Version.Parse(version), install.Version);
        Assert.Equal(text, install.VersionText);
    }

    [Fact]
    public void NonBlenderOutputIsRejected()
    {
        Assert.Null(BlenderLocator.ParseVersion("other.exe", "Usage: other [options]"));
    }

    [Fact]
    public void UserSelectedBlenderIsTheFirstCandidate()
    {
        const string selected = @"C:\Blender 4.4\blender.exe";

        Assert.Equal(selected, BlenderLocator.Candidates(selected).First());
    }

    [Theory]
    [InlineData("rifle_ak", "AK-47")]
    [InlineData("_default_pistol", "USP-S (default pistol)")]
    [InlineData("rifle_awp", "AWP")]
    [InlineData("knife_karambit", "KARAMBIT")]
    [InlineData("inspects", "INSPECTS")]
    public void WeaponLabelsAreReadable(string id, string label)
    {
        Assert.Equal(label, WeaponLabels.For(id));
    }

    /// <summary>Writes the AK's rig package from a real sandbox; skipped without CS2.</summary>
    [Fact]
    public void RigPackageDescribesSkeletonsClipsAndPoses()
    {
        if (GameLocator.Locate() is null)
        {
            Assert.Skip("CS2 is not installed on this machine.");
        }

        using var connection = GameConnection.Open();
        var store = new SandboxStore(_root);
        var weapon = connection.FindWeapon("rifle_ak")!;
        var sandbox = store.Create(connection, weapon, targetAddon: null);

        var result = RigPackage.Write(sandbox, connection);

        using var rig = JsonDocument.Parse(File.ReadAllText(Path.Combine(result.InputPath, RigPackage.RigFileName)));
        var root = rig.RootElement;
        Assert.Equal(RigPackage.Schema, root.GetProperty("schema").GetString());
        Assert.Equal("AK-47", root.GetProperty("label").GetString());
        Assert.Equal("idle_ak", root.GetProperty("defaultClip").GetString());
        Assert.Equal(56, root.GetProperty("skeletons").GetProperty("arms").GetProperty("bones").GetArrayLength());
        Assert.Equal(8, root.GetProperty("skeletons").GetProperty("weapon").GetProperty("bones").GetArrayLength());
        // First person: every clip (11 before CS2 1.41.9.3, 12 since); third person (0.26): the 6 full ones, on the body.
        var views = root.GetProperty("clips").EnumerateArray().GroupBy(clip => clip.TryGetProperty("view", out var view) ? view.GetString() : null).ToDictionary(group => group.Key ?? "first", group => group.Count());
        Assert.Equal(weapon.ClipPaths.Count, views["first"]);
        Assert.Equal(6, views[RigPackage.WorldView]);
        Assert.Equal(75, root.GetProperty("skeletons").GetProperty("body").GetProperty("bones").GetArrayLength());
        Assert.Equal(8, root.GetProperty("skeletons").GetProperty("worldWeapon").GetProperty("bones").GetArrayLength());
        Assert.Equal(4, result.PreviewCount); // weapon, arms, gloves, and the agent's third-person body

        var reload = root.GetProperty("clips").EnumerateArray().Single(clip => clip.GetProperty("id").GetString() == "reload_ak");
        var reloadInfo = connection.ReadClip(weapon.ClipPaths.Single(path => path.EndsWith("/reload_ak.vnmclip_c", StringComparison.Ordinal)));
        Assert.Equal(reloadInfo.Events.Count, reload.GetProperty("events").GetArrayLength());
        using var file = File.OpenRead(Path.Combine(result.InputPath, reload.GetProperty("poses").GetString()!));
        using var gzip = new GZipStream(file, CompressionMode.Decompress);
        using var poses = JsonDocument.Parse(gzip);
        var arms = poses.RootElement.GetProperty("arms");
        Assert.Equal(74 * 56 * 8, arms.GetProperty("values").GetArrayLength());
        Assert.Equal(74 * 8 * 8, poses.RootElement.GetProperty("weapon").GetProperty("values").GetArrayLength());
        Assert.False(root.GetProperty("skeletons").GetProperty("weapon").TryGetProperty("freeBones", out _)); // No props unless ticked.
    }

    /// <summary>
    /// A bone ticked "Keep apart from the gun" reaches the .blend as a prop (rig.json freeBones), on CS2's own
    /// skeleton too (0.23: a magazine kept in the left hand); the root never does. Skipped without CS2.
    /// </summary>
    [Fact]
    public void TickedBonesReachTheBlendAsProps()
    {
        if (GameLocator.Locate() is null)
        {
            Assert.Skip("CS2 is not installed on this machine.");
        }

        using var connection = GameConnection.Open();
        var store = new SandboxStore(_root);
        var sandbox = store.Create(connection, connection.FindWeapon("rifle_ak")!, targetAddon: null);
        var weaponBones = connection.ReadSkeleton("animation/skeletons/weapons/ak47.vnmskel").Bones;
        var prop = weaponBones.First(bone => bone.ParentIndex >= 0).Name;

        var result = RigPackage.Write(sandbox, connection, propBones: [prop, weaponBones[0].Name, "not_a_bone"]);

        using var rig = JsonDocument.Parse(File.ReadAllText(Path.Combine(result.InputPath, RigPackage.RigFileName)));
        var free = rig.RootElement.GetProperty("skeletons").GetProperty("weapon").GetProperty("freeBones").EnumerateArray().Select(item => item.GetString()).ToList();
        Assert.Equal([prop], free);
    }
}
