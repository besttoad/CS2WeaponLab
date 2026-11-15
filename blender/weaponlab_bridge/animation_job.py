"""Weapon Lab's background animation jobs (0.25), on a copy of the weapon's .blend:

    blender --background --factory-startup <copy.blend> --python animation_job.py --
        <mode> <input folder> <clip> <file> [<sample out>]

Modes: export-keys, export-fbx (write the clip to <file>), import-keys, import-fbx (put <file> on the clip, then
write the clip's poses to <sample out> for Weapon Lab to export). Prints WEAPONLAB_RESULT {json}.
"""
import json
import sys
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import animation_io  # noqa: E402  (needs the path above)
import pose_io  # noqa: E402


def clip_frames(input_dir, clip):
    lengths_file = input_dir / "clip_lengths.json"
    lengths = json.loads(lengths_file.read_text(encoding="utf-8")).get("clips", {}) if lengths_file.is_file() else {}
    rig_doc = json.loads((input_dir / "rig.json").read_text(encoding="utf-8"))
    return int(lengths.get(clip) or next(item for item in rig_doc["clips"] if item["id"] == clip)["frames"]), rig_doc


def main():
    arguments = sys.argv[sys.argv.index("--") + 1:]
    if len(arguments) < 4:
        raise RuntimeError("Usage: ... --python animation_job.py -- <mode> <input folder> <clip> <file> [<sample out>]")
    mode, input_dir, clip, file = arguments[0], Path(arguments[1]), arguments[2], Path(arguments[3])
    scene = bpy.context.scene
    raw_meta = scene.get("weaponlab_meta")
    if raw_meta is None:
        raw_meta = scene.get("weaponlab")
    if not isinstance(raw_meta, str) or not raw_meta:
        raise RuntimeError("The .blend has no Weapon Lab project metadata. Build it again from Weapon Lab.")
    meta = json.loads(raw_meta)
    action = bpy.data.actions[meta["actions"][clip]]
    # A third-person clip (0.26) plays on the Body rig and its own weapon rig.
    arms, weapon = pose_io.scene_rigs(scene, meta, pose_io.is_world(action))
    if arms is None:
        raise RuntimeError("The .blend has no rig for " + clip + ". Build the .blend again.")
    pose_io.show_view(bpy.context.view_layer, pose_io.is_world(action))
    frames, rig_doc = clip_frames(input_dir, clip)
    result = {"mode": mode, "clip": clip}
    if mode == "export-keys":
        animation_io.export_keys(file, action, arms, weapon)
    elif mode == "export-fbx":
        animation_io.export_fbx(file, scene, arms, weapon, action, frames)
    elif mode in ("import-keys", "import-fbx"):
        if len(arguments) < 5:
            raise RuntimeError("An import needs the sample output path.")
        result.update(animation_io.import_keys(file, action, arms, weapon) if mode == "import-keys"
                      else animation_io.import_fbx(file, scene, arms, weapon, action, frames))
        pose_io.write_poses(Path(arguments[4]), animation_io.sample_action(scene, rig_doc, arms, weapon, action, frames))
    else:
        raise RuntimeError("Unknown mode " + mode)
    print("WEAPONLAB_RESULT " + json.dumps(result), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - reported to Weapon Lab
        traceback.print_exc()
        print("WEAPONLAB_ERROR " + str(error), flush=True)
        sys.exit(1)
